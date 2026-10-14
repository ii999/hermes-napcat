"""Group-scoped grants; Hermes lifecycle helpers here are simulated, not a live gateway."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from hermes_napcat import group_tools, tools
from hermes_napcat.media import MediaStore
from hermes_napcat.policy import Policy
from hermes_napcat.protocol import Incoming, Target
from test_adapter import make_adapter
from test_group_adapter import background_gateway, group_adapter as group_adapter
from test_group_context import event
from test_group_chat import MENTION
from test_group_continuation import select
from test_media_pipeline import cached, part
from test_tools import bind_session


@pytest.mark.parametrize("grants", [
    "group:300", ["*"], ["group:*"], ["private:300"], [300], [True],
    ["group:999"], ["group:0"], ["group:300:1"], [" group:300"],
])
def test_group_grants_reject_wildcards_private_targets_and_unlisted_groups(settings, grants):
    with pytest.raises(ValidationError):
        settings(group_allowed_chats=grants)


def test_normalized_grants_reach_hermes_without_mutating_other_profile_config(hermes_doubles, settings):
    cfg = hermes_doubles.PlatformConfig(extra={
        **settings().model_dump(), "group_allowed_chats": ["group:00300", "group:300"]})
    adapter = hermes_doubles.module.NapCatAdapter(cfg)
    assert adapter.settings.group_allowed_chats == ("group:300",)
    assert adapter.config.extra["group_allowed_chats"] == ["group:300"]
    assert cfg.extra["group_allowed_chats"] == ["group:00300", "group:300"]
    other = make_adapter(hermes_doubles, settings)
    assert other.config.extra["group_allowed_chats"] == []
    assert not other.policy.can_receive(Incoming.parse(event(user=999)))


def test_group_grants_leave_private_outbound_and_other_group_acl_closed(settings):
    policy = Policy(settings(allowed_groups=["300", "301"], group_allowed_chats=["group:300"]))
    assert policy.can_receive(Incoming.parse(event(user=999)))
    for raw in [event(user=999, message_type="private"), event(user=999, group=301),
                event(user=999, group=302), event(user=999, self_id=101), event(user=100)]:
        assert not policy.can_receive(Incoming.parse(raw))
    assert not policy.authorized_user("999")
    assert not policy.can_send(Target.parse("private:999"))
    assert policy.can_receive(Incoming.parse(event(user=200, message_type="private")))
    assert policy.can_receive(Incoming.parse(event(user=200, group=301)))


@pytest.mark.parametrize("context_enabled", [False, True])
async def test_public_member_admission_keeps_identity_and_denies_private_before_media(
    group_adapter, context_enabled,
):
    adapter = group_adapter(group_allowed_chats=["group:300"],
                            group_context={"enabled": context_enabled, "history_backfill": False})
    adapter._attachments = AsyncMock(return_value=([], [], []))
    try:
        await adapter._receive(event(1, user=999, message_type="private", parts=[part("image")]))
        adapter._attachments.assert_not_awaited()
        adapter.handle_message.assert_not_awaited()
        await adapter._receive(event(2, user=999, text="/stop", parts=[MENTION],
                                     sender={"user_id": 999, "role": "owner"}))
        await adapter.groups.wait_idle()
        message = adapter.handle_message.await_args.args[0]
        assert message.user_id == "999" and message.source.user_id == "999"
        assert message.source.chat_id == "group:300" and not message.allow_gateway_control
        assert adapter.toolsets_for_source(message.source) == []
    finally:
        await adapter.disconnect()


async def test_public_members_share_observation_and_native_continuation(group_adapter):
    adapter = group_adapter(
        group_allowed_chats=["group:300"],
        group_context={"enabled": True, "observe_untriggered": True,
                       "observe_all_members": False, "history_backfill": False},
        proactive_assist={"dry_run": False, "quiet_window_ms": 100,
                          "continuation": {"enabled": True}},
    )
    adapter.transport.call = AsyncMock(return_value={"message_id": 900})
    received = []
    adapter._decide_followup = AsyncMock(side_effect=lambda batch, incoming: select(batch))
    async def run(message):
        if message.metadata['napcat_continuation']:
            received.append(message)
            assert (await adapter.send('group:300', 'reply to the next speaker')).success
    background_gateway(adapter, run)
    try:
        await adapter._receive(event(1, user=997, text="shared background"))
        await adapter._receive(event(2, user=998, text="analyze", parts=[MENTION]))
        await adapter.groups.wait_idle()
        await adapter._receive(event(3, user=999, text="additional details"))
        await adapter.groups.wait_idle()
        assert len(received) == 1
        assert received[0].source.user_id == "999" and not received[0].allow_gateway_control
        assert "shared background" in received[0].channel_context
        assert '"user_id":"998"' in received[0].channel_context
        assert '"user_id":"999"' in received[0].channel_context
        assert adapter.transport.call.await_args.args[0] == "send_group_msg"
        assert not adapter.policy.can_send(Target.parse("private:999"))
    finally:
        await adapter.disconnect()


async def test_explicit_tools_use_group_grant_without_private_or_cross_chat_access(
    group_adapter, monkeypatch,
):
    adapter = group_adapter(
        group_allowed_chats=["group:300"], allowed_groups=["300", "301"],
        group_context={"enabled": True, "history_backfill": False},
        qq_tools={"enabled": True, "allow_cross_chat": True},
    )
    adapter.transport.call = AsyncMock(return_value={"message_id": 900})
    try:
        await adapter._receive(event(1, user=999, text="group record"))
        bind_session(monkeypatch, adapter, user="998")
        recent = json.loads(await group_tools.qq_get_recent_messages({"limit": 10}))
        assert recent["success"] and recent["messages"][0]["sender"]["user_id"] == "999"
        assert json.loads(await tools.qq_send_message({"text": "same group"}))["success"]
        adapter.transport.call.reset_mock()
        for target in ("group:301", "private:200", "private:998"):
            assert not json.loads(await tools.qq_send_message({"target": target, "text": "denied"}))["success"]
        bind_session(monkeypatch, adapter, target="private:998", user="998")
        assert not json.loads(await tools.qq_send_message({"target": "group:300", "text": "denied"}))["success"]
        adapter.transport.call.assert_not_awaited()
    finally:
        await adapter.disconnect()


async def test_public_group_media_is_readable_but_grant_does_not_follow_user_to_other_chats(
    hermes_doubles, settings, tmp_path,
):
    adapter = make_adapter(
        hermes_doubles, settings, allowed_groups=["300", "301"],
        group_allowed_chats=["group:300"],
        media={"references": {"enabled": True}, "download_mode": "stream"},
    )
    adapter.media = MediaStore(adapter.settings.media, tmp_path / "cache")
    image = cached(adapter.media)
    async def download(*args, access_check, **kwargs):
        access_check()
        return image
    adapter._stream_downloader = SimpleNamespace(download=AsyncMock(side_effect=download))
    try:
        source = Incoming.parse(event(user=999, parts=[part("image")]))
        ref = adapter.media_refs.remember(source)[0]
        assert await adapter.resolve_media(ref.media_id, source.target, "998") == image
        await adapter._receive(event(2, user=999, parts=[MENTION, part("image")]))
        assert adapter.handle_message.await_args.args[0].media_urls == [str(image.path)]
        for target in (Target.parse("private:998"), Target.parse("group:301")):
            with pytest.raises(PermissionError):
                await adapter.resolve_media(ref.media_id, target, "998")
    finally:
        await adapter.disconnect()
