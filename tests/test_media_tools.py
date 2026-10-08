"""Gateway/tool contract fixtures; these do not execute a real Hermes model."""
from __future__ import annotations

import base64
import asyncio
import contextvars
import json
import sys
import time
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_napcat import tools
from hermes_napcat.media import Downloaded
from hermes_napcat.protocol import Incoming, Target
from test_media import PNG
from test_tools import bind_session, make_adapter
from test_media_pipeline import adapter_for, event, part


@pytest.mark.parametrize("cross_loop", [False, True])
async def test_cancelled_read_releases_import_and_allows_next_read(
    monkeypatch, hermes_doubles, settings, tmp_path, cross_loop,
):
    adapter = adapter_for(hermes_doubles, settings, tmp_path, download_mode="stream")
    adapter.settings = adapter.settings.model_copy(update={
        "qq_tools": adapter.settings.qq_tools.model_copy(update={"read_enabled": True})})
    bind_session(monkeypatch, adapter)
    incoming = Incoming.parse(event(parts=[part("image", "opaque-id")]))
    ref = adapter.media_refs.remember(incoming)[0]
    started, cleaned, release = threading.Event(), asyncio.Event(), asyncio.Event()

    async def transfer(*args, **kwargs):
        async def chunks():
            yield PNG[:8]
            started.set()
            await release.wait()
            yield PNG[8:]
        try:
            return await adapter.media.import_stream(chunks(), kind="image")
        finally:
            cleaned.set()

    adapter._stream_downloader = SimpleNamespace(download=transfer)

    async def caller():
        request = asyncio.create_task(tools.qq_get_media({"media_id": ref.media_id}))
        assert await asyncio.to_thread(started.wait, 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

    if cross_loop:
        await asyncio.to_thread(lambda: asyncio.run(caller()))
    else:
        await caller()
    await asyncio.wait_for(cleaned.wait(), 1)
    assert not list(adapter.media.root.glob("*.part"))
    assert adapter.media._reserved_bytes == 0
    release.set()
    result = json.loads(await tools.qq_get_media({"media_id": ref.media_id}))
    assert result["success"]


@pytest.mark.parametrize("cross_loop", [False, True])
async def test_cancelled_send_retains_inflight_delivery(
    monkeypatch, hermes_doubles, settings, tmp_path, cross_loop,
):
    adapter, _ = make_adapter(hermes_doubles, settings, tmp_path)
    bind_session(monkeypatch, adapter)
    started, release, delivered = threading.Event(), asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        started.set()
        await release.wait()
        delivered.set()
        return {"message_id": 77}

    adapter.transport.call = AsyncMock(side_effect=send)

    async def caller():
        request = asyncio.create_task(tools.qq_send_message({"text": "hello"}))
        assert await asyncio.to_thread(started.wait, 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

    if cross_loop:
        await asyncio.to_thread(lambda: asyncio.run(caller()))
    else:
        await caller()
    assert not delivered.is_set()
    release.set()
    await asyncio.wait_for(delivered.wait(), 1)
    await asyncio.gather(*adapter._agent_actions.values())
    adapter.transport.call.assert_awaited_once()


def media_session(monkeypatch, hermes_doubles, settings, tmp_path):
    adapter, _ = make_adapter(
        hermes_doubles, settings, tmp_path,
        qq_tools={"read_enabled": True}, media={"references": {"enabled": True}})
    bind_session(monkeypatch, adapter)
    incoming = Incoming.parse({
        "post_type": "message", "message_type": "group", "self_id": 100,
        "group_id": 300, "user_id": 200, "message_id": 11, "time": time.time(),
        "message": [{"type": "image", "data": {"file": "safe-image-id"}}],
    })
    ref = adapter.media_refs.remember(incoming)[0]
    cached = adapter.media.import_inline("base64://" + base64.b64encode(PNG).decode(), kind="image")
    adapter.media_refs.set_downloaded(ref.media_id, incoming.target, cached)
    return adapter, ref, cached


def registry_fixture(monkeypatch, dispatch, available=True):
    module = ModuleType("tools.registry")
    module.registry = SimpleNamespace(
        get_definitions=lambda names, quiet=False: [{"name": next(iter(names))}] if available else [],
        dispatch=dispatch,
    )
    monkeypatch.setitem(sys.modules, "tools.registry", module)


async def test_read_only_permission_preserves_native_multimodal_and_call_context(
    monkeypatch, hermes_doubles, settings, tmp_path,
):
    adapter, ref, cached = media_session(monkeypatch, hermes_doubles, settings, tmp_path)
    profile = contextvars.ContextVar("test_profile", default="wrong")
    profile.set("active-profile")
    native = {"_multimodal": True, "content": [{"type": "text", "text": "image pixels"}],
              "text_summary": "QQ image"}
    calls = []

    def dispatch(name, args, **kwargs):
        calls.append((name, args, kwargs, profile.get()))
        return native

    registry_fixture(monkeypatch, dispatch)
    result = await tools.qq_read_media({"media_id": ref.media_id, "question": "Compare the colors"},
                                       task_id="task-9")
    assert result == native
    assert calls == [("vision_analyze", {"image_url": str(cached.path), "question": "Compare the colors"},
                      {"task_id": "task-9"}, "active-profile")]
    denied = json.loads(await tools.qq_send_message({"text": "must not send"}))
    assert not denied["success"]
    adapter.transport.call.assert_not_called()


async def test_media_read_rechecks_recall_after_analysis(
    monkeypatch, hermes_doubles, settings, tmp_path,
):
    from hermes_napcat import media_understanding

    adapter, ref, _ = media_session(monkeypatch, hermes_doubles, settings, tmp_path)

    async def analyze(*args, **kwargs):
        adapter.media_refs.recall(Target.parse("group:300"), "11")
        return {"_multimodal": True, "content": [{"type": "text", "text": "must not escape"}]}

    monkeypatch.setattr(media_understanding, "read_media", analyze)
    result = json.loads(await tools.qq_read_media({"media_id": ref.media_id, "question": "Read it"}))
    assert not result["success"] and "content" not in result


async def test_unavailable_analysis_is_not_reported_as_understanding(
    monkeypatch, hermes_doubles, settings, tmp_path,
):
    _, ref, _ = media_session(monkeypatch, hermes_doubles, settings, tmp_path)
    registry_fixture(monkeypatch, lambda *_a, **_k: None, available=False)
    result = json.loads(await tools.qq_read_media({"media_id": ref.media_id, "question": "Read it"}))
    assert not result["success"] and "unavailable" in result["error"]


async def test_document_read_uses_visible_path_and_bounded_pagination(monkeypatch, hermes_doubles, tmp_path):
    from hermes_napcat.media_understanding import read_media

    path = tmp_path / "controlled.pdf"
    path.write_bytes(b"%PDF-content")
    monkeypatch.setattr(sys.modules["tools.credential_files"], "to_agent_visible_cache_path",
                        lambda _path: "/root/.hermes/cache/documents/napcat/controlled.pdf")
    calls = []

    def dispatch(name, args, **kwargs):
        calls.append((name, args))
        return json.dumps({"success": True, "content": "a" * 20000, "next_offset": 999})

    registry_fixture(monkeypatch, dispatch)
    result = await read_media(Downloaded(path, "application/pdf", path.stat().st_size),
                              kind="file", question="Read the introduction", task_id="task",
                              offset=8, limit=30)
    assert calls == [("read_file", {"path": "/root/.hermes/cache/documents/napcat/controlled.pdf",
                                   "offset": 8, "limit": 30})]
    assert len(result["content"]) == 16000 and result["truncated"]
    assert "next_offset" not in result


async def test_transcription_local_recovery_is_explicit(monkeypatch, hermes_doubles, tmp_path):
    from hermes_napcat.media_understanding import read_media

    module = ModuleType("tools.transcription_tools")
    module.transcribe_audio = lambda path, source: {"success": False, "error": "provider unavailable"}
    module.transcribe_audio_local_fallback = lambda path: {
        "success": True, "transcript": "hello", "provider": "local"}
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", module)
    result = await read_media(Downloaded(tmp_path / "audio.mp3", "audio/mpeg", 50),
                              kind="record", question="Transcribe", task_id="task")
    assert result["success"] and result["transcript"] == "hello"
    assert result["fallback"] and result["method"] == "transcription"


async def test_get_media_translates_cache_path(monkeypatch, hermes_doubles, settings, tmp_path):
    _, ref, _ = media_session(monkeypatch, hermes_doubles, settings, tmp_path)
    monkeypatch.setattr(sys.modules["tools.credential_files"], "to_agent_visible_cache_path",
                        lambda _path: "~/.hermes/cache/documents/napcat/image.png")
    result = json.loads(await tools.qq_get_media({"media_id": ref.media_id}))
    assert result["success"] and result["path"].startswith("~/.hermes/cache/documents/")
