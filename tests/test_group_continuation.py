"""Invited multi-person continuation at simulated admission and dispatch boundaries."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from hermes_napcat import continuation
from hermes_napcat.continuation import FollowupDecision
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
    monkeypatch.setattr(continuation, "time", SimpleNamespace(
        monotonic=lambda: current[0], time=time.time))
    return current


def select(batch, action="execute"):
    return FollowupDecision(action, tuple(row.message_id for row in batch.messages)
                            if action != "unrelated" else ())


def continuing_controller(settings=None, **kwargs):
    ctl = controller(settings or continuation_config(), **kwargs)
    ctl.classifier.decide_followup = AsyncMock(side_effect=select)
    return ctl


async def invite(ctl, identifier=1, **kwargs):
    await ctl.receive(event(identifier, text="Analyze the failure", parts=[MENTION], **kwargs))
    await ctl.wait_idle()


async def test_ordered_multi_member_batch_survives_unrelated_tail_and_human_quote():
    ctl = continuing_controller(continuation_config(allowed_users=["200", "201"]))
    try:
        await invite(ctl)
        def decide(batch):
            assert [(row.message_id, row.user_id) for row in batch.messages] == [
                ("2", "200"), ("3", "201"), ("4", "200")]
            assert batch.messages[1].reply_to == "2"
            return FollowupDecision("execute", ("2", "3"))
        ctl.classifier.decide_followup.side_effect = decide
        await ctl.receive(event(2, text="The error appears at startup"))
        await ctl.receive(event(3, user=201, text="Only with IPv6", parts=[
            {"type": "reply", "data": {"id": "2"}}]))
        await ctl.receive(event(4, text="Lunch is ready"))
        await ctl.wait_idle()
        turn = ctl.dispatch.await_args.args[0]
        assert turn.continuation and not turn.proactive and turn.incoming.user_id == "201"
        payload = json.loads(turn.context)
        assert payload["request"]["message_id"] == "1"
        assert [row["message_id"] for row in payload["pending_messages"]] == ["2", "3"]
        assert payload["execution_message_id"] == "3"
    finally:
        await ctl.close()


@pytest.mark.parametrize("during_followup", [False, True])
async def test_new_input_survives_running_task_and_ordinary_observer(during_followup):
    entered, release = asyncio.Event(), asyncio.Event()
    turns = []
    async def dispatch(turn):
        turns.append(turn)
        if turn.continuation == during_followup and not entered.is_set():
            entered.set()
            await release.wait()
        ctl.sent(turn.incoming.target, str(1000 + len(turns)), "send_group_msg",
                 {"message": [{"type": "text", "data": {"text": "completed result"}}]})
    ctl = continuing_controller(dispatch=dispatch)
    try:
        await ctl.receive(event(1, parts=[MENTION]))
        if during_followup:
            await ctl.wait_idle()
            await ctl.receive(event(2, text="More details"))
        await asyncio.wait_for(entered.wait(), 1)
        await ctl.receive(event(3, text="Another constraint"))
        await ctl.receive(event(4, user=999, text="Observer chatter"))
        release.set()
        await asyncio.wait_for(ctl.wait_idle(), 2)
        assert turns[-1].incoming.message_id == "3"
        assert "completed result" in turns[-1].context
        assert len(turns) == (3 if during_followup else 2)
    finally:
        release.set()
        await ctl.close()


async def test_immediate_input_before_worker_starts_is_retained():
    ctl = continuing_controller()
    try:
        await ctl.receive(event(1, parts=[MENTION]))
        await ctl.receive(event(2, text="First supplement"))
        await ctl.wait_idle()
        assert ctl.dispatch.await_count == 2
    finally:
        await ctl.close()


async def test_new_arrivals_during_end_decision_are_reassessed():
    ctl = continuing_controller()
    try:
        await invite(ctl)
        async def decide(batch):
            if batch.target_message_id == "2":
                await ctl.receive(event(3, text="Correction: the issue still occurs"))
                return select(batch, "end")
            return select(batch)
        ctl.classifier.decide_followup.side_effect = decide
        await ctl.receive(event(2, text="Solved"))
        await ctl.wait_idle()
        assert ctl.dispatch.await_args.args[0].incoming.message_id == "3"
    finally:
        await ctl.close()


async def test_preexpiry_input_and_admitted_results_survive_long_execution(clock):
    entered, release = asyncio.Event(), asyncio.Event()
    turns = []
    async def dispatch(turn):
        turns.append(turn)
        if not turn.continuation:
            entered.set()
            await release.wait()
        else:
            clock[0] = 1000
            ctl.activate_turn(turn)
            ctl.before_send(Target.parse("group:300"))
    ctl = continuing_controller(dispatch=dispatch)
    try:
        await ctl.receive(event(1, parts=[MENTION]))
        await entered.wait()
        clock[0] = 210
        await ctl.receive(event(2, text="Accepted before expiry"))
        clock[0] = 500
        release.set()
        await ctl.wait_idle()
        assert len(turns) == 2
        await ctl.receive(event(3, text="New input after expiry"))
        await ctl.wait_idle()
        assert len(turns) == 2
    finally:
        release.set()
        await ctl.close()


async def test_listen_retains_contributions_and_unrelated_does_not_renew(clock):
    ctl = continuing_controller()
    try:
        await invite(ctl)
        clock[0] = 210
        ctl.classifier.decide_followup.side_effect = lambda batch: select(batch, "listen")
        await ctl.receive(event(2, text="Checking logs"))
        await ctl.wait_idle()
        clock[0] = 300
        ctl.classifier.decide_followup.side_effect = select
        await ctl.receive(event(3, text="Found the error"))
        await ctl.wait_idle()
        assert json.loads(ctl.dispatch.await_args.args[0].context)["accepted_supplements"][0]["message_id"] == "2"
        ctl.classifier.decide_followup.side_effect = lambda batch: select(batch, "unrelated")
        clock[0] = 410
        await ctl.receive(event(4, text="Lunch"))
        await ctl.wait_idle()
        clock[0] = 421
        await ctl.receive(event(5, text="Too late"))
        await ctl.wait_idle()
        assert ctl.classifier.decide_followup.await_count == 3
    finally:
        await ctl.close()


@pytest.mark.parametrize("invalidation", ["recall", "stop"])
async def test_recall_or_control_closes_invitation(invalidation):
    ctl = continuing_controller(continuation_config(admins=["200"]))
    try:
        await invite(ctl)
        if invalidation == "recall":
            await ctl.receive({"post_type": "notice", "notice_type": "group_recall",
                               "self_id": 100, "group_id": 300, "message_id": 1})
        else:
            await ctl.receive(event(2, parts=[MENTION], text="/stop"))
        await ctl.receive(event(3, text="Continue"))
        await ctl.wait_idle()
        ctl.classifier.decide_followup.assert_not_awaited()
    finally:
        await ctl.close()


@pytest.mark.parametrize("extra", [{"user": 201}, {"user": 100}, {"group": 999}, {"text": "/restart"}])
async def test_unauthorized_echo_and_commands_do_not_drive_followup(extra):
    ctl = continuing_controller()
    try:
        await invite(ctl)
        await ctl.receive(event(2, **{"text": "Continue", **extra}))
        await ctl.wait_idle()
        ctl.classifier.decide_followup.assert_not_awaited()
    finally:
        await ctl.close()


@pytest.mark.parametrize("dry_run", [False, True])
async def test_separate_budgets_and_dry_run(dry_run):
    ctl = continuing_controller(continuation_config(
        continuation={"max_responses_per_hour": 1, "max_decisions_per_hour": 2},
        proactive={"dry_run": dry_run}))
    try:
        await invite(ctl)
        for identifier in range(2, 5):
            await ctl.receive(event(identifier, text="Supplement"))
            await ctl.wait_idle()
        assert ctl.classifier.decide_followup.await_count == 2
        assert ctl.dispatch.await_count == (1 if dry_run else 2)
        assert ctl.stats["dry_run"] == int(dry_run)
    finally:
        await ctl.close()


async def test_failure_retains_input_until_new_arrival_without_replaying_execution():
    ctl = continuing_controller()
    try:
        await invite(ctl)
        ctl.classifier.decide_followup.side_effect = ValueError("bad result")
        await ctl.receive(event(2, text="First"))
        await ctl.wait_idle()
        ctl.classifier.decide_followup.side_effect = select
        ctl.dispatch.side_effect = RuntimeError("uncertain effect")
        await ctl.receive(event(3, text="Second"))
        await ctl.wait_idle()
        assert [row.message_id for row in ctl.dispatch.await_args.args[0].followup.selected] == ["2", "3"]
        await ctl.wait_idle()
        assert ctl.dispatch.await_count == 2
        ctl.dispatch.side_effect = None
        await ctl.receive(event(4, text="Correction"))
        await ctl.wait_idle()
        data = json.loads(ctl.dispatch.await_args.args[0].context)
        assert [row["message_id"] for row in data["pending_messages"]] == ["4"]
        assert "uncertain" in data["last_outcome"]
    finally:
        await ctl.close()


@pytest.mark.parametrize("overrides", [
    {"group_context": {"enabled": False}},
    {"group_context": {"enabled": True, "observe_untriggered": False}},
    {"continuation": {"idle_seconds": 30, "max_duration_seconds": 20}},
])
def test_continuation_requires_valid_live_context(overrides):
    with pytest.raises(ValidationError):
        continuation_config(**overrides)


@pytest.mark.parametrize("classifier", [False, True])
def test_invited_execution_allows_configured_tools(classifier):
    settings = continuation_config(group_toolsets=["terminal"],
                                   proactive={"classifier": {"enabled": classifier, "model": "test"}})
    assert settings.group_toolsets == ("terminal",)


async def test_late_quote_verification_cannot_replace_a_newer_invitation():
    from hermes_napcat.context import GroupMessage

    ctl = continuing_controller()
    ctl._quote = AsyncMock(return_value=GroupMessage('90', '100', 'bot', 1, 'Prior answer', own=True))
    try:
        await ctl.receive(event(1, text='Earlier request', parts=[{'type': 'reply', 'data': {'id': '90'}}]))
        await ctl.receive(event(2, text='Newer task', parts=[MENTION]))
        await ctl.receive(event(3, text='Supplement to the newer task'))
        await ctl.wait_idle()
        data = json.loads(ctl.dispatch.await_args.args[0].context)
        assert data['request']['message_id'] == '2'
        assert data['pending_messages'][0]['message_id'] == '3'
    finally:
        await ctl.close()


async def test_rate_rejected_invitation_cannot_admit_supplements():
    ctl = continuing_controller()
    ctl.policy.rate_allowed = lambda incoming: incoming.message_id != '1'
    try:
        await ctl.receive(event(1, parts=[MENTION]))
        await ctl.receive(event(2, text='Supplement'))
        await ctl.wait_idle()
        ctl.dispatch.assert_not_awaited()
        ctl.classifier.decide_followup.assert_not_awaited()
    finally:
        await ctl.close()


async def test_recalled_contributor_cannot_restore_collaborative_cross_chat_privilege():
    ctl = continuing_controller(continuation_config(allowed_users=['200', '201']))
    try:
        await invite(ctl)
        ctl.classifier.decide_followup.side_effect = lambda batch: select(batch, 'listen')
        await ctl.receive(event(2, user=201, text='Non-admin requirement'))
        await ctl.wait_idle()
        await ctl.receive({'post_type': 'notice', 'notice_type': 'group_recall',
                           'self_id': 100, 'group_id': 300, 'message_id': 2})
        ctl.classifier.decide_followup.side_effect = select
        await ctl.receive(event(3, text='Continue as administrator'))
        await ctl.wait_idle()
        assert not ctl.dispatch.await_args.args[0].followup.cross_chat_allowed
    finally:
        await ctl.close()


async def test_pending_queue_is_bounded_without_overwriting_older_supplements():
    entered, release = asyncio.Event(), asyncio.Event()
    async def dispatch(turn):
        if not turn.continuation:
            entered.set()
            await release.wait()
    ctl = continuing_controller(continuation_config(group_context={
        'enabled': True, 'max_pending_messages': 2}), dispatch=AsyncMock(side_effect=dispatch))
    try:
        await ctl.receive(event(1, parts=[MENTION]))
        await entered.wait()
        for identifier in (2, 3, 4):
            await ctl.receive(event(identifier, text='Supplement'))
        release.set()
        await ctl.wait_idle()
        assert [row.message_id for row in ctl.dispatch.await_args.args[0].followup.selected] == ['2', '3']
        assert ctl.stats['queue_dropped'] == 1
    finally:
        release.set()
        await ctl.close()
