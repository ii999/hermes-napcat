"""Group policy simulations, including the host's fail-open middleware semantics."""
from __future__ import annotations

import asyncio
import json
import sys
import types
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from hermes_napcat import tool_policy as policy


def schema(name):
    return {"type": "function", "function": {
        "name": name, "description": name, "parameters": {"type": "object"}}}


def source(chat="group:300", user="200", profile=""):
    return SimpleNamespace(platform="napcat", chat_id=chat, user_id=user, profile=profile,
                           chat_type="group" if chat.startswith("group:") else "dm")


@pytest.fixture
def host(monkeypatch):
    env = ContextVar("test_policy_session_env", default={})
    sets = {"web": ["web_search"], "file": ["read_file", "write_file"],
            "write": ["write_file"], "qq": ["qq_send_message"], "mcp-test": ["mcp_allowed"],
            "code": ["execute_code"], "terminal": ["terminal"], "delegation": ["delegate_task"],
            "messaging": ["send_message"], "cronjob": ["cronjob_manage"]}
    state = SimpleNamespace(env=env, sets=sets, disabled=[], fail=False, calls=[], adapter=None)

    def definitions(*, enabled_toolsets, disabled_toolsets=None, **kwargs):
        state.calls.append(tuple(enabled_toolsets))
        if state.fail:
            raise RuntimeError("private-provider-secret")
        names = {name for toolset in enabled_toolsets for name in sets[toolset]}
        for toolset in disabled_toolsets or []:
            names.difference_update(sets[toolset])
        # Simulate the source-inspected Kanban auto-add path too.
        names.add("kanban_done")
        return [schema(name) for name in sorted(names)]

    def assemble(raw, *, config):
        visible = [item for item in raw if not item["function"]["name"].startswith("mcp_")]
        deferred = [item for item in raw if item["function"]["name"].startswith("mcp_")]
        if deferred:
            bridges = [schema(name) for name in ("tool_search", "tool_describe", "tool_call")]
            for bridge in bridges:
                bridge["function"]["description"] = ",".join(
                    item["function"]["name"] for item in deferred)
            visible.extend(bridges)
        return SimpleNamespace(tool_defs=visible)

    contents = {
        "gateway": {}, "gateway.config": {"Platform": lambda name: name},
        "gateway.run": {
            "_load_gateway_config": lambda: {"agent": {"disabled_toolsets": state.disabled}},
            "_gateway_runner_ref": lambda: SimpleNamespace(
                _authorization_adapter=lambda platform, profile: state.adapter)},
        "gateway.session_context": {"get_session_env": lambda name, default="": env.get().get(name, default)},
        "agent": {}, "agent.skill_utils": {"parse_config_string_list": lambda value: value or []},
        "model_tools": {"get_tool_definitions": definitions},
        "toolsets": {"resolve_toolset": lambda name: sets[name],
                     "validate_toolset": lambda name: name in sets},
        "tools": {}, "tools.tool_search": {
            "assemble_tool_defs": assemble, "load_config": lambda: {},
            "dispatch_tool_search": lambda args, current_tool_defs: json.dumps(
                [item["function"]["name"] for item in current_tool_defs]),
            "dispatch_tool_describe": lambda args, current_tool_defs: json.dumps(
                [item["function"]["name"] for item in current_tool_defs
                 if item["function"]["name"] in args.get("tools", [])]),
        },
    }
    for name, attrs in contents.items():
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    token = policy._scope.set(None)
    yield state
    policy._scope.reset(token)


def adapter(*sets):
    return SimpleNamespace(settings=SimpleNamespace(group_toolsets=tuple(sets)))


def names(request):
    return {item.get("function", item)["name"] for item in request.get("tools", [])}


def outbound(*tool_names):
    return {"model": "test", "messages": [{"role": "user", "content": "task"}],
            "tools": [schema(name) for name in tool_names], "tool_choice": "auto"}


def test_empty_group_cap_strips_cached_defaults_and_blocks_hidden_calls(host):
    request = outbound("terminal", "qq_send_message", "tool_call")
    request["extra_body"] = {"tools": [schema("hosted")], "parallel_tool_calls": True,
                             "temperature": .2}
    executed = []
    with policy.group_tool_scope(adapter(), source()):
        filtered = policy.run_group_llm(request=request, next_call=lambda value: value)
        result = policy.run_group_tool(tool_name="terminal", args={},
                                       next_call=lambda value: executed.append(value))
        veto = policy.guard_group_tool(tool_name="qq_send_message", args={})
    assert "tools" not in filtered and "tool_choice" not in filtered
    assert filtered["extra_body"] == {"temperature": .2}
    assert "error" in json.loads(result) and veto["action"] == "block"
    assert not executed and not host.calls
    assert "tools" in request  # cached/other-session definitions are not mutated


def test_nonempty_cap_keeps_normal_tools_excludes_defaults_and_profile_disabled(host):
    host.disabled = ["write"]
    request = outbound("web_search", "read_file", "write_file", "terminal", "qq_send_message")
    executed = []
    with policy.group_tool_scope(adapter("web", "file"), source()):
        filtered = policy.filter_llm_request(request=request)["request"]
        assert policy.run_group_tool(tool_name="read_file", args={"path": "ok"},
                                     next_call=lambda args: executed.append(args)) is None
        assert policy.guard_group_tool(tool_name="write_file", args={})["action"] == "block"
    assert names(filtered) == {"web_search", "read_file"}
    assert executed == [{"path": "ok"}]
    assert set(host.calls) == {("web", "file")}


@pytest.mark.parametrize("shape", ["chat", "responses", "anthropic"])
def test_wire_tool_shapes_are_capped_with_hosted_tools_removed(host, shape):
    tools = [schema("web_search"), schema("terminal")]
    if shape == "responses":
        tools = [{"type": "function", **item["function"]} for item in tools]
    elif shape == "anthropic":
        tools = [{"name": item["function"]["name"], "input_schema": {}} for item in tools]
    request = {"tools": tools + [{"type": "web_search"}], "tool_choice": "required"}
    with policy.group_tool_scope(adapter("web"), source()):
        result = policy.run_group_llm(request=request, next_call=lambda value: value)
    assert names(result) == {"web_search"} and len(result["tools"]) == 1
    assert result["tools"][0].get("type") == (None if shape == "anthropic" else "function")


def test_forced_forbidden_tool_choice_is_removed(host):
    request = outbound("web_search", "terminal")
    request["tool_choice"] = {"type": "function", "function": {"name": "terminal"}}
    with policy.group_tool_scope(adapter("web"), source()):
        result = policy.run_group_llm(request=request, next_call=lambda value: value)
    assert "tool_choice" not in result


def test_deferred_catalog_and_embedded_bridge_list_are_scoped(host):
    request = outbound("tool_search", "tool_describe", "tool_call", "mcp_forbidden", "terminal")
    with policy.group_tool_scope(adapter("mcp-test"), source()):
        filtered = policy.run_group_llm(request=request, next_call=lambda value: value)
        searched = policy.run_group_tool(tool_name="tool_search", args={}, next_call=lambda _: None)
        described = policy.run_group_tool(tool_name="tool_describe", args={"tools": ["mcp_forbidden"]},
                                         next_call=lambda _: None)
        executed = policy.run_group_tool(tool_name="mcp_allowed", args={}, next_call=lambda _: "real")
        denied = policy.run_group_tool(tool_name="mcp_forbidden", args={}, next_call=lambda _: "bad")
        batch = policy.run_group_tool(tool_name="tool_call", args={"calls": []},
                                     next_call=lambda _: "bad")
    assert names(filtered) == {"tool_search", "tool_describe", "tool_call"}
    assert all(item["function"]["description"] == "mcp_allowed" for item in filtered["tools"])
    assert json.loads(searched) == ["mcp_allowed"] and json.loads(described) == []
    assert executed == "real" and "error" in json.loads(denied)
    assert "cannot preserve" in json.loads(batch)["error"]


def test_cached_agent_policy_changes_and_registry_additions_cannot_restore_tools(host):
    current = adapter("web", "qq")
    cached_request = outbound("web_search", "qq_send_message", "terminal", "kanban_done")
    with policy.group_tool_scope(current, source()):
        assert names(policy.run_group_llm(request=cached_request, next_call=lambda value: value)) == {
            "web_search", "qq_send_message"}
        current.settings.group_toolsets = ("web",)
        assert names(policy.run_group_llm(request=cached_request, next_call=lambda value: value)) == {
            "web_search"}
        assert policy.guard_group_tool(tool_name="qq_send_message", args={})["action"] == "block"
        host.sets["other-plugin"] = ["new_forbidden"]
        assert policy.guard_group_tool(tool_name="new_forbidden", args={})["action"] == "block"


def test_callback_resolution_failure_denies_under_host_fail_open_semantics(host):
    host.fail = True
    executed = []

    # Same contract as Hermes middleware.py: callback errors continue downstream.
    def host_run(callback, payload, terminal, **context):
        try:
            return callback(**payload, next_call=terminal, **context)
        except Exception:
            return terminal(next(iter(payload.values())))

    with policy.group_tool_scope(adapter("web"), source()):
        filtered = host_run(policy.run_group_llm, {"request": outbound("terminal")}, lambda value: value)
        result = host_run(policy.run_group_tool, {"tool_name": "terminal", "args": {}},
                          lambda value: executed.append(value))
        veto = policy.guard_group_tool(tool_name="web_search", args={})
    assert not filtered.get("tools") and "error" in json.loads(result)
    assert veto["action"] == "block" and not executed


def test_unknown_configured_toolset_fails_closed(host):
    with policy.group_tool_scope(adapter("not-installed"), source()):
        assert not policy.run_group_llm(request=outbound("terminal"), next_call=lambda value: value).get("tools")
        assert policy.guard_group_tool(tool_name="terminal", args={})["action"] == "block"


def test_private_and_other_platforms_preserve_requests_and_dispatch(host):
    request = outbound("terminal")
    with policy.group_tool_scope(adapter(), source("private:200")):
        assert policy.run_group_llm(request=request, next_call=lambda value: value) is request
        assert policy.run_group_tool(tool_name="terminal", args={}, next_call=lambda _: "private") == "private"
    assert policy.run_group_llm(request=request, next_call=lambda value: value, platform="telegram") is request
    assert not host.calls


def test_unbound_group_context_uses_current_profile_adapter_or_denies(host):
    host.env.set({"HERMES_SESSION_PLATFORM": "napcat", "HERMES_SESSION_CHAT_ID": "group:300",
                  "HERMES_SESSION_USER_ID": "200", "HERMES_SESSION_PROFILE": "secondary"})
    host.adapter = adapter("web")
    assert names(policy.run_group_llm(request=outbound("web_search", "terminal"),
                                    next_call=lambda value: value)) == {"web_search"}
    host.adapter = None
    assert policy.guard_group_tool(tool_name="web_search", args={})["action"] == "block"


def test_stale_scope_cannot_borrow_another_user_or_profile_grant(host):
    with policy.group_tool_scope(adapter("qq"), source(user="200", profile="first")):
        host.env.set({"HERMES_SESSION_PLATFORM": "napcat", "HERMES_SESSION_CHAT_ID": "group:300",
                      "HERMES_SESSION_USER_ID": "201", "HERMES_SESSION_PROFILE": "second"})
        assert policy.guard_group_tool(tool_name="qq_send_message", args={})["action"] == "block"


async def test_thread_and_parallel_task_scopes_remain_independent(host):
    async def turn(cap):
        with policy.group_tool_scope(adapter(*cap), source()):
            await asyncio.sleep(0)
            return await asyncio.to_thread(policy.run_group_llm, request=outbound("web_search", "terminal"),
                                           next_call=lambda value: value)
    first, second = await asyncio.gather(turn(("web",)), turn(()))
    assert names(first) == {"web_search"} and not names(second)


def test_explicit_code_execution_keeps_cap_on_nested_tool_bridge(host):
    with policy.group_tool_scope(adapter("code"), source()):
        def code_rpc(_):
            return policy.run_group_tool(tool_name="terminal", args={}, next_call=lambda _: "bad")
        result = policy.run_group_tool(tool_name="execute_code", args={}, next_call=code_rpc)
        denied = policy.run_group_tool(tool_name="delegate_task", args={}, next_call=lambda _: "bad")
    assert "error" in json.loads(result)
    assert "cannot preserve" in json.loads(denied)["error"]


def test_shared_followup_cannot_launder_generic_message_or_scheduled_delivery(host):
    current = adapter("web", "qq", "messaging", "cronjob")
    current.groups = SimpleNamespace(cross_chat_allowed=False)
    with policy.group_tool_scope(current, source(user="200")):
        request = outbound("web_search", "qq_send_message", "send_message", "cronjob_manage")
        assert names(policy.run_group_llm(request=request, next_call=lambda value: value)) == {
            "web_search", "qq_send_message"}
        for name in ("send_message", "cronjob_manage"):
            assert policy.guard_group_tool(tool_name=name, args={})["action"] == "block"
            assert "error" in json.loads(policy.run_group_tool(
                tool_name=name, args={}, next_call=lambda _: "must not execute"))
        current.groups.cross_chat_allowed = True
        assert policy.run_group_tool(tool_name="send_message", args={},
                                     next_call=lambda _: "authorized") == "authorized"


def test_context_failure_does_not_reach_host_fail_open_path(host, monkeypatch):
    monkeypatch.setattr(policy, "_current_scope", lambda context: 1 / 0)
    called = []
    assert not policy.run_group_llm(request=outbound("terminal"), next_call=lambda value: value).get("tools")
    assert policy.guard_group_tool(tool_name="terminal", args={})["action"] == "block"
    result = policy.run_group_tool(tool_name="terminal", args={}, next_call=lambda _: called.append(True))
    assert "error" in json.loads(result) and not called


async def test_adapter_handler_and_queue_rebind_actual_source(hermes_doubles, settings, host):
    current = hermes_doubles.module.NapCatAdapter(
        hermes_doubles.PlatformConfig(extra=settings(group_toolsets=["web"]).model_dump()))
    observed = []

    async def handler(event):
        observed.append(names(policy.run_group_llm(request=outbound("web_search", "terminal"),
                                                   next_call=lambda value: value)))
        await current.on_processing_start(SimpleNamespace(source=source("private:200")))
        observed.append(names(policy.run_group_llm(request=outbound("terminal"),
                                                   next_call=lambda value: value)))
        await current.on_processing_start(SimpleNamespace(source=source("group:300", user="201")))
        observed.append(policy.guard_group_tool(tool_name="terminal", args={})["action"])
        return "normal response"

    current.set_message_handler(handler)
    assert await current._message_handler(SimpleNamespace(source=source())) == "normal response"
    assert observed == [{"web_search"}, {"terminal"}, "block"]


def test_registration_requires_real_host_policy_capabilities(host):
    middleware, hooks = {}, {}
    policy.register_group_tool_policy(SimpleNamespace(
        register_middleware=lambda kind, callback: middleware.update({kind: callback}),
        register_hook=lambda kind, callback: hooks.update({kind: callback})))
    assert set(middleware) == {"llm_request", "llm_execution", "tool_execution"}
    assert hooks["pre_tool_call"] is policy.guard_group_tool
    with pytest.raises(RuntimeError, match="middleware"):
        policy.register_group_tool_policy(SimpleNamespace())
