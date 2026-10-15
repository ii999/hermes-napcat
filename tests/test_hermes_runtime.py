"""Admission boundary simulations; these do not execute a Hermes model or QQ."""
from __future__ import annotations

import asyncio
import sys
import threading
import types
from contextlib import contextmanager
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from hermes_napcat.hermes_runtime import HermesDecisionError, judge_participation


@pytest.fixture
def runtime(monkeypatch):
    profile = ContextVar("test_admission_profile", default="outside")
    clients = []
    routed = []
    configs = []

    class Client:
        def __init__(self):
            self.closed = 0
            self.calls = []
            self.result = {"choices": [{"message": {"content": '{"action":"observe"}'},
                                       "finish_reason": "stop"}]}
            self.create_impl = None
            self.base_url = "https://provider.test/v1"
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        def create(self, **kwargs):
            self.calls.append((profile.get(), kwargs))
            return self.create_impl() if self.create_impl else self.result

        def close(self):
            self.closed += 1

    class CodexClient(Client):
        pass

    class AnthropicClient(Client):
        pass

    def resolve(provider, **kwargs):
        routed.append((profile.get(), provider, kwargs))
        client_type = {"codex_responses": CodexClient,
                       "anthropic_messages": AnthropicClient}.get(kwargs["api_mode"], Client)
        client = client_type()
        if setup.prepare:
            setup.prepare(client)
        clients.append(client)
        return client, setup.wire_model or kwargs["model"]

    class Runner:
        @contextmanager
        def _profile_scope_for_source(self, source):
            token = profile.set(source.profile)
            try:
                yield
            finally:
                profile.reset(token)

        def _resolve_session_agent_runtime(self, *, source, user_config):
            assert profile.get() == source.profile
            return source.model, {"provider": source.provider, "api_mode": source.api_mode,
                                  "api_key": "test-secret", "base_url": "https://provider.test/v1"}

        async def _run_in_executor_with_context(self, callback):
            return await asyncio.to_thread(callback)

    def load():
        configs.append(profile.get())
        return {"model": {"default": "different-profile-default"}}

    setup = SimpleNamespace(prepare=None, wire_model=None, clients=clients, routed=routed,
                            configs=configs, profile=profile, runner=Runner())
    for name, attrs in {
        "gateway": {}, "gateway.run": {
            "_gateway_runner_ref": lambda: setup.runner, "_load_gateway_config": load},
        "agent": {}, "agent.auxiliary_client": {
            "resolve_provider_client": resolve,
            "AnthropicAuxiliaryClient": AnthropicClient, "CodexAuxiliaryClient": CodexClient},
    }.items():
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    return setup


def source(profile="secondary", model="selected-session-model", api_mode="codex_responses"):
    return SimpleNamespace(profile=profile, model=model, provider="custom", api_mode=api_mode)


@pytest.mark.parametrize("api_mode", ["codex_responses", "anthropic_messages", "chat_completions", None])
async def test_current_profile_session_model_and_api_mode_without_tools(runtime, api_mode):
    text = await judge_participation(source(api_mode=api_mode), "strict decision prompt",
                                     timeout=1, max_tokens=123)
    assert text == '{"action":"observe"}'
    assert runtime.configs == ["secondary"]
    profile, provider, route = runtime.routed[0]
    assert (profile, provider, route["model"], route["api_mode"]) == (
        "secondary", "custom", "selected-session-model", api_mode)
    request_profile, request = runtime.clients[0].calls[0]
    assert request_profile == "secondary"
    assert request["model"] == "selected-session-model"
    assert request["messages"] == [{"role": "user", "content": "strict decision prompt"}]
    assert request["max_tokens"] == 123 and request["timeout"] == 1
    assert "tools" not in request
    if api_mode == "codex_responses":
        assert request["extra_body"] == {"reasoning": {"effort": "low", "enabled": True}}
    else:
        assert not {"extra_body", "reasoning", "reasoning_effort", "thinking"}.intersection(request)
    assert runtime.clients[0].closed == 1
    assert runtime.profile.get() == "outside"


async def test_plain_transport_without_codex_adapter_keeps_request_defaults(runtime, monkeypatch):
    monkeypatch.delattr(sys.modules["agent.auxiliary_client"], "CodexAuxiliaryClient")
    await judge_participation(source(api_mode="chat_completions"), "decision", timeout=1)
    assert "extra_body" not in runtime.clients[0].calls[0][1]


async def test_concurrent_profiles_keep_selected_runtimes_independent(runtime):
    await asyncio.gather(
        judge_participation(source("a", "model-a", "anthropic_messages"), "one", timeout=1),
        judge_participation(source("b", "model-b", "chat_completions"), "two", timeout=1),
    )
    assert {(p, kw["model"]) for p, _, kw in runtime.routed} == {
        ("a", "model-a"), ("b", "model-b")}
    assert all(client.closed == 1 for client in runtime.clients)


@pytest.mark.parametrize("result", [
    {"choices": []},
    {"choices": [{"message": {"content": "", "tool_calls": [{"name": "terminal"}]}}]},
    {"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}]},
    {"choices": [{"message": {"content": "x" * 32_001}}]},
    {"choices": [{"message": {"content": None}}]},
])
async def test_invalid_or_tool_bearing_response_fails_without_replay(runtime, result):
    runtime.prepare = lambda client: setattr(client, "result", result)
    with pytest.raises(HermesDecisionError):
        await judge_participation(source(), "decision", timeout=1)
    assert len(runtime.clients[0].calls) == 1 and runtime.clients[0].closed == 1


async def test_provider_failure_is_sanitized_and_does_not_fallback(runtime):
    def prepare(client):
        def fail():
            raise RuntimeError("Authorization: test-secret")
        client.create_impl = fail
    runtime.prepare = prepare
    with pytest.raises(HermesDecisionError) as error:
        await judge_participation(source(), "decision", timeout=1)
    assert "test-secret" not in str(error.value)
    assert len(runtime.routed) == 1 and runtime.clients[0].closed == 1


async def test_router_cannot_substitute_another_model(runtime):
    runtime.wire_model = "fallback-model"
    with pytest.raises(HermesDecisionError, match="selected model"):
        await judge_participation(source(), "decision", timeout=1)
    assert not runtime.clients[0].calls and runtime.clients[0].closed == 1


async def test_router_cannot_substitute_another_endpoint(runtime):
    runtime.prepare = lambda client: setattr(client, "base_url", "https://different.test/v1")
    with pytest.raises(HermesDecisionError, match="selected endpoint"):
        await judge_participation(source(), "decision", timeout=1)
    assert not runtime.clients[0].calls and runtime.clients[0].closed == 1


async def test_missing_transport_cannot_silently_use_weaker_wire(runtime, monkeypatch):
    monkeypatch.setattr(sys.modules["agent.auxiliary_client"], "AnthropicAuxiliaryClient", type("Other", (), {}))
    with pytest.raises(HermesDecisionError, match="API format"):
        await judge_participation(source(api_mode="anthropic_messages"), "decision", timeout=1)
    assert not runtime.clients[0].calls and runtime.clients[0].closed == 1


async def test_concrete_provider_prefix_normalization_is_supported(runtime):
    runtime.wire_model = "bare-model"
    await judge_participation(source(model="custom/bare-model"), "decision", timeout=1)
    assert runtime.clients[0].calls[0][1]["model"] == "bare-model"


async def test_deadline_cancels_and_closes_active_client(runtime):
    started = threading.Event()
    released = threading.Event()
    original_close = None

    def prepare(client):
        nonlocal original_close
        original_close = client.close

        def blocked():
            started.set()
            released.wait(2)
            return client.result

        def close():
            original_close()
            released.set()

        client.create_impl = blocked
        client.close = close

    runtime.prepare = prepare
    with pytest.raises(TimeoutError):
        await judge_participation(source(), "decision", timeout=.03)
    assert started.is_set()
    await asyncio.wait_for(asyncio.to_thread(released.wait), timeout=1)
    assert runtime.clients[0].closed == 1


@pytest.mark.parametrize("provider", ["auto", "moa", "copilot-acp"])
async def test_unsupported_provider_does_not_route(runtime, provider):
    current = source()
    current.provider = provider
    with pytest.raises(HermesDecisionError):
        await judge_participation(current, "decision", timeout=1)
    assert not runtime.routed


@pytest.mark.parametrize("kwargs", [{"timeout": 0}, {"timeout": float("nan")},
                                   {"timeout": 1, "max_tokens": 0}])
async def test_invalid_budgets_are_rejected_before_runtime_access(runtime, kwargs):
    with pytest.raises(HermesDecisionError):
        await judge_participation(source(), "decision", **kwargs)
    assert not runtime.configs
