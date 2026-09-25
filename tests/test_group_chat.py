import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from hermes_napcat.engagement import Decision, WindowBudget
from hermes_napcat.group_chat import GroupChatController
from hermes_napcat.policy import Policy
from hermes_napcat.protocol import Incoming, Target
from test_group_context import config, event


MENTION = {"type": "at", "data": {"qq": "100"}}


def controller(settings=None, *, call=None, dispatch=None, state=None):
    settings = settings or config()
    return GroupChatController(settings, Policy(settings),
                               call or AsyncMock(return_value={"messages": []}),
                               dispatch or AsyncMock(), transport_state=state or (lambda: (1, 0)))


async def test_observe_all_members_then_inject_on_mention_without_changing_principal():
    ctl = controller()
    await ctl.receive(event(1, user=201, text="CUDA 13 在这台机器上报错"))
    assert ctl.dispatch.await_count == 0
    await ctl.receive(event(2, text="你怎么看？", parts=[MENTION]))
    await ctl.wait_idle()
    turn = ctl.dispatch.await_args.args[0]
    assert turn.incoming.user_id == "200" and turn.text == "你怎么看？"
    rows = [json.loads(line) for line in turn.context.splitlines()][1:]
    assert rows[0]["sender"]["user_id"] == "201" and "CUDA 13" in rows[0]["text"]
    assert not turn.proactive
    await ctl.close()


async def test_unauthorized_mention_never_dispatches_or_calls_history():
    ctl = controller()
    await ctl.receive(event(1, user=201, text="/restart", parts=[MENTION]))
    await ctl.wait_idle()
    assert ctl.dispatch.await_count == ctl.call.await_count == 0
    await ctl.receive(event(2, group=999, parts=[MENTION]))
    assert not ctl.context.records("group:999")
    await ctl.close()


async def test_plain_chatter_does_not_spend_agent_rate_and_duplicate_events_do_not_replay():
    ctl = controller(config(messages_per_minute=1))
    for i in range(20):
        await ctl.receive(event(i + 1, text="聊天"))
    trigger = event(100, text="请回答", parts=[MENTION])
    await ctl.receive(trigger)
    await ctl.receive(trigger)
    await ctl.wait_idle()
    assert ctl.dispatch.await_count == 1
    await ctl.receive(event(101, parts=[MENTION]))
    await ctl.wait_idle()
    assert ctl.dispatch.await_count == 1
    await ctl.close()


async def test_history_failure_preserves_current_message_and_live_context():
    ctl = controller(call=AsyncMock(side_effect=TimeoutError))
    await ctl.receive(event(1, user=201, text="known context"))
    await ctl.receive(event(2, text="current", parts=[MENTION]))
    await ctl.wait_idle()
    turn = ctl.dispatch.await_args.args[0]
    assert turn.text == "current" and "known context" in turn.context
    assert "unavailable" in turn.context and ctl.stats["history_errors"] == 1
    await ctl.close()


async def test_backfill_filters_other_group_and_deduplicates_live():
    now = time.time()
    history = [event(1, text="history", stamp=now - 4),
               event(2, text="duplicate", stamp=now - 3),
               event(3, group=999, text="foreign", stamp=now - 2)]
    ctl = controller(call=AsyncMock(return_value={"messages": history}))
    await ctl.receive(history[1])
    await ctl.receive(event(4, text="current", parts=[MENTION]))
    await ctl.wait_idle()
    turn = ctl.dispatch.await_args.args[0]
    assert "history" in turn.context and "foreign" not in turn.context
    assert turn.context.count('"message_id":"2"') == 1
    action, params = ctl.call.await_args.args
    assert action == "get_group_msg_history" and params["message_seq"] == "4"
    assert params["disable_get_url"] and not params["parse_mult_msg"]
    await ctl.close()


async def test_verified_quote_is_injected_and_cross_group_quote_cannot_trigger():
    async def call(action, params):
        if action == "get_msg":
            return event(params["message_id"], user=100, text="prior answer", stamp=time.time() - 50,
                         group=999 if params["message_id"] == 99 else 300)
        return {"messages": []}
    ctl = controller(call=AsyncMock(side_effect=call))
    def reply(mid):
        return {"type": "reply", "data": {"id": str(mid)}}
    await ctl.receive(event(1, text="继续", parts=[reply(80)]))
    await ctl.receive(event(2, text="cross-group", parts=[reply(99)]))
    await ctl.wait_idle()
    assert ctl.dispatch.await_count == 1
    turn = ctl.dispatch.await_args.args[0]
    assert turn.own_reply and turn.quote.message_id == "80" and "prior answer" in turn.context
    await ctl.close()


async def test_slow_turn_does_not_block_observation_or_other_groups_and_commands_bypass_queue():
    entered, release = asyncio.Event(), asyncio.Event()
    delivered = []
    async def dispatch(turn):
        delivered.append(turn.text)
        if turn.text == "slow":
            entered.set()
            await release.wait()
    ctl = controller(config(allowed_groups=["300", "301"]), dispatch=dispatch)
    await ctl.receive(event(1, text="slow", parts=[MENTION]))
    await asyncio.wait_for(entered.wait(), 1)
    await ctl.receive(event(2, user=201, text="still observing"))
    await ctl.receive(event(3, group=301, text="other group", parts=[MENTION]))
    await ctl.receive(event(4, text="/stop", parts=[MENTION]))
    await asyncio.sleep(0.01)
    assert ctl.context.lookup("group:300", "2") is not None
    assert "other group" in delivered and "/stop" in delivered
    release.set()
    await ctl.wait_idle()
    await ctl.close()


async def test_reconnect_and_drop_state_request_bounded_backfill_again():
    state = [1, 0]
    ctl = controller(state=lambda: tuple(state))
    target = Target.parse("group:300")
    await ctl.ensure_history(target)
    await ctl.ensure_history(target)
    assert ctl.call.await_count == 1
    state[:] = [2, 1]
    ctl._fetch_state[target.address] = ((1, 0), time.monotonic() - 31)
    await ctl.ensure_history(target)
    assert ctl.call.await_count == 2 and ctl.context.room(target.address).gap
    await ctl.close()


async def test_recent_tool_limits_authorization_and_scoped_pagination():
    ctl = controller()
    await ctl.receive(event(1))
    await ctl.receive(event(2))
    result = await ctl.recent_messages(Target.parse("group:300"), limit=1, before="2")
    assert result["messages"][0]["message_id"] == "1"
    for target in ["group:999", "private:200"]:
        with pytest.raises(PermissionError):
            await ctl.recent_messages(Target.parse(target))
    with pytest.raises(ValueError):
        await ctl.recent_messages(Target.parse("group:300"), limit=500)
    with pytest.raises(ValueError):
        await ctl.recent_messages(Target.parse("group:300"), before="999")
    await ctl.close()


async def test_recall_notice_prevents_later_use_and_replayed_trigger():
    ctl = controller()
    raw = event(1, text="withdrawn", parts=[MENTION])
    await ctl.receive({"post_type": "notice", "notice_type": "group_recall",
                       "self_id": 100, "group_id": 300, "message_id": 1})
    await ctl.receive(raw)
    assert ctl.dispatch.await_count == 0
    ctl.context.put(Incoming.parse(raw), history=True)
    assert ctl.context.lookup("group:300", "1") is None
    await ctl.close()


def proactive_config(**overrides):
    proactive = {"enabled": True, "quiet_window_ms": 100, "cooldown_seconds": 1}
    proactive.update(overrides)
    return config(proactive_assist=proactive)


async def test_proactive_burst_dry_run_has_no_agent_or_send_and_other_speaker_suppresses():
    ctl = controller(proactive_config())
    await ctl.receive(event(1, text="有人知道"))
    await ctl.receive(event(2, text="这个 CUDA 报错"))
    await ctl.receive(event(3, text="应该怎么解决吗？"))
    await ctl.wait_idle()
    assert ctl.stats["dry_run"] == 1 and ctl.dispatch.await_count == 0
    await ctl.close()
    ctl = controller(proactive_config())
    await ctl.receive(event(1, text="有人知道怎么解决吗？"))
    await ctl.receive(event(2, user=201, text="我知道，已经解决了"))
    await ctl.wait_idle()
    assert ctl.stats["dry_run"] == 0 and ctl.dispatch.await_count == 0
    await ctl.close()


async def test_proactive_ignores_other_mentions_bots_and_unauthorized_speakers():
    ctl = controller(proactive_config(ignored_users=["200"]))
    await ctl.receive(event(1, text="求助"))
    await ctl.wait_idle()
    assert ctl.stats["decisions"] == 0
    await ctl.close()
    ctl = controller(proactive_config())
    await ctl.receive(event(1, text="有人知道吗？", parts=[{"type": "at", "data": {"qq": "201"}}]))
    await ctl.wait_idle()
    await ctl.receive(event(2, user=201, text="请问有人能帮忙吗？"))
    await ctl.wait_idle()
    assert ctl.stats["decisions"] == 0 and ctl.dispatch.await_count == 0
    await ctl.close()


async def test_live_proactive_runs_in_authenticated_scope_and_send_rechecks_freshness():
    entered, release = asyncio.Event(), asyncio.Event()
    outcomes = []
    async def dispatch(turn):
        outcomes.append((turn.incoming.user_id, turn.proactive))
        entered.set()
        await release.wait()
        with pytest.raises(PermissionError):
            ctl.before_send(turn.incoming.target)
    ctl = controller(proactive_config(dry_run=False), dispatch=dispatch)
    await ctl.receive(event(1, text="有人知道这个问题怎么解决吗？"))
    await asyncio.wait_for(entered.wait(), 1)
    await ctl.receive(event(2, user=201, text="已经解决了，不用回复"))
    release.set()
    await ctl.wait_idle()
    assert outcomes == [("200", True)] and ctl.stats["stale_suppressed"] == 1
    await ctl.close()


async def test_classifier_failure_is_silent_without_fallback():
    ctl = controller(proactive_config())
    ctl.classifier.decide = AsyncMock(side_effect=ValueError("bad classifier"))
    await ctl.receive(event(1, text="有人知道吗？"))
    await ctl.wait_idle()
    assert ctl.stats["classifier_errors"] == 1 and ctl.dispatch.await_count == 0
    await ctl.close()


async def test_classifier_low_confidence_does_not_dispatch():
    ctl = controller(proactive_config(dry_run=False))
    ctl.classifier.decide = AsyncMock(return_value=Decision(True, 0.5, "classifier"))
    await ctl.receive(event(1, text="有人知道吗？"))
    await ctl.wait_idle()
    assert ctl.dispatch.await_count == 0
    await ctl.close()


async def test_cooldown_hourly_budget_and_shutdown():
    budget = WindowBudget(1, 3600, 1)
    assert budget.take("group:300", now=0)
    assert not budget.take("group:300", now=10)
    assert budget.take("group:300", now=3600)
    ctl = controller(proactive_config())
    await ctl.receive(event(1, text="求助"))
    await ctl.wait_idle()
    await ctl.receive(event(2, text="求助"))
    await ctl.wait_idle()
    assert ctl.stats["dry_run"] == 1
    await ctl.close()
    assert not ctl._timers and not ctl._workers and not ctl.context.rooms
