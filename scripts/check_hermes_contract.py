#!/usr/bin/env python3
"""Run with the real Hermes Python interpreter. Does not start an agent or access QQ."""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    from gateway.config import Platform, PlatformConfig
    from gateway.authz_mixin import GatewayAuthorizationMixin
    from gateway.platform_registry import PlatformEntry
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.platforms.event import MessageEvent
    from gateway.session_context import get_session_env
    from hermes_cli.plugins import PluginContext
    from hermes_cli.commands import should_bypass_active_session
    from tools.clarify_gateway import get_pending_for_session
    from tools.credential_files import to_agent_visible_cache_path
    from tools.registry import registry
    from tools.transcription_tools import transcribe_audio, transcribe_audio_local_fallback
    from hermes_constants import get_hermes_dir
    from hermes_napcat.group_adapter import GroupNapCatAdapter
    from hermes_napcat.plugin import register

    assert issubclass(GroupNapCatAdapter, BasePlatformAdapter)
    assert not inspect.isabstract(GroupNapCatAdapter), GroupNapCatAdapter.__abstractmethods__
    assert "extra" in inspect.signature(PlatformConfig).parameters
    assert {
        "allow_gateway_control", "channel_context", "channel_prompt",
        "reply_to_author_id", "reply_to_author_name", "reply_to_is_own_message",
    }.issubset(inspect.signature(MessageEvent).parameters)
    assert "scope_id" in inspect.signature(BasePlatformAdapter.build_source).parameters
    assert "event" in inspect.signature(BasePlatformAdapter.on_processing_start).parameters
    assert "handler" in inspect.signature(BasePlatformAdapter.set_message_handler).parameters
    assert {"event", "session_key", "text_content", "metadata", "reply_to", "is_ephemeral_response"}.issubset(
        inspect.signature(BasePlatformAdapter.send_final_ledgered).parameters)
    assert "event" in inspect.signature(BasePlatformAdapter._event_session_key).parameters
    assert callable(should_bypass_active_session)
    assert "include_choice_prompts" in inspect.signature(get_pending_for_session).parameters
    assert "profile" in inspect.signature(GatewayAuthorizationMixin._authorization_adapter).parameters
    assert callable(get_session_env)
    assert callable(get_hermes_dir) and callable(to_agent_visible_cache_path)
    assert {"name", "args"}.issubset(inspect.signature(registry.dispatch).parameters)
    assert "quiet" in inspect.signature(registry.get_definitions).parameters
    assert "source" in inspect.signature(transcribe_audio).parameters
    assert callable(transcribe_audio_local_fallback)
    tool_parameters = inspect.signature(PluginContext.register_tool).parameters
    assert {"toolset", "schema", "handler", "is_async"}.issubset(tool_parameters)
    entries = []
    tools = []
    register(SimpleNamespace(
        register_platform=lambda **kw: entries.append(PlatformEntry(**kw)),
        register_tool=lambda **kw: tools.append(kw),
    ))
    assert len(entries) == 1 and entries[0].name == "napcat"
    assert entries[0].parse_target_ref_fn("group:12345") == ("group:12345", None)
    assert entries[0].validate_target_ref_fn("12345") is not True
    assert entries[0].check_fn()
    by_name = {item["name"]: item for item in tools}
    assert by_name["qq_read_media"]["toolset"] == "napcat_qq_read"
    assert by_name["qq_get_forward"]["toolset"] == "napcat_qq_read"
    assert by_name["qq_send_message"]["toolset"] == "napcat_qq"
    assert all(item["is_async"] for item in tools)
    # Exercise the host's native chat grant with test-owned IDs, without a gateway or secrets.
    probe = SimpleNamespace(_adapter_extra_for_source=lambda source: {
        "group_allowed_chats": ["group:300"]})
    for chat_type, target, expected in (
        ("group", "group:300", True), ("group", "group:301", False),
        ("dm", "private:300", False),
    ):
        source = SimpleNamespace(platform=Platform("napcat"), chat_id=target, is_bot=False)
        assert GatewayAuthorizationMixin._chat_scoped_grant(
            probe, source, None, chat_type == "group", False) is expected
    print("PASS: real Hermes imports, adapter contract, platform and QQ tool registration")
    print("PASS: Hermes native group grant helper isolates group IDs from private chats")
    print("This check does not exercise the full gateway/session runner or a real QQ account.")


if __name__ == "__main__":
    main()
