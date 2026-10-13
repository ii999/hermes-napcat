"""Actual plugin wiring with the repository's Hermes doubles, not a real Gateway run."""
import asyncio
import contextvars
import importlib
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_napcat.protocol import Target
from hermes_napcat.transport import DeliveryUncertain
from test_group_context import config, event

MENTION = {'type': 'at', 'data': {'qq': '100'}}


@pytest.fixture
def group_adapter(hermes_doubles):
    sys.modules.pop('hermes_napcat.group_adapter', None)
    module = importlib.import_module('hermes_napcat.group_adapter')
    def make(**kwargs):
        settings = config(**kwargs)
        instance = module.GroupNapCatAdapter(
            hermes_doubles.PlatformConfig(extra=settings.model_dump()))
        instance.transport.call = AsyncMock(return_value={'messages': []})
        return instance
    yield make
    sys.modules.pop('hermes_napcat.group_adapter', None)


async def test_group_boundary_injects_context_without_rewriting_trigger_or_principal(group_adapter):
    adapter = group_adapter()
    adapter._attachments = AsyncMock(return_value=([], [], []))
    await adapter._receive(event(1, user=201, text='prior group discussion'))
    adapter._attachments.assert_not_called()
    await adapter._receive(event(2, text='your view?', parts=[MENTION]))
    await adapter.groups.wait_idle()
    delivered = adapter.handle_message.await_args.args[0]
    assert delivered.text == 'your view?' and delivered.user_id == '200'
    assert delivered.source.user_id == '200' and delivered.source.scope_id == '100'
    assert 'prior group discussion' in delivered.channel_context
    assert not delivered.metadata['napcat_proactive']
    await adapter.disconnect()


async def test_private_and_disabled_group_behavior_still_use_base_adapter(group_adapter):
    adapter = group_adapter(group_context={'enabled': False})
    raw = event(1, text='private', message_type='private')
    await adapter._receive(raw)
    assert adapter.handle_message.await_args.args[0].source.chat_id == 'private:200'
    adapter.handle_message.reset_mock()
    await adapter._receive(event(2, text='plain unmentioned'))
    adapter.handle_message.assert_not_called()
    await adapter._receive(event(3, text='explicit', parts=[MENTION]))
    assert adapter.handle_message.await_args.args[0].text == 'explicit'
    await adapter.disconnect()


async def test_group_file_notice_is_lazy_and_cannot_anchor_history(group_adapter):
    adapter = group_adapter(media={"references": {"enabled": True}})
    adapter._attachments = AsyncMock()
    notice = {"post_type": "notice", "notice_type": "group_upload", "self_id": 100,
              "group_id": 300, "user_id": 200, "time": time.time(),
              "file": {"id": "opaque-file", "name": "report.pdf", "size": 100}}
    await adapter._receive(notice)
    await adapter._receive(notice)
    rows = adapter.groups.context.records("group:300", limit=10)
    assert len(rows) == 1
    refs = adapter.media_refs.for_message(Target.parse("group:300"), rows[0].message_id)
    assert len(refs) == 1 and refs[0].notice_only
    adapter._attachments.assert_not_called()
    adapter.handle_message.assert_not_called()
    with pytest.raises(ValueError, match="file notices"):
        await adapter.groups.recent_messages(Target.parse("group:300"), before=rows[0].message_id)
    adapter.transport.call.assert_not_called()
    await adapter.disconnect()


async def test_successful_outbound_is_observed_but_uncertain_delivery_is_not_retried(group_adapter):
    adapter = group_adapter()
    adapter.transport.call.side_effect = [{'message_id': 77}, DeliveryUncertain('ack lost')]
    result = await adapter.send('group:300', 'assistant response')
    assert result.success and adapter.groups.context.lookup('group:300', '77').own
    result = await adapter.send('group:300', 'uncertain')
    assert not result.success and result.raw_response['delivery_uncertain']
    assert not result.retryable and adapter.transport.call.await_count == 2
    await adapter._receive(event(77, user=100, text='assistant response'))
    adapter.handle_message.assert_not_called()
    await adapter.disconnect()


async def test_recall_during_current_media_download_cancels_dispatch(group_adapter):
    adapter = group_adapter()
    async def attachments(incoming):
        await adapter.groups.receive({'post_type': 'notice', 'notice_type': 'group_recall',
                                      'self_id': 100, 'group_id': 300, 'message_id': 2})
        return [], [], []
    adapter._attachments = attachments
    await adapter._receive(event(2, text='withdraw', parts=[MENTION]))
    await adapter.groups.wait_idle()
    adapter.handle_message.assert_not_called()
    await adapter.disconnect()


async def test_recent_tool_reuses_profile_identity_and_target_acl(group_adapter, monkeypatch):
    from hermes_napcat import group_tools, tools
    adapter = group_adapter(qq_tools={'enabled': True})
    await adapter._receive(event(1, user=201, text='public in this group'))
    session = tools.ToolSession(Target.parse('group:300'), '200', 'profile-a', 'session-a', '2')
    runner = SimpleNamespace(_gateway_loop=asyncio.get_running_loop())
    monkeypatch.setattr(tools, '_current_session', lambda _session_id='': session)
    def resolve(profile):
        assert profile == 'profile-a'
        return runner, adapter
    monkeypatch.setattr(tools, '_live_adapter', resolve)
    result = json.loads(await group_tools.qq_get_recent_messages({'limit': 10}))
    assert result['success'] and result['messages'][0]['sender']['user_id'] == '201'
    denied = json.loads(await group_tools.qq_get_recent_messages({'target': 'group:999'}))
    assert not denied['success']
    await adapter.disconnect()


def test_plugin_registers_group_adapter_and_deferred_recent_tool(hermes_doubles, group_adapter):
    from hermes_napcat.plugin import register
    platform_calls, tool_calls = [], []
    ctx = SimpleNamespace(register_platform=lambda **kw: platform_calls.append(kw),
                          register_tool=lambda **kw: tool_calls.append(kw))
    register(ctx)
    assert any(row['name'] == 'qq_get_recent_messages' for row in tool_calls)
    assert platform_calls[0]['adapter_factory'] is type(group_adapter())
    assert platform_calls[0]['allowed_users_env'] == 'NAPCAT_ALLOWED_USERS'


def background_gateway(adapter, run, *, context=None):
    """Simulate Hermes admission and lifecycle hooks, including a separate task context."""
    async def admit(event):
        event._gateway_accepted = True

        async def process():
            await adapter.on_processing_start(event)
            await run(event)

        asyncio.create_task(process(), context=context() if context else contextvars.Context())

    adapter.handle_message.side_effect = admit


async def test_background_turns_hold_group_order_and_model_slots_but_allow_controls(group_adapter):
    adapter = group_adapter(event_workers=1, allowed_groups=['300', '301'])
    entered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    delivered = []

    async def run(event):
        delivered.append(event.text)
        if event.text == 'slow':
            entered.set()
            await release.wait()
        if event.text == '/stop':
            stopped.set()

    background_gateway(adapter, run)
    await adapter._receive(event(1, text='slow', parts=[MENTION]))
    await asyncio.wait_for(entered.wait(), 1)
    await adapter._receive(event(2, text='next', parts=[MENTION]))
    await adapter._receive(event(3, group=301, text='other', parts=[MENTION]))
    await adapter._receive(event(4, user=201, text='observation'))
    await adapter._receive(event(5, text='/stop', parts=[MENTION]))
    await adapter._receive(event(6, text='/unknown', parts=[MENTION]))
    await asyncio.wait_for(stopped.wait(), 1)
    assert delivered == ['slow', '/stop']
    assert adapter.groups.context.lookup('group:300', '4').text == 'observation'
    release.set()
    await asyncio.wait_for(adapter.groups.wait_idle(), 1)
    assert sorted(delivered) == ['/stop', '/unknown', 'next', 'other', 'slow']
    await adapter.disconnect()


async def test_background_tasks_bind_their_own_proactive_ticket(group_adapter):
    adapter = group_adapter(proactive_assist={
        'enabled': True, 'dry_run': False, 'quiet_window_ms': 100,
    })
    entered, release = asyncio.Event(), asyncio.Event()
    inherited = [contextvars.Context()]
    delivered = []

    async def run(message):
        target = Target.parse(message.source.chat_id)
        if message.metadata['napcat_proactive']:
            inherited[0] = contextvars.copy_context()
            entered.set()
            await release.wait()
            with pytest.raises(PermissionError):
                adapter.groups.before_send(target)
            delivered.append('stale suppressed')
        else:
            adapter.groups.before_send(target)
            delivered.append(message.text)

    background_gateway(adapter, run, context=lambda: inherited[0].copy())
    await adapter._receive(event(1, text='有人知道怎么解决吗？'))
    await asyncio.wait_for(entered.wait(), 1)
    await adapter._receive(event(2, text='explicit reply', parts=[MENTION]))
    release.set()
    await asyncio.wait_for(adapter.groups.wait_idle(), 1)
    assert delivered == ['stale suppressed', 'explicit reply']
    await adapter.disconnect()


async def test_clarification_reply_bypasses_the_waiting_group_turn(group_adapter, monkeypatch):
    adapter = group_adapter(group_toolsets=['clarify'])
    entered, answered = asyncio.Event(), asyncio.Event()
    pending = []
    clarify = SimpleNamespace(get_pending_for_session=lambda key, **kw: pending or None)
    monkeypatch.setitem(sys.modules, 'tools', SimpleNamespace(clarify_gateway=clarify))
    monkeypatch.setitem(sys.modules, 'tools.clarify_gateway', clarify)
    adapter._event_session_key = lambda message: message.source.user_id

    async def run(message):
        if message.text == 'question':
            pending.append(message)
            entered.set()
            await answered.wait()
        elif message.text == '2':
            pending.clear()
            answered.set()

    background_gateway(adapter, run)
    await adapter._receive(event(1, text='question', parts=[MENTION]))
    await asyncio.wait_for(entered.wait(), 1)
    await adapter._receive(event(2, text='2', parts=[MENTION]))
    await asyncio.wait_for(adapter.groups.wait_idle(), 1)
    assert answered.is_set()
    await adapter.disconnect()


async def test_disconnect_cancels_background_group_processing(group_adapter):
    adapter = group_adapter()
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def run(message):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    background_gateway(adapter, run)
    await adapter._receive(event(1, parts=[MENTION]))
    await asyncio.wait_for(entered.wait(), 1)
    await adapter.disconnect()
    assert cancelled.is_set()


async def test_continuation_gateway_preserves_principal_and_disables_controls_and_media(group_adapter):
    from hermes_napcat.engagement import Decision

    adapter = group_adapter(proactive_assist={
        'dry_run': False, 'quiet_window_ms': 100,
        'classifier': {'enabled': True, 'model': 'test-classifier'},
        'continuation': {'enabled': True},
    })
    adapter.groups.classifier.decide = AsyncMock(
        return_value=Decision(True, .99, 'test', 'continue'))
    adapter._attachments = AsyncMock(return_value=([], [], []))
    entered, release = asyncio.Event(), asyncio.Event()
    delivered = []

    async def run(message):
        delivered.append(message)
        if not message.metadata['napcat_continuation']:
            entered.set()
            await release.wait()
            adapter.transport.call.return_value = {'message_id': 1001}
            assert (await adapter.send('group:300', 'initial answer')).success
        else:
            adapter.groups.before_send(Target.parse('group:300'))

    background_gateway(adapter, run)
    try:
        await adapter._receive(event(1, text='分析一下', parts=[MENTION]))
        await asyncio.wait_for(entered.wait(), 1)
        await adapter._receive(event(2, text='补充一点'))
        await asyncio.sleep(.15)
        release.set()
        await asyncio.wait_for(adapter.groups.wait_idle(), 2)
        assert len(delivered) == 2
        followup = delivered[-1]
        assert followup.user_id == followup.source.user_id == '200'
        assert followup.source.scope_id == '100' and followup.source.chat_id == 'group:300'
        assert followup.metadata['napcat_proactive'] and not followup.allow_gateway_control
        assert not followup.media_urls and 'initial answer' in followup.channel_context
        adapter._attachments.assert_awaited_once()
    finally:
        release.set()
        await adapter.disconnect()


async def test_continuation_keeps_invitation_in_truncated_gateway_context(group_adapter):
    from hermes_napcat.engagement import Decision

    adapter = group_adapter(
        allowed_users=['200', '201'],
        group_context={'enabled': True, 'history_limit': 1},
        proactive_assist={
            'dry_run': False, 'quiet_window_ms': 100,
            'classifier': {'enabled': True, 'model': 'test-classifier'},
            'continuation': {'enabled': True},
        })
    adapter.groups.classifier.decide = AsyncMock(
        return_value=Decision(True, .99, 'test', 'continue'))
    try:
        await adapter._receive(event(1, text='比较这两个数据库', parts=[MENTION]))
        await adapter.groups.wait_idle()
        await adapter._receive(event(2, user=201, text='哪个更快'))
        await adapter.groups.wait_idle()
        followup = adapter.handle_message.await_args.args[0]
        rows = [json.loads(line) for line in followup.channel_context.splitlines()][1:]
        assert [row['message_id'] for row in rows] == ['1']
        assert followup.user_id == '201' and followup.metadata['napcat_continuation']
        assert followup.reply_to_message_id is None and followup.reply_to_text is None
    finally:
        await adapter.disconnect()
