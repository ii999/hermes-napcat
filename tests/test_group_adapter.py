"""Actual plugin wiring with the repository's Hermes doubles, not a real Gateway run."""
import asyncio
import importlib
import json
import sys
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
