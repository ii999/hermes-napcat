"""Simulated Hermes admission/handler boundaries; no real model or QQ account."""
import copy
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_napcat.continuation import FollowupDecision
from hermes_napcat.transport import DeliveryUncertain
from test_group_adapter import background_gateway, group_adapter as group_adapter
from test_group_chat import MENTION
from test_group_context import event


def native_adapter(make, monkeypatch, *, dry_run=False, **kwargs):
    adapter = make(proactive_assist={
        "enabled": False, "dry_run": dry_run, "quiet_window_ms": 100,
        "continuation": {"enabled": True},
    }, group_context={"enabled": True, "observe_untriggered": True,
                      "history_backfill": False}, **kwargs)
    clarify = SimpleNamespace(get_pending_for_session=lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, 'tools.clarify_gateway', clarify)
    adapter._event_session_key = lambda message: message.source.user_id
    adapter.transport.call = AsyncMock(return_value={"message_id": 900})
    async def decide(source, prompt, **_):
        payload = json.loads(prompt.split("\n", 1)[1])
        return json.dumps({"action": "execute", "confidence": .99,
                           "target_message_id": payload["target_message_id"],
                           "message_ids": [row["message_id"] for row in payload["pending_messages"]]})
    admission = AsyncMock(side_effect=decide)
    monkeypatch.setattr(sys.modules['hermes_napcat.group_adapter'], 'judge_participation', admission)
    return adapter, admission


async def invite(adapter):
    await adapter._receive(event(1, text="Analyze", parts=[MENTION]))
    await adapter.groups.wait_idle()


async def followup(adapter, identifier=2):
    await adapter._receive(event(identifier, text="More details"))
    await adapter.groups.wait_idle()


@pytest.mark.parametrize("dry_run", [False, True])
async def test_native_admission_is_separate_and_execution_returns_normal_output(group_adapter, monkeypatch, dry_run):
    adapter, admission = native_adapter(group_adapter, monkeypatch, dry_run=dry_run,
                                        group_toolsets=["web", "qq"])
    outputs, principals = [], []
    async def handler(message):
        principals.append(message.source.user_id)
        if message.metadata["napcat_continuation"]:
            assert not message.allow_gateway_control
            assert message.media_urls == [] and message.source.scope_id == "100"
            # Ordinary tool-facing output and final delivery remain usable after admission.
            assert (await adapter.send('group:300', 'tool progress')).success
            return "A normal Hermes answer"
        return "Initial answer"
    adapter.set_message_handler(handler)
    async def run(message):
        output = await adapter._message_handler(message)
        outputs.append(output)
        if message.metadata['napcat_continuation']:
            result, _ = await adapter.send_final_ledgered(message, 'session', output, {}, reply_to=None)
            assert result.success
    background_gateway(adapter, run)
    try:
        await invite(adapter)
        await followup(adapter)
        assert admission.await_count == 1
        assert outputs == (["Initial answer"] if dry_run else ["Initial answer", "A normal Hermes answer"])
        assert principals == (["200"] if dry_run else ["200", "200"])
        assert adapter.transport.call.await_count == (0 if dry_run else 2)
    finally:
        await adapter.disconnect()


@pytest.mark.parametrize("output", [
    None, "plain answer", pytest.param("x" * 32769, id="oversize"), "[]",
    '{"action":"execute"}',
    '{"action":"execute","confidence":1,"target_message_id":"999","message_ids":["2"]}',
    '{"action":"execute","confidence":1,"target_message_id":"2","message_ids":["1"]}',
    '{"action":"execute","confidence":1,"target_message_id":"2","message_ids":["2","2"]}',
    '{"action":"execute","confidence":true,"target_message_id":"2","message_ids":["2"]}',
    '{"action":"unrelated","confidence":1,"target_message_id":"2","message_ids":["2"]}',
    '{"action":"execute","action":"end","confidence":1,"target_message_id":"2","message_ids":["2"]}',
])
async def test_invalid_admission_never_invokes_handler_or_sends(group_adapter, monkeypatch, output):
    adapter, admission = native_adapter(group_adapter, monkeypatch)
    admission.side_effect = None
    admission.return_value = output
    try:
        await invite(adapter)
        adapter.handle_message.reset_mock()
        await followup(adapter)
        adapter.handle_message.assert_not_awaited()
        adapter.transport.call.assert_not_awaited()
        assert adapter.groups.stats["agent_decision_errors"] == 1
    finally:
        await adapter.disconnect()


async def test_contributor_cap_follows_processing_context_and_resets_for_host_queue(group_adapter, monkeypatch):
    from hermes_napcat.tools import ToolSession, _authorized_target
    from hermes_napcat.protocol import Target

    adapter, _ = native_adapter(group_adapter, monkeypatch, allowed_users=['200', '201'],
                                admins=['200'], allowed_groups=['300', '301'],
                                qq_tools={'enabled': True, 'allow_cross_chat': True})
    checked = []
    # Relevant non-admin input is retained even if the latest contributor is an administrator.
    async def run(message):
        if not message.metadata['napcat_continuation']:
            return
        assert message.user_id == '200'
        session = ToolSession(Target.parse('group:300'), '200', '', 'session', message.message_id)
        assert _authorized_target(adapter, session, 'group:300').id == '300'
        with pytest.raises(PermissionError, match='contributor'):
            _authorized_target(adapter, session, 'group:301')
        queued = copy.copy(message)
        queued.message_id = '5'
        queued.metadata = {}
        await adapter.on_processing_start(queued)
        assert _authorized_target(adapter, session, 'group:301').id == '301'
        checked.append(True)
    background_gateway(adapter, run)
    try:
        await invite(adapter)
        await adapter._receive(event(2, user=201, text='Non-admin constraint'))
        await adapter._receive(event(3, user=200, text='Apply that constraint'))
        await adapter.groups.wait_idle()
        assert checked == [True]
    finally:
        await adapter.disconnect()


async def test_uncertain_followup_delivery_is_not_replayed(group_adapter, monkeypatch):
    adapter, _ = native_adapter(group_adapter, monkeypatch)
    adapter.transport.call.side_effect = DeliveryUncertain('lost acknowledgement')
    async def run(message):
        if message.metadata['napcat_continuation']:
            result = await adapter.send('group:300', 'answer')
            assert not result.success and not result.retryable
    background_gateway(adapter, run)
    try:
        await invite(adapter)
        await followup(adapter)
        await adapter.groups.wait_idle()
        assert adapter.transport.call.await_count == 1
    finally:
        await adapter.disconnect()


async def test_selected_message_recall_during_admission_prevents_execution(group_adapter, monkeypatch):
    adapter, admission = native_adapter(group_adapter, monkeypatch)
    original = admission.side_effect
    async def decide(source, prompt, **kwargs):
        await adapter._receive({'post_type': 'notice', 'notice_type': 'group_recall',
                                'self_id': 100, 'group_id': 300, 'message_id': 2})
        return await original(source, prompt, **kwargs)
    admission.side_effect = decide
    try:
        await invite(adapter)
        adapter.handle_message.reset_mock()
        await followup(adapter)
        adapter.handle_message.assert_not_awaited()
    finally:
        await adapter.disconnect()


async def test_decision_rejects_reordered_ids(group_adapter, monkeypatch):
    adapter, _ = native_adapter(group_adapter, monkeypatch)
    async def decide(batch, incoming):
        return FollowupDecision('execute', ('3', '2'))
    adapter._decide_followup = decide
    try:
        await invite(adapter)
        adapter.handle_message.reset_mock()
        await adapter._receive(event(2, text='First'))
        await adapter._receive(event(3, text='Second'))
        await adapter.groups.wait_idle()
        adapter.handle_message.assert_not_awaited()
    finally:
        await adapter.disconnect()
