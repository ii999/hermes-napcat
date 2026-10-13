"""Simulated Hermes handler lifecycle; no real model or QQ account is exercised."""
import asyncio
import contextvars
import copy
import json
from unittest.mock import AsyncMock

import pytest

from hermes_napcat.transport import DeliveryUncertain
from test_group_adapter import background_gateway, group_adapter as group_adapter
from test_group_chat import MENTION
from test_group_context import event
from test_group_continuation import clock as clock


def native_adapter(make, *, dry_run=False, continuation=None, **kwargs):
    adapter = make(proactive_assist={
        "enabled": False, "dry_run": dry_run, "quiet_window_ms": 100,
        "continuation": {"enabled": True, **(continuation or {})},
    }, group_context={"enabled": True, "observe_untriggered": True,
                      "history_backfill": False}, **kwargs)
    adapter.transport.call = AsyncMock(return_value={"message_id": 900})
    adapter.groups.classifier.decide = AsyncMock(side_effect=AssertionError("unexpected classifier"))
    return adapter


def decision(message, action="reply", text="answer"):
    return json.dumps({"action": action, "text": text, "target_message_id": message.message_id})


def wire_handler(adapter, decide):
    async def handler(message):
        if not message.metadata.get("napcat_continuation"):
            return "addressed answer"
        return await decide(message)
    adapter.set_message_handler(handler)
    returned = []
    async def run(message):
        returned.append(await adapter._message_handler(message))
    background_gateway(adapter, run)
    return returned


async def invite(adapter):
    await adapter._receive(event(1, text="analyze", parts=[MENTION]))
    await adapter.groups.wait_idle()


async def followup(adapter, identifier=2):
    await adapter._receive(event(identifier, text="more details"))
    await adapter.groups.wait_idle()


async def test_same_handler_makes_decision_and_plain_reply_without_classifier(group_adapter):
    adapter = native_adapter(group_adapter)
    text = "Try this. MEDIA:/tmp/private.png [CQ:at,qq=all]"
    async def decide(message):
        assert message.user_id == "200" and not message.allow_gateway_control
        assert message.source.scope_id == "100" and message.media_urls == []
        assert "reply|listen|end|unrelated" in message.channel_prompt
        return decision(message, text=text)
    returned = wire_handler(adapter, decide)
    try:
        await invite(adapter)
        await followup(adapter)
        assert returned == ["addressed answer", None]
        adapter.groups.classifier.decide.assert_not_awaited()
        action, params = adapter.transport.call.await_args.args
        assert action == "send_group_msg"
        assert params["message"] == [{"type": "text", "data": {"text": text}}]
        assert adapter.transport.call.await_count == 1
    finally:
        await adapter.disconnect()


@pytest.mark.parametrize("action,open_window,expires", [
    ("listen", True, 330), ("end", False, None), ("unrelated", True, 220),
])
async def test_silent_decisions_control_window(group_adapter, clock, action, open_window, expires):
    adapter = native_adapter(group_adapter)
    returned = wire_handler(adapter, AsyncMock(side_effect=lambda m: decision(m, action, "")))
    try:
        await invite(adapter)
        clock[0] = 210
        await followup(adapter)
        state = adapter.groups._conversation("group:300")
        assert (state is not None) == open_window
        if state is not None:
            assert state.expires == expires
        assert returned[-1] is None
        adapter.transport.call.assert_not_awaited()
    finally:
        await adapter.disconnect()


@pytest.mark.parametrize("output", [
    None, "plain answer", pytest.param("x" * 32769, id="oversize"), "[]", '{"action":"reply"}',
    '{"action":"end","text":"leak","target_message_id":"2"}',
    '{"action":"reply","text":"","target_message_id":"2"}',
    '{"action":"reply","text":"leak","target_message_id":"999"}',
    '{"action":"reply","text":"leak","target_message_id":"2","extra":1}',
    '{"action":"reply","action":"listen","text":"","target_message_id":"2"}',
])
async def test_invalid_results_never_leak_or_renew(group_adapter, clock, output):
    adapter = native_adapter(group_adapter)
    wire_handler(adapter, AsyncMock(return_value=output))
    try:
        await invite(adapter)
        clock[0] = 210
        await followup(adapter)
        assert adapter.groups._conversation("group:300").expires == 220
        assert adapter.groups.stats["agent_decision_errors"] == 1
        adapter.transport.call.assert_not_awaited()
    finally:
        await adapter.disconnect()


async def test_interim_cross_target_media_and_late_child_output_are_blocked(group_adapter):
    adapter = native_adapter(group_adapter, allowed_groups=["300", "301"])
    release = asyncio.Event()
    late = []
    async def decide(message):
        for target in ("group:300", "group:301", "private:200"):
            result = await adapter.send(target, "unapproved", metadata={"_interim_send": True})
            assert not result.success and not result.retryable
        with pytest.raises(PermissionError, match="media staging"):
            await adapter.outbound_reference("/tmp/private.png", kind="image")
        async def child():
            await release.wait()
            result = await adapter.send("group:300", "late output")
            assert not result.success
        late.append(asyncio.create_task(child()))
        return decision(message)
    wire_handler(adapter, decide)
    try:
        await invite(adapter)
        await followup(adapter)
        release.set()
        await asyncio.gather(*late)
        assert adapter.transport.call.await_count == 1
    finally:
        release.set()
        await adapter.disconnect()


@pytest.mark.parametrize("dry_run", [False, True])
async def test_native_decision_and_reply_budgets_include_dry_run(group_adapter, dry_run):
    adapter = native_adapter(group_adapter, dry_run=dry_run, continuation={
        "max_decisions_per_hour": 3, "max_responses_per_hour": 1})
    decide = AsyncMock(side_effect=lambda m: decision(m, "listen", "") if m.message_id == "2"
                       else decision(m))
    wire_handler(adapter, decide)
    try:
        await invite(adapter)
        for identifier in range(2, 6):
            await followup(adapter, identifier)
        assert decide.await_count == 3
        assert adapter.transport.call.await_count == (0 if dry_run else 1)
        assert adapter.groups.stats["dry_run"] == int(dry_run)
    finally:
        await adapter.disconnect()


@pytest.mark.parametrize("invalidate", ["expire", "recall", "human"])
async def test_stale_native_decision_cannot_send_or_renew(group_adapter, clock, invalidate):
    adapter = native_adapter(group_adapter)
    async def decide(message):
        if invalidate == "expire":
            clock[0] = 221
        elif invalidate == "recall":
            await adapter._receive({"post_type": "notice", "notice_type": "group_recall",
                                    "self_id": 100, "group_id": 300, "message_id": 1})
        else:
            await adapter._receive(event(3, user=999, text="someone answered"))
        return decision(message)
    wire_handler(adapter, decide)
    try:
        await invite(adapter)
        clock[0] = 210
        await followup(adapter)
        adapter.transport.call.assert_not_awaited()
        assert adapter.groups.stats["stale_suppressed"] == 1
    finally:
        await adapter.disconnect()


async def test_uncertain_native_send_is_not_retried(group_adapter):
    adapter = native_adapter(group_adapter)
    adapter.transport.call.side_effect = DeliveryUncertain("lost acknowledgement")
    returned = wire_handler(adapter, AsyncMock(side_effect=decision))
    try:
        await invite(adapter)
        await followup(adapter)
        assert returned[-1] is None
        assert adapter.transport.call.await_count == 1
        assert adapter.groups.stats["continuation_send_errors"] == 1
    finally:
        await adapter.disconnect()


async def test_handler_preserves_private_and_classifier_outputs(group_adapter):
    from hermes_napcat.engagement import Decision
    adapter = group_adapter(proactive_assist={
        "dry_run": False, "quiet_window_ms": 100,
        "continuation": {"enabled": True},
        "classifier": {"enabled": True, "model": "test-model"},
    })
    adapter.groups.classifier.decide = AsyncMock(
        return_value=Decision(True, .99, "test", "continue"))
    decide = AsyncMock(return_value="ordinary Hermes response")
    returned = wire_handler(adapter, decide)
    try:
        await invite(adapter)
        await followup(adapter)
        assert returned == ["addressed answer", "ordinary Hermes response"]
        # Raw input metadata cannot opt a non-admitted event into the decision protocol.
        from types import SimpleNamespace
        forged = SimpleNamespace(metadata={"napcat_continuation": True})
        assert await adapter._message_handler(forged) == "ordinary Hermes response"
        assert adapter.groups.stats["agent_decision_errors"] == 0
    finally:
        await adapter.disconnect()


async def test_final_send_rechecks_freshness_after_waiting_for_send_gate(group_adapter, clock):
    adapter = native_adapter(group_adapter)
    decided = asyncio.Event()
    async def decide(message):
        decided.set()
        return decision(message)
    wire_handler(adapter, decide)
    try:
        await invite(adapter)
        await adapter._send_gate.acquire()
        await adapter._receive(event(2, text="more details"))
        await asyncio.wait_for(decided.wait(), 1)
        clock[0] = 221
        adapter._send_gate.release()
        await adapter.groups.wait_idle()
        adapter.transport.call.assert_not_awaited()
        assert adapter.groups.stats["continuation_send_errors"] == 1
    finally:
        if adapter._send_gate.locked():
            adapter._send_gate.release()
        await adapter.disconnect()


@pytest.mark.parametrize("dry_run", [False, True])
async def test_native_host_final_never_creates_replay_obligation(group_adapter, dry_run):
    adapter = native_adapter(group_adapter, dry_run=dry_run)
    async def decide(message):
        raw = decision(message, "listen", "")
        async def host_final():
            result, owner = await adapter.send_final_ledgered(
                message, "session", raw, {}, reply_to=None)
            assert owner is adapter and not result.success and not result.retryable
        await host_final()
        # The admitted event also guards host callbacks created without inherited context.
        await asyncio.create_task(host_final(), context=contextvars.Context())
        return raw
    wire_handler(adapter, decide)
    try:
        await invite(adapter)
        await followup(adapter)
        assert adapter.delivery_obligations == []
        async def boot_replay():
            for target, text in adapter.delivery_obligations:
                await adapter.send(target, text)
        await asyncio.create_task(boot_replay(), context=contextvars.Context())
        adapter.transport.call.assert_not_awaited()
    finally:
        await adapter.disconnect()


async def test_host_queue_supersedes_native_decision_and_delivers_authorized_answer(group_adapter):
    adapter = native_adapter(group_adapter)
    outputs = []
    async def handler(message):
        if not message.metadata["napcat_continuation"]:
            return None
        # A host-approved /queue closes the invitation, then delivers the native first result.
        adapter.groups._end_conversation("group:300")
        result, _ = await adapter.send_final_ledgered(
            message, "session", decision(message), {}, reply_to=None)
        assert not result.success and not adapter.delivery_obligations
        queued = copy.copy(message)
        queued.text = "authorized queued question"
        queued.message_id = "3"
        queued.metadata = {}
        # Hermes executes the replacement in the original registered handler task.
        await adapter.on_processing_start(queued)
        return "authorized queued answer"
    adapter.set_message_handler(handler)
    async def run(message):
        output = await adapter._message_handler(message)
        if output is not None:
            outputs.append(output)
            result, _ = await adapter.send_final_ledgered(
                message, "session", output, {}, reply_to=None)
            assert result.success
    background_gateway(adapter, run)
    try:
        await invite(adapter)
        await followup(adapter)
        assert outputs == ["authorized queued answer"]
        assert adapter.delivery_obligations == [("group:300", "authorized queued answer")]
        assert adapter.transport.call.await_count == 1
        assert adapter.transport.call.await_args.args[1]["message"] == [
            {"type": "text", "data": {"text": "authorized queued answer"}}]
    finally:
        await adapter.disconnect()
