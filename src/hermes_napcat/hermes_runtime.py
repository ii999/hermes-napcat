"""A nonpersistent, tool-free admission call using the live Hermes session runtime."""
from __future__ import annotations

import asyncio
import logging
import math
import threading
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
_MAX_PROMPT_CHARS = 80_000
_MAX_RESPONSE_CHARS = 32_000


class HermesDecisionError(RuntimeError):
    """The current Hermes runtime could not provide a safe admission result."""


def _field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _decision_text(response: Any) -> str:
    choices = _field(response, "choices")
    if not isinstance(choices, (list, tuple)) or len(choices) != 1:
        raise HermesDecisionError("Hermes admission returned an invalid response")
    message = _field(choices[0], "message")
    if _field(message, "tool_calls") or _field(message, "function_call"):
        raise HermesDecisionError("Hermes admission attempted a tool call")
    if _field(choices[0], "finish_reason") in {"length", "tool_calls", "function_call"}:
        raise HermesDecisionError("Hermes admission returned an incomplete response")
    content = _field(message, "content")
    if not isinstance(content, str) or not content.strip() or len(content) > _MAX_RESPONSE_CHARS:
        raise HermesDecisionError("Hermes admission returned invalid decision text")
    return content


def _same_wire_model(selected: str, resolved: str, provider: str) -> bool:
    # Hermes strips a concrete provider prefix for transports that use bare model IDs.
    return resolved == selected or selected == f"{provider}/{resolved}"


def _same_endpoint(selected: Any, actual: Any) -> bool:
    if not selected:
        return True  # Some native providers resolve their profile endpoint internally.
    left, right = urlsplit(str(selected)), urlsplit(str(actual or ""))
    return (left.scheme, left.hostname, left.port, left.path.rstrip("/")) == (
        right.scheme, right.hostname, right.port, right.path.rstrip("/"))


async def judge_participation(
    source: Any, prompt: str, *, timeout: float, max_tokens: int = 800,
) -> str:
    """Return decision text without an Agent, history, tool dispatch, or provider fallback.

    Only the live profile/session runtime is accepted. The caller owns decision validation.
    The outer deadline includes runtime/client resolution and executor waiting; the owned
    provider client is closed on completion, error, timeout, or cancellation.
    """
    if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > _MAX_PROMPT_CHARS
            or not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not math.isfinite(timeout) or timeout <= 0
            or not isinstance(max_tokens, int) or isinstance(max_tokens, bool)
            or not 1 <= max_tokens <= 4096):
        raise HermesDecisionError("Hermes admission input exceeds its limits")

    try:
        from gateway.run import _gateway_runner_ref, _load_gateway_config
        from agent import auxiliary_client
        resolve_provider_client = auxiliary_client.resolve_provider_client

        runner = _gateway_runner_ref()
        if runner is None:
            raise HermesDecisionError("Hermes gateway is not running")
        for name in ("_profile_scope_for_source", "_resolve_session_agent_runtime",
                     "_run_in_executor_with_context"):
            if not callable(getattr(runner, name, None)):
                raise HermesDecisionError("Hermes admission runtime contract is unavailable")
    except HermesDecisionError:
        raise
    except Exception as exc:
        log.warning("Hermes admission runtime is unavailable (%s)", type(exc).__name__)
        raise HermesDecisionError("Hermes admission runtime is unavailable") from None

    # Closing can race with a cancelled executor worker. Ownership is transferred exactly
    # once, and a late client is immediately closed by the worker after cancellation.
    lock = threading.Lock()
    owned: list[Any] = []
    cancelled = threading.Event()

    def close_owned() -> None:
        with lock:
            client = owned.pop() if owned else None
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                log.warning("Could not close Hermes admission client (%s)", type(exc).__name__)

    def call() -> str:
        try:
            # Profile scope lives in the worker too: resolution may access scoped secrets.
            with runner._profile_scope_for_source(source):
                config = _load_gateway_config()
                model, runtime = runner._resolve_session_agent_runtime(
                    source=source, user_config=config)
                provider = runtime.get("provider")
                if (not isinstance(model, str) or not model.strip()
                        or not isinstance(provider, str) or not provider.strip()
                        or provider.lower() in {"auto", "moa", "copilot-acp"}
                        or str(runtime.get("base_url") or "").startswith(("acp:", "moa:"))):
                    raise HermesDecisionError("Hermes admission requires a concrete model provider")
                if cancelled.is_set():
                    raise HermesDecisionError("Hermes admission was cancelled")
                client, wire_model = resolve_provider_client(
                    provider, model=model, async_mode=False,
                    explicit_base_url=runtime.get("base_url"),
                    explicit_api_key=runtime.get("api_key"), api_mode=runtime.get("api_mode"),
                    main_runtime={**runtime, "model": model},
                )
                if client is not None:
                    with lock:
                        owned.append(client)
                if (client is None or not isinstance(wire_model, str)
                        or not _same_wire_model(model, wire_model, provider)):
                    raise HermesDecisionError("Hermes admission could not retain the selected model")
                if not callable(getattr(client, "close", None)):
                    raise HermesDecisionError("Hermes admission provider has no cleanup contract")
                if not _same_endpoint(runtime.get("base_url"), getattr(client, "base_url", None)):
                    raise HermesDecisionError("Hermes admission could not retain the selected endpoint")
                # The router can fall back to OpenAI wire when an Anthropic SDK is
                # missing. Such a weaker transport is not an admission fallback.
                expected_transport = {
                    "anthropic_messages": "AnthropicAuxiliaryClient",
                    "codex_responses": "CodexAuxiliaryClient",
                }.get(runtime.get("api_mode"))
                if expected_transport:
                    transport = getattr(auxiliary_client, expected_transport, None)
                    if not isinstance(transport, type) or not isinstance(client, transport):
                        raise HermesDecisionError("Hermes admission could not retain the selected API format")
                if cancelled.is_set():
                    raise HermesDecisionError("Hermes admission was cancelled")
                # No auxiliary task routing, fallback ladder, request overrides, or tools.
                response = client.chat.completions.create(
                    model=wire_model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=max_tokens, timeout=timeout,
                )
                return _decision_text(response)
        except HermesDecisionError:
            raise
        except Exception as exc:
            # Provider exception text can contain credentials or full request bodies.
            log.warning("Hermes admission failed (%s)", type(exc).__name__)
            raise HermesDecisionError("Hermes admission provider request failed") from None
        finally:
            close_owned()

    try:
        with runner._profile_scope_for_source(source):
            async with asyncio.timeout(timeout):
                return await runner._run_in_executor_with_context(call)
    finally:
        cancelled.set()
        # Most calls already closed in the worker. Cancellation closes a still-active
        # client without extending the caller's deadline on blocking provider cleanup.
        if owned:
            cleanup = asyncio.create_task(asyncio.to_thread(close_owned))
            cleanup.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
