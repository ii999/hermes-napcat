"""Enforce group tool grants at Hermes provider and dispatch boundaries.

The native adapter hook is advisory on the supported host: empty overrides are
ignored and plugin/MCP defaults can expand nonempty selections. This policy is
independent of Agent caches and travels with the actual turn's ContextVars.
"""
from __future__ import annotations

import copy
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from .protocol import Target

log = logging.getLogger(__name__)
_BRIDGES = frozenset({"tool_search", "tool_describe", "tool_call"})
# Delegated child processes are not proven to preserve the group policy. Ordinary
# explicitly granted terminal/execute_code access is kept; execute_code's tool RPC
# returns through host hooks/middleware with propagated parent context.
_UNSUPPORTED = frozenset({"delegate_task"})
_CROSS_CHAT_ENTRY_POINTS = frozenset({"send_message", "cronjob_manage"})


@dataclass(frozen=True)
class _Scope:
    adapter: Any
    chat_id: str
    user_id: str
    profile: str
    denied: bool = False


@dataclass(frozen=True)
class _Grant:
    names: frozenset[str]
    raw: list[dict[str, Any]]
    shown: list[dict[str, Any]]


_scope: ContextVar[_Scope | None] = ContextVar("napcat_group_tool_scope", default=None)


def _source_scope(adapter: Any, source: Any) -> _Scope | None:
    platform = getattr(source, "platform", "")
    if str(getattr(platform, "value", platform)) != "napcat":
        return None
    address = str(getattr(source, "chat_id", ""))
    try:
        target = Target.parse(address)
    except ValueError:
        return _Scope(None, address, "", "", denied=True)
    if target.kind != "group":
        return None
    return _Scope(adapter, address, str(getattr(source, "user_id", "") or ""),
                  str(getattr(source, "profile", "") or ""))


def bind_group_tool_scope(adapter: Any, source: Any) -> None:
    """Rebind a queued processing task to its actual source; private turns clear it."""
    _scope.set(_source_scope(adapter, source))


@contextmanager
def group_tool_scope(adapter: Any, source: Any) -> Iterator[None]:
    """Cover handler paths that do not invoke on_processing_start, restoring on exit."""
    token = _scope.set(_source_scope(adapter, source))
    try:
        yield
    finally:
        _scope.reset(token)


def _current_scope(context: dict[str, Any]) -> _Scope | None:
    bound = _scope.get()
    raw_platform = context.get("platform") or ""
    platform = str(getattr(raw_platform, "value", raw_platform))
    # A policy scope deliberately follows nested execution, which can omit platform.
    if platform and platform != "napcat":
        return None
    try:
        from gateway.session_context import get_session_env

        env_platform = str(get_session_env("HERMES_SESSION_PLATFORM", "") or "")
        chat_id = str(get_session_env("HERMES_SESSION_CHAT_ID", "") or "")
        user_id = str(get_session_env("HERMES_SESSION_USER_ID", "") or "")
        profile = str(get_session_env("HERMES_SESSION_PROFILE", "") or "")
    except Exception:
        if bound is not None:
            return bound
        return _Scope(None, "", "", "", denied=True) if platform == "napcat" else None
    if bound is not None:
        if (env_platform and env_platform != "napcat"
                or chat_id and chat_id != bound.chat_id
                or user_id and bound.user_id and user_id != bound.user_id
                or profile and bound.profile and profile != bound.profile):
            return _Scope(None, chat_id, user_id, profile, denied=True)
        return bound
    if env_platform != "napcat" and platform != "napcat":
        return None
    try:
        target = Target.parse(chat_id)
    except ValueError:
        return _Scope(None, chat_id, user_id, profile, denied=True)
    if target.kind == "private":
        return None
    # Background group work may bypass the adapter processing callback. Derive the
    # live profile's cap from its authorization adapter, never from host defaults.
    try:
        from gateway.config import Platform
        from gateway.run import _gateway_runner_ref

        runner = _gateway_runner_ref()
        adapter = runner._authorization_adapter(Platform("napcat"), profile or None)
        if adapter is None:
            raise ValueError("missing profile adapter")
        return _Scope(adapter, chat_id, user_id, profile)
    except Exception:
        return _Scope(None, chat_id, user_id, profile, denied=True)


def _name(schema: Any) -> str | None:
    if not isinstance(schema, dict):
        return None
    kind = schema.get("type")
    if kind == "function":
        function = schema.get("function", schema)
        name = function.get("name") if isinstance(function, dict) else None
    elif kind is None and "input_schema" in schema:  # Anthropic wire format
        name = schema.get("name")
    else:
        return None
    return name if isinstance(name, str) and name else None


def _grant(scope: _Scope) -> _Grant:
    if scope.denied or scope.adapter is None:
        raise ValueError("group source policy is unavailable")
    configured = list(scope.adapter.settings.group_toolsets)
    if not configured:
        return _Grant(frozenset(), [], [])
    from gateway.run import _load_gateway_config
    from agent.skill_utils import parse_config_string_list
    from model_tools import get_tool_definitions
    from toolsets import resolve_toolset, validate_toolset

    if not all(isinstance(name, str) and validate_toolset(name) for name in configured):
        raise ValueError("configured group toolset is unavailable")
    config = _load_gateway_config()
    disabled = parse_config_string_list((config.get("agent") or {}).get("disabled_toolsets"))
    # Filter the host's exceptional Kanban worker auto-add too. Explicit toolset
    # resolution is the ceiling; availability and profile disabled sets can shrink it.
    declared = {name for toolset in configured for name in resolve_toolset(toolset)}
    raw = get_tool_definitions(enabled_toolsets=configured, disabled_toolsets=disabled or None,
                               quiet_mode=True, skip_tool_search_assembly=True)
    denied = set(_UNSUPPORTED)
    groups = getattr(scope.adapter, "groups", None)
    if groups is not None and not groups.cross_chat_allowed:
        # These host entry points can send as the operator or create a future run
        # whose origin no longer contains the contributors' reduced authority.
        denied.update(_CROSS_CHAT_ENTRY_POINTS)
    raw = [item for item in raw if _name(item) in declared and _name(item) not in denied]
    names = frozenset(_name(item) for item in raw)
    if not all(isinstance(name, str) for name in names):
        raise ValueError("invalid group tool schemas")
    # Assemble from the capped raw catalog, not from cached broader Agent definitions.
    from tools.tool_search import assemble_tool_defs, load_config

    shown = assemble_tool_defs(raw, config=load_config()).tool_defs
    return _Grant(names, raw, shown)


def _wire_schema(schema: dict[str, Any], sample: dict[str, Any]) -> dict[str, Any]:
    """Convert the host's Chat Completions definition to the request's tool shape."""
    function = copy.deepcopy(schema["function"])
    if "function" in sample:
        return {"type": "function", "function": function}
    if sample.get("type") == "function":
        return {"type": "function", **function}
    return {"name": function["name"], "description": function.get("description", ""),
            "input_schema": function.get("parameters", {})}


def _without_tools(request: dict[str, Any]) -> dict[str, Any]:
    result = dict(request)
    keys = {"tools", "tool_choice", "parallel_tool_calls", "functions", "function_call"}
    for key in keys:
        result.pop(key, None)
    extra = result.get("extra_body")
    if isinstance(extra, dict):
        result["extra_body"] = {key: value for key, value in extra.items()
                                if key not in keys and key != "extra_body"}
    return result


def _filtered_request(request: dict[str, Any], grant: _Grant) -> dict[str, Any]:
    result = _without_tools(request)
    schemas = {name: schema for schema in grant.shown if (name := _name(schema))}
    selected = []
    for item in request.get("tools") or []:
        name = _name(item)
        if name in schemas:
            # Replace definitions (including the embedded Tool Search catalog) with
            # capped host schemas. Native hosted tools have no grant and are removed.
            selected.append(_wire_schema(schemas[name], item))
    if selected:
        result["tools"] = selected
        # Optional choices must not force a removed function or a native hosted tool.
        choice = request.get("tool_choice")
        if choice in ("auto", "none", "required", "any"):
            result["tool_choice"] = choice
        elif isinstance(choice, dict):
            chosen = _name(choice)
            if chosen in {_name(item) for item in selected}:
                result["tool_choice"] = copy.deepcopy(choice)
        if "parallel_tool_calls" in request:
            result["parallel_tool_calls"] = request["parallel_tool_calls"]
    return result


def _filter(request: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    try:
        scope = _current_scope(context)
        if scope is None:
            return request
        return _filtered_request(request, _grant(scope))
    except Exception as exc:
        log.warning("NapCat group tool policy denied schemas (%s)", type(exc).__name__)
        return _without_tools(request)


def filter_llm_request(*, request: dict[str, Any], **context: Any) -> dict[str, Any]:
    """Public request middleware contract: return a replacement provider request."""
    return {"request": _filter(request, context), "source": "napcat",
            "reason": "Apply the current group tool grant"}


def run_group_llm(*, request: dict[str, Any], next_call: Any, **context: Any) -> Any:
    """Repeat the cap immediately before the provider call, including cached Agents."""
    return next_call(_filter(request, context))


def _denial(tool_name: str) -> str:
    if tool_name in _UNSUPPORTED or tool_name == "tool_call":
        return "This tool entry point cannot preserve the NapCat group tool policy"
    return "This tool is not permitted by the current NapCat group tool policy"


def guard_group_tool(*, tool_name: str, args: dict[str, Any], **context: Any) -> Any:
    try:
        scope = _current_scope(context)
        if scope is None:
            return None
        grant = _grant(scope)
        if tool_name in grant.names or tool_name in {"tool_search", "tool_describe"} and grant.raw:
            return None
    except Exception as exc:
        log.warning("NapCat group tool policy denied execution (%s)", type(exc).__name__)
    return {"action": "block", "message": _denial(tool_name)}


def run_group_tool(
    *, tool_name: str, args: dict[str, Any], next_call: Any, **context: Any,
) -> Any:
    try:
        scope = _current_scope(context)
        if scope is not None:
            grant = _grant(scope)
            if tool_name in {"tool_search", "tool_describe"} and grant.raw:
                from tools.tool_search import dispatch_tool_describe, dispatch_tool_search

                dispatch = dispatch_tool_search if tool_name == "tool_search" else dispatch_tool_describe
                return dispatch(args, current_tool_defs=grant.raw)
            if tool_name not in grant.names:
                return json.dumps({"error": _denial(tool_name)})
    except Exception as exc:
        log.warning("NapCat group tool policy denied execution (%s)", type(exc).__name__)
        return json.dumps({"error": "NapCat group tool policy is unavailable; tool was not executed"})
    # Downstream errors belong to Hermes. Do not catch them and accidentally call twice.
    return next_call(args)


def register_group_tool_policy(ctx: Any) -> None:
    """Require the supported public host policy APIs before registering the platform."""
    if not callable(getattr(ctx, "register_middleware", None)) or not callable(
            getattr(ctx, "register_hook", None)):
        raise RuntimeError("NapCat requires Hermes LLM and tool policy middleware")
    ctx.register_middleware("llm_request", filter_llm_request)
    ctx.register_middleware("llm_execution", run_group_llm)
    ctx.register_middleware("tool_execution", run_group_tool)
    ctx.register_hook("pre_tool_call", guard_group_tool)
