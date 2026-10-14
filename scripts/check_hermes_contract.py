#!/usr/bin/env python3
"""Run with the real Hermes Python interpreter. Does not start an agent or access QQ."""
from __future__ import annotations

import inspect
import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _check_group_policy_contract() -> None:
    """Use an isolated real manager; no live Agent, tool handler, model, or QQ call."""
    from agent.auxiliary_client import resolve_provider_client
    from agent.turn_api_call import perform_api_call
    from agent.tool_executor import _run_agent_tool_execution_middleware
    from gateway.run import GatewayRunner
    from hermes_cli.middleware import VALID_MIDDLEWARE
    from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
    from model_tools import get_tool_definitions
    from toolsets import resolve_toolset, validate_toolset
    from tools.thread_context import propagate_context_to_thread
    from tools.tool_search import (
        assemble_tool_defs, dispatch_tool_describe, dispatch_tool_search,
    )
    from hermes_napcat.tool_policy import group_tool_scope, register_group_tool_policy

    assert {"llm_request", "llm_execution", "tool_execution"} <= VALID_MIDDLEWARE
    assert {"kind", "callback"} <= inspect.signature(PluginContext.register_middleware).parameters.keys()
    assert {"hook_name", "callback"} <= inspect.signature(PluginContext.register_hook).parameters.keys()
    assert {"provider", "model", "explicit_base_url", "explicit_api_key", "api_mode", "main_runtime"} <= (
        inspect.signature(resolve_provider_client).parameters.keys())
    assert {"source", "user_config"} <= inspect.signature(
        GatewayRunner._resolve_session_agent_runtime).parameters.keys()
    assert "source" in inspect.signature(GatewayRunner._profile_scope_for_source).parameters
    assert "func" in inspect.signature(GatewayRunner._run_in_executor_with_context).parameters
    assert {"enabled_toolsets", "disabled_toolsets", "skip_tool_search_assembly"} <= (
        inspect.signature(get_tool_definitions).parameters.keys())
    assert callable(resolve_toolset) and callable(validate_toolset)
    assert callable(assemble_tool_defs) and callable(propagate_context_to_thread)
    for dispatch in (dispatch_tool_search, dispatch_tool_describe):
        assert "current_tool_defs" in inspect.signature(dispatch).parameters

    # Confirm that the installed source actually calls the policy entry points,
    # beyond merely exporting callable registration methods. No core patching.
    for function, expected in (
        (perform_api_call, {"run_llm_execution_middleware"}),
        (_run_agent_tool_execution_middleware,
         {"apply_tool_request_middleware", "run_tool_execution_middleware"}),
    ):
        tree = ast.parse(inspect.getsource(function))
        calls = {node.func.id for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        assert expected <= calls, f"Installed Hermes no longer calls {sorted(expected)}"

    manager = PluginManager(scope_key=str(ROOT))
    ctx = PluginContext(PluginManifest(name="napcat-contract-probe"), manager)
    register_group_tool_policy(ctx)
    source = SimpleNamespace(platform="napcat", chat_id="group:300", user_id="200", profile="")
    adapter = SimpleNamespace(settings=SimpleNamespace(group_toolsets=()))
    request = {"model": "probe", "tools": [{"type": "function", "function": {
        "name": "terminal", "parameters": {"type": "object"}}}], "tool_choice": "auto"}
    dispatched = []
    with group_tool_scope(adapter, source):
        results = manager.invoke_middleware("llm_request", request=request, platform="napcat")
        assert len(results) == 1 and "tools" not in results[0]["request"]
        results = manager.invoke_middleware(
            "llm_execution", request=request, platform="napcat", next_call=lambda payload: payload)
        assert len(results) == 1 and "tools" not in results[0]
        results = manager.invoke_middleware(
            "tool_execution", tool_name="terminal", args={},
            next_call=lambda args: dispatched.append(args))
        assert len(results) == 1 and "error" in json.loads(results[0])
        vetoes = manager.invoke_hook("pre_tool_call", tool_name="terminal", args={})
        assert vetoes and vetoes[0]["action"] == "block"
    assert not dispatched
    print("PASS: real Hermes policy registration, source call sites and empty-cap callbacks")
    print("PASS: selected-runtime admission and scoped deferred-catalog API signatures")


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
    middleware = []
    hooks = []
    register(SimpleNamespace(
        register_platform=lambda **kw: entries.append(PlatformEntry(**kw)),
        register_tool=lambda **kw: tools.append(kw),
        register_middleware=lambda kind, callback: middleware.append((kind, callback)),
        register_hook=lambda name, callback: hooks.append((name, callback)),
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
    assert {kind for kind, _ in middleware} == {"llm_request", "llm_execution", "tool_execution"}
    assert {name for name, _ in hooks} == {"pre_tool_call"}
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
    _check_group_policy_contract()
    print("This check does not exercise the full gateway/session runner or a real QQ account.")


if __name__ == "__main__":
    main()
