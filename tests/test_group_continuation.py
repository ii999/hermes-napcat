"""Invited group continuation with simulated classifier and dispatch boundaries."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from hermes_napcat import group_chat
from hermes_napcat.engagement import Decision
from hermes_napcat.protocol import Target
from test_group_chat import MENTION, controller
from test_group_context import config, event


def continuation_config(*, continuation=None, proactive=None, **overrides):
    options = {
        "enabled": False, "dry_run": False, "quiet_window_ms": 100,
        "classifier": {"enabled": True, "model": "test-classifier"},
        "continuation": {"enabled": True, **(continuation or {})},
    }
    options.update(proactive or {})
    return config(proactive_assist=options, messages_per_minute=300, **overrides)


@pytest.fixture
def clock(monkeypatch):
    current = [100.0]
    monkeypatch.setattr(group_chat, "time", SimpleNamespace(
        monotonic=lambda: current[0], time=time.time))
    return current


def continuing_controller(settings=None, **kwargs):
    ctl = controller(settings or continuation_config(), **kwargs)
    ctl.classifier.decide = AsyncMock(return_value=Decision(True, .99, "test", "continue"))
    return ctl


async def invite(ctl, identifier=1, **kwargs):
    await ctl.receive(event(identifier, text="请帮我分析这个错误", parts=[MENTION], **kwargs))
    await ctl.wait_idle()


async def test_only_invited_groups_follow_up_and_preserve_current_speaker():
    ctl = continuing_controller(continuation_config(
        allowed_users=["200", "201"], allowed_groups=["300", "301"]))
    try:
        await ctl.receive(event(1, text="有人知道吗？"))
        await ctl.wait_idle()
        ctl.classifier.decide.assert_not_awaited()
        await invite(ctl, 2)
        await ctl.receive(event(3, user=201, text="补充一下，是启动的时候报错"))
        await ctl.wait_idle()
        turn = ctl.dispatch.await_args.args[0]
        assert turn.continuation and turn.proactive and turn.incoming.user_id == "201"
        assert turn.text == "补充一下，是启动的时候报错"
        candidate, context = ctl.classifier.decide.await_args.args
        assert candidate.conversation_start_id == "2"
        assert '"message_id":"2"' in context
        await ctl.receive(event(4, group=301, text="这个也是"))
        await ctl.wait_idle()
        assert ctl.dispatch.await_count == 2
    finally:
        await ctl.close()


async def test_related_listening_renews_but_unrelated_traffic_does_not(clock):
    ctl = continuing_controller()
    try:
        await invite(ctl)
        clock[0] = 210
        ctl.classifier.decide.return_value = Decision(False, .99, "test", "continue")
        await ctl.receive(event(2, text="我先检查下日志"))
        await ctl.wait_idle()
        clock[0] = 300
        ctl.classifier.decide.return_value = Decision(False, .99, "test", "unrelated")
        await ctl.receive(event(3, text="午饭吃什么"))
        await ctl.wait_idle()
        clock[0] = 331
        await ctl.receive(event(4, text="有人知道怎么办吗？"))
        await ctl.wait_idle()
        assert ctl.classifier.decide.await_count == 2
        assert ctl.dispatch.await_count == 1
        await invite(ctl, 5)
        ctl.classifier.decide.return_value = Decision(True, .99, "test", "continue")
        await ctl.receive(event(6, text="日志是这样的"))
        await ctl.wait_idle()
        assert ctl.dispatch.await_count == 3
    finally:
        await ctl.close()


async def test_topic_end_waits_for_a_new_invitation():
    ctl = continuing_controller()
    try:
        await invite(ctl)
        ctl.classifier.decide.return_value = Decision(False, .99, "test", "end")
        await ctl.receive(event(2, text="解决了，就这样吧"))
        await ctl.wait_idle()
        await ctl.receive(event(3, text="还有另外一个问题"))
        await ctl.wait_idle()
        assert ctl.classifier.decide.await_count == 1
        assert ctl.dispatch.await_count == 1
        await invite(ctl, 4)
        ctl.classifier.decide.return_value = Decision(True, .99, "test", "continue")
        await ctl.receive(event(5, text="这个情况也一样"))
        await ctl.wait_idle()
        assert ctl.dispatch.await_count == 3
    finally:
        await ctl.close()


async def test_hard_window_limit_survives_continuous_related_activity(clock):
    ctl = continuing_controller(continuation_config(
        continuation={"idle_seconds": 10, "max_duration_seconds": 20}))
    try:
        await invite(ctl)
        for identifier, when in [(2, 109), (3, 118), (4, 121)]:
            clock[0] = when
            await ctl.receive(event(identifier, text="再补充一点"))
            await ctl.wait_idle()
        assert ctl.classifier.decide.await_count == 2
        assert ctl.dispatch.await_count == 3
    finally:
        await ctl.close()


@pytest.mark.parametrize("extra", [
    {"user": 201}, {"user": 100}, {"group": 999},
    {"parts": [{"type": "at", "data": {"qq": "201"}}]},
    {"parts": [{"type": "reply", "data": {"id": "999"}}]},
    {"text": "/restart"},
])
async def test_continuation_excludes_unauthorized_or_directed_messages(extra):
    ctl = continuing_controller()
    try:
        await invite(ctl)
        await ctl.receive(event(2, **{"text": "继续分析", **extra}))
        await ctl.wait_idle()
        assert ctl.dispatch.await_count == 1
        ctl.classifier.decide.assert_not_awaited()
    finally:
        await ctl.close()


async def test_ignored_speakers_and_commands_cannot_drive_continuation():
    ctl = continuing_controller(continuation_config(proactive={"ignored_users": ["200"]}))
    try:
        await invite(ctl)
        await ctl.receive(event(2, text="继续"))
        await ctl.wait_idle()
        ctl.classifier.decide.assert_not_awaited()
    finally:
        await ctl.close()
    ctl = continuing_controller()
    try:
        await ctl.receive(event(1, text="/unknown", parts=[MENTION]))
        await ctl.wait_idle()
        await ctl.receive(event(2, text="继续"))
        await ctl.wait_idle()
        assert ctl.dispatch.await_count == 1
        ctl.classifier.decide.assert_not_awaited()
    finally:
        await ctl.close()


async def test_invitation_recall_closes_window():
    ctl = continuing_controller()
    try:
        await invite(ctl)
        await ctl.receive({"post_type": "notice", "notice_type": "group_recall",
                           "self_id": 100, "group_id": 300, "message_id": 1})
        await ctl.receive(event(2, text="继续"))
        await ctl.wait_idle()
        ctl.classifier.decide.assert_not_awaited()
    finally:
        await ctl.close()


@pytest.mark.parametrize("during_continuation", [False, True])
async def test_pending_followup_survives_generation_and_bot_send(during_continuation):
    entered, release = asyncio.Event(), asyncio.Event()
    turns = []

    async def dispatch(turn):
        turns.append(turn)
        if turn.continuation == during_continuation and not entered.is_set():
            entered.set()
            await release.wait()
        ctl.sent(turn.incoming.target, str(1000 + len(turns)), "send_group_msg",
                 {"message": [{"type": "text", "data": {"text": "generated answer"}}]})

    ctl = continuing_controller(dispatch=dispatch)
    try:
        await ctl.receive(event(1, text="分析一下", parts=[MENTION]))
        if during_continuation:
            await ctl.wait_idle()
            await ctl.receive(event(2, text="这个怎么改"))
        await asyncio.wait_for(entered.wait(), 1)
        await ctl.receive(event(3, text="补充：还会闪退"))
        await asyncio.sleep(.15)
        release.set()
        await asyncio.wait_for(ctl.wait_idle(), 2)
        assert turns[-1].incoming.message_id == "3" and turns[-1].continuation
        candidate, context = ctl.classifier.decide.await_args.args
        assert candidate.message_id == "3" and "generated answer" in context
        assert "generated answer" in turns[-1].context
    finally:
        release.set()
        await ctl.close()


async def test_immediate_followup_before_worker_starts_is_retained():
    ctl = continuing_controller()
    try:
        await ctl.receive(event(1, parts=[MENTION]))
        await ctl.receive(event(2, text="补充说明"))
        await ctl.wait_idle()
        assert ctl.dispatch.await_count == 2
        assert ctl.dispatch.await_args.args[0].continuation
    finally:
        await ctl.close()


async def test_pending_candidate_yields_to_observation_only_speaker():
    entered, release = asyncio.Event(), asyncio.Event()

    async def dispatch(turn):
        entered.set()
        await release.wait()

    ctl = continuing_controller(dispatch=dispatch)
    try:
        await ctl.receive(event(1, parts=[MENTION]))
        await asyncio.wait_for(entered.wait(), 1)
        await ctl.receive(event(2, text="补充"))
        await asyncio.sleep(.15)
        await ctl.receive(event(3, user=201, text="我已经解释过了"))
        release.set()
        await ctl.wait_idle()
        ctl.classifier.decide.assert_not_awaited()
    finally:
        release.set()
        await ctl.close()


@pytest.mark.parametrize("dry_run", [False, True])
async def test_followups_have_separate_budgets_and_obey_dry_run(dry_run):
    ctl = continuing_controller(continuation_config(
        continuation={"max_responses_per_hour": 1, "max_decisions_per_hour": 2},
        proactive={"dry_run": dry_run, "cooldown_seconds": 3600}))
    try:
        await invite(ctl)
        for identifier in range(2, 5):
            await ctl.receive(event(identifier, text="补充说明"))
            await ctl.wait_idle()
        assert ctl.classifier.decide.await_count == 2
        assert ctl.dispatch.await_count == (1 if dry_run else 2)
        assert ctl.stats["dry_run"] == int(dry_run)
    finally:
        await ctl.close()


@pytest.mark.parametrize("decision", [Decision(True, .5, "test", "continue"), ValueError("invalid")])
async def test_uncertain_decision_does_not_extend_window(clock, decision):
    ctl = continuing_controller()
    try:
        await invite(ctl)
        clock[0] = 210
        if isinstance(decision, Exception):
            ctl.classifier.decide.side_effect = decision
        else:
            ctl.classifier.decide.return_value = decision
        await ctl.receive(event(2, text="补充"))
        await ctl.wait_idle()
        clock[0] = 221
        await ctl.receive(event(3, text="补充"))
        await ctl.wait_idle()
        assert ctl.classifier.decide.await_count == 1 and ctl.dispatch.await_count == 1
    finally:
        await ctl.close()


async def test_expiry_during_classification_cannot_revive_conversation(clock):
    ctl = continuing_controller()
    try:
        await invite(ctl)
        async def decide(*args):
            clock[0] = 221
            return Decision(True, .99, "test", "continue")
        ctl.classifier.decide.side_effect = decide
        await ctl.receive(event(2, text="补充"))
        await ctl.wait_idle()
        assert ctl.dispatch.await_count == 1
        assert ctl.stats["stale_suppressed"] == 1
    finally:
        await ctl.close()


async def test_send_checks_conversation_expiry_and_preserves_started_send(clock):
    ctl = continuing_controller()
    try:
        await invite(ctl)
        await ctl.receive(event(2, text="补充"))
        await ctl.wait_idle()
        turn = ctl.dispatch.await_args.args[0]
        ctl.activate_turn(turn)
        clock[0] = 221
        with pytest.raises(PermissionError):
            ctl.before_send(Target.parse("group:300"))
        ctl.activate_turn(None)
        await invite(ctl, 3)
        await ctl.receive(event(4, text="再补充"))
        await ctl.wait_idle()
        ctl.activate_turn(ctl.dispatch.await_args.args[0])
        ctl.before_send(Target.parse("group:300"))
        clock[0] = 500
        ctl.before_send(Target.parse("group:300"))
    finally:
        ctl.activate_turn(None)
        await ctl.close()


@pytest.mark.parametrize("overrides", [
    {"group_context": {"enabled": False}},
    {"group_context": {"enabled": True, "observe_untriggered": False}},
    {"group_toolsets": ["terminal"]},
    {"proactive": {"classifier": {"enabled": False}, "dry_run": True},
     "group_toolsets": ["terminal"]},
    {"continuation": {"idle_seconds": 30, "max_duration_seconds": 20}},
])
def test_continuation_requires_live_context_and_no_tools(overrides):
    with pytest.raises(ValidationError):
        continuation_config(**overrides)
