from __future__ import annotations

import asyncio
import base64
import stat
from contextlib import aclosing

import pytest

from hermes_napcat.media import MediaError, MediaStore
from hermes_napcat.stream_download import (
    StreamDownloadError,
    StreamDownloader,
    StreamDownloadUnsupported,
    StreamFileUnavailable,
)
from hermes_napcat.transport import NotConnected, OneBotTransport, StreamLimitError
from test_transport import fake_napcat, ignore, wait_until

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 900


def stream_packets(request, content=PNG, *, name="../../image.png", declared=None):
    chunk_size = request["params"]["chunk_size"]
    info = {"type": "stream", "data_type": "file_info", "file_name": name,
            "file_size": len(content) if declared is None else declared, "chunk_size": chunk_size}
    if request["action"] == "download_file_record_stream":
        info["out_format"] = "mp3"
    packets = [info]
    for index, start in enumerate(range(0, len(content), chunk_size)):
        chunk = content[start:start + chunk_size]
        encoded = base64.b64encode(chunk).decode("ascii")
        packets.append({"type": "stream", "data_type": "file_chunk", "index": index,
                        "data": encoded, "size": len(chunk), "base64_size": len(encoded),
                        "progress": round(min(start + chunk_size, len(content)) / len(content) * 100)})
    packets.append({"type": "response", "data_type": "file_complete",
                    "total_chunks": len(packets) - 1, "total_bytes": len(content),
                    "message": "Download completed"})
    return packets


async def send_packet(ws, request, data, **changes):
    await ws.send_json({"status": "ok", "retcode": 0, "stream": "stream-action",
                        "data": data, "echo": request["echo"], **changes})


async def test_multipacket_download_interleaves_single_responses(settings, tmp_path):
    async def callback(ws, request):
        if request["action"] == "get_status":
            await ws.send_json({"status": "ok", "retcode": 0, "data": {"online": True},
                                "echo": request["echo"]})
            return
        for packet in stream_packets(request):
            await send_packet(ws, request, packet)
            await asyncio.sleep(0.005)

    async with fake_napcat(callback) as server:
        config = settings(ws_url=server["url"], ws_max_bytes=1024)
        store = MediaStore(config.media, tmp_path / "cache")
        transport = OneBotTransport(config, ignore)
        try:
            await transport.start()
            downloader = StreamDownloader(transport, config, store)
            download = asyncio.create_task(downloader.download("opaque_id", kind="image"))
            await wait_until(lambda: transport.pending_count == 1)
            assert await transport.call("get_status") == {"online": True}
            result = await download
            assert result.path.parent == store.root and result.path.read_bytes() == PNG
            assert result.mime == "image/png" and result.size == len(PNG)
            assert stat.S_IMODE(result.path.stat().st_mode) == 0o600
            assert len(list(store.root.iterdir())) == 1
            assert transport.pending_count == 0 and store._reserved_bytes == 0
        finally:
            await transport.stop()
            await store.close()


@pytest.mark.parametrize("fault", [
    "missing_info", "repeated_info", "bad_index", "bad_base64", "noncanonical_base64",
    "bad_base64_size", "oversized_chunk", "bad_total", "wrong_declared", "wrong_stream",
    "invalid_content", "empty", "wrong_retcode",
])
async def test_malformed_download_never_publishes_or_falls_back(settings, tmp_path, fault):
    async def callback(ws, request):
        content = b"not an image" if fault == "invalid_content" else PNG
        packets = stream_packets(request, content)
        if fault == "missing_info":
            packets.pop(0)
        elif fault == "repeated_info":
            packets.insert(1, packets[0])
        elif fault == "bad_index":
            packets[1]["index"] = 1
        elif fault == "bad_base64":
            packets[1]["data"] = "!" + packets[1]["data"][1:]
        elif fault == "noncanonical_base64":
            # One byte has canonical encoding eA==; nonzero unused bits must fail.
            packets = stream_packets(request, b"x")
            packets[1]["data"] = "eB=="
        elif fault == "bad_base64_size":
            packets[1]["base64_size"] += 4
        elif fault == "oversized_chunk":
            packets[1]["size"] = request["params"]["chunk_size"] + 1
        elif fault == "bad_total":
            packets[-1]["total_bytes"] += 1
        elif fault == "wrong_declared":
            packets[0]["file_size"] += 1
        elif fault == "empty":
            packets = [packets[0], {"type": "response", "data_type": "file_complete",
                                     "total_chunks": 0, "total_bytes": 0}]
        for packet in packets:
            await send_packet(ws, request, packet,
                              **({"stream": "normal-action"} if fault == "wrong_stream" else
                                 {"retcode": True} if fault == "wrong_retcode" else {}))
            await asyncio.sleep(0)

    async with fake_napcat(callback) as server:
        config = settings(ws_url=server["url"], ws_max_bytes=1024)
        store = MediaStore(config.media, tmp_path / "cache")
        transport = OneBotTransport(config, ignore)
        try:
            await transport.start()
            with pytest.raises(MediaError) as failure:
                await StreamDownloader(transport, config, store).download("opaque_id", kind="image")
            assert not isinstance(failure.value, (StreamDownloadUnsupported, StreamFileUnavailable))
            assert not list(store.root.iterdir())
            assert transport.pending_count == 0 and store._reserved_bytes == 0
            assert len([r for r in server["requests"] if r["action"] != "get_login_info"]) == 1
        finally:
            await transport.stop()


@pytest.mark.parametrize("kind,content,name,mime,suffix", [
    ("record", b"ID3" + b"x" * 600, "../original.silk", "audio/mpeg", ".mp3"),
    ("file", b"PK\x03\x04" + b"x" * 600, "../../report.docx", "application/octet-stream", ".docx"),
    ("file", b"%PDF-1.7\n" + b"x" * 600, "D:\\unsafe\\file.exe", "application/pdf", ".pdf"),
    ("video", b"\x00\x00\x00\x18ftypisom" + b"x" * 600, "/unsafe/movie.mp4", "video/mp4", ".mp4"),
])
async def test_typed_content_and_safe_document_suffix(settings, tmp_path, kind, content, name, mime, suffix):
    async def callback(ws, request):
        assert request["params"]["file"] == "opaque_id"
        if kind == "record":
            assert request["params"]["out_format"] == "mp3"
        for packet in stream_packets(request, content, name=name,
                                     declared=20_000 if kind == "record" else None):
            await send_packet(ws, request, packet)
            await asyncio.sleep(0)

    async with fake_napcat(callback) as server:
        config = settings(ws_url=server["url"], ws_max_bytes=1024,
                          media={"max_bytes": 1024, "cache_max_bytes": 1024})
        store = MediaStore(config.media, tmp_path / "cache")
        transport = OneBotTransport(config, ignore)
        try:
            await transport.start()
            result = await StreamDownloader(transport, config, store).download("opaque_id", kind=kind)
            assert result.path.parent == store.root and result.path.read_bytes() == content
            assert result.mime == mime and result.path.suffix == suffix
        finally:
            await transport.stop()


@pytest.mark.parametrize("cause", ["timeout", "cancel", "disconnect", "access"])
async def test_interrupted_stream_cleans_pending_and_partial_cache(settings, tmp_path, cause):
    ready = asyncio.Event()
    proceed = asyncio.Event()
    allowed = True

    def access_check():
        if not allowed:
            raise MediaError("access revoked")

    async def callback(ws, request):
        packets = stream_packets(request)
        await send_packet(ws, request, packets[0])
        await send_packet(ws, request, packets[1])
        ready.set()
        await proceed.wait()
        if cause == "disconnect":
            await ws.close()
        elif cause == "access":
            for packet in packets[2:]:
                await send_packet(ws, request, packet)

    async with fake_napcat(callback) as server:
        config = settings(ws_url=server["url"], ws_max_bytes=1024,
                          media={"timeout": 0.2}, reconnect_min=0.5, reconnect_max=0.5)
        store = MediaStore(config.media, tmp_path / "cache")
        transport = OneBotTransport(config, ignore)
        try:
            await transport.start()
            task = asyncio.create_task(StreamDownloader(transport, config, store).download(
                "opaque_id", kind="image", access_check=access_check))
            await ready.wait()
            await wait_until(lambda: any(path.stat().st_size or path.name.endswith(".part")
                                        for path in store.root.iterdir()))
            if cause == "cancel":
                task.cancel()
            elif cause == "access":
                allowed = False
            proceed.set()
            with pytest.raises(asyncio.CancelledError if cause == "cancel" else MediaError):
                await task
            assert not list(store.root.iterdir())
            assert transport.pending_count == 0 and store._reserved_bytes == 0
        finally:
            proceed.set()
            await transport.stop()


@pytest.mark.parametrize("code,stream,message,error", [
    (1404, "normal-action", "unknown API", StreamDownloadUnsupported),
    (1200, "stream-action", "Download failed: file not found", StreamFileUnavailable),
    (1401, "normal-action", "permission denied", StreamDownloadError),
    (1404, "stream-action", "resource unavailable", StreamDownloadError),
    (1200, "stream-action", "Download failed: private error", StreamDownloadError),
])
async def test_only_explicit_unsupported_or_missing_file_can_fallback(settings, tmp_path, code, stream, message, error):
    async def callback(ws, request):
        await send_packet(ws, request, None, status="failed", retcode=code,
                          stream=stream, message=message)

    async with fake_napcat(callback) as server:
        config = settings(ws_url=server["url"])
        store = MediaStore(config.media, tmp_path / "cache")
        transport = OneBotTransport(config, ignore)
        try:
            await transport.start()
            with pytest.raises(error):
                await StreamDownloader(transport, config, store).download("opaque_id", kind="image")
            assert transport.pending_count == 0 and not list(store.root.iterdir())
        finally:
            await transport.stop()


@pytest.mark.parametrize("limit", ["frames", "bytes"])
async def test_transport_stream_admission_limits(settings, limit):
    async def callback(ws, request):
        for index in range(8):
            await send_packet(ws, request, {"type": "stream", "data_type": "chunk",
                                            "data": "x" * 256, "index": index})

    async with fake_napcat(callback) as server:
        transport = OneBotTransport(settings(ws_url=server["url"]), ignore)
        try:
            await transport.start()
            responses = transport.stream_call("download_file_stream", {}, timeout=1,
                                              max_frames=2 if limit == "frames" else 10,
                                              max_buffer_bytes=200 if limit == "bytes" else 10_000)
            async with aclosing(responses):
                with pytest.raises(StreamLimitError):
                    async for _ in responses:
                        await asyncio.sleep(0.01)
            assert transport.pending_count == 0
            assert await transport.call("get_login_info") == {"user_id": 100}
        finally:
            await transport.stop()


async def test_epoch_and_quota_denial_happen_before_download_write(settings, tmp_path):
    async with fake_napcat() as server:
        config = settings(ws_url=server["url"], media={"max_bytes": 1024, "cache_max_bytes": 1024})
        store = MediaStore(config.media, tmp_path / "cache")
        transport = OneBotTransport(config, ignore)
        try:
            await transport.start()
            with pytest.raises(NotConnected):
                async for _ in transport.stream_call("download_file_stream", {}, timeout=1,
                                                      max_frames=5, max_buffer_bytes=1024,
                                                      expected_epoch=transport.connection_epoch - 1):
                    pass
            store.import_inline("base64://" + base64.b64encode(PNG).decode("ascii"), kind="image")
            with pytest.raises(MediaError, match="quota"):
                await StreamDownloader(transport, config, store).download("opaque_id", kind="image")
            assert [request["action"] for request in server["requests"]] == ["get_login_info"]
        finally:
            await transport.stop()


async def test_remaining_turn_budget_is_enforced_during_receive(settings, tmp_path):
    async def callback(ws, request):
        for packet in stream_packets(request):
            await send_packet(ws, request, packet)

    async with fake_napcat(callback) as server:
        config = settings(ws_url=server["url"], ws_max_bytes=1024)
        store = MediaStore(config.media, tmp_path / "cache")
        transport = OneBotTransport(config, ignore)
        try:
            await transport.start()
            with pytest.raises(MediaError):
                await StreamDownloader(transport, config, store).download("opaque_id", kind="image", max_bytes=200)
            assert not list(store.root.iterdir()) and store._reserved_bytes == 0
        finally:
            await transport.stop()


async def test_converted_record_actual_bytes_are_bounded(settings, tmp_path):
    async def callback(ws, request):
        content = b"ID3" + b"x" * 1400
        for packet in stream_packets(request, content, declared=20):
            await send_packet(ws, request, packet)
            await asyncio.sleep(0.005)

    async with fake_napcat(callback) as server:
        config = settings(ws_url=server["url"], ws_max_bytes=1024,
                          media={"max_bytes": 1024, "cache_max_bytes": 1024})
        store = MediaStore(config.media, tmp_path / "cache")
        transport = OneBotTransport(config, ignore)
        try:
            await transport.start()
            with pytest.raises(MediaError):
                await StreamDownloader(transport, config, store).download("opaque_id", kind="record")
            assert transport.pending_count == 0 and not list(store.root.iterdir())
            assert store._reserved_bytes == 0
        finally:
            await transport.stop()


async def test_old_echo_after_reconnect_cannot_complete_new_stream(settings, tmp_path):
    old_echo = None
    calls = 0

    async def callback(ws, request):
        nonlocal old_echo, calls
        calls += 1
        if calls == 1:
            old_echo = request["echo"]
            await send_packet(ws, request, stream_packets(request)[0])
            await ws.close()
            return
        assert request["echo"] != old_echo
        await ws.send_json({"status": "ok", "retcode": 0, "stream": "stream-action",
                            "echo": old_echo, "data": {"type": "response", "data_type": "file_complete",
                                                        "total_chunks": 1, "total_bytes": 1}})
        for packet in stream_packets(request):
            await send_packet(ws, request, packet)

    async with fake_napcat(callback) as server:
        config = settings(ws_url=server["url"], ws_max_bytes=1024)
        store = MediaStore(config.media, tmp_path / "cache")
        transport = OneBotTransport(config, ignore)
        try:
            await transport.start()
            downloader = StreamDownloader(transport, config, store)
            with pytest.raises(StreamDownloadError):
                await downloader.download("opaque_id", kind="image")
            assert not list(store.root.iterdir())
            await wait_until(lambda: transport.stats.connections >= 2)
            result = await downloader.download("other_id", kind="image")
            assert result.path.read_bytes() == PNG
            assert transport.stats.late_responses >= 1 and transport.pending_count == 0
        finally:
            await transport.stop()


async def test_closing_transport_iterator_releases_echo(settings):
    async def callback(ws, request):
        await send_packet(ws, request, {"type": "stream", "data_type": "file_info"})

    async with fake_napcat(callback) as server:
        transport = OneBotTransport(settings(ws_url=server["url"]), ignore)
        try:
            await transport.start()
            async with aclosing(transport.stream_call("download_file_stream", {}, timeout=1,
                                                      max_frames=5, max_buffer_bytes=1024)) as responses:
                assert (await anext(responses))["data_type"] == "file_info"
                assert transport.pending_count == 1
            assert transport.pending_count == 0
        finally:
            await transport.stop()


@pytest.mark.parametrize("enabled", [True, False])
async def test_file_notices_use_normal_authenticated_event_lane(settings, enabled):
    received = []

    async def handler(event):
        received.append(event)

    async with fake_napcat() as server:
        config = settings(ws_url=server["url"], media={"enabled": enabled})
        transport = OneBotTransport(config, handler)
        try:
            await transport.start()
            for kind in ("group_upload", "offline_file"):
                await server["ws"].send_json({"post_type": "notice", "notice_type": kind,
                                              "self_id": 100, "group_id": 300, "user_id": 200,
                                              "file": {"id": "opaque_id", "name": "report.pdf"}})
            await server["ws"].send_json({"post_type": "notice", "notice_type": "group_upload",
                                          "self_id": 999, "group_id": 300, "user_id": 200})
            if enabled:
                await wait_until(lambda: len(received) == 2)
                assert all("file" in event for event in received)
            else:
                await asyncio.sleep(0.03)
                assert not received
            assert transport._recalls.empty()
        finally:
            await transport.stop()
