from __future__ import annotations

import base64
import os
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from hermes_napcat.config import MediaSettings
from hermes_napcat.media import MediaError, MediaStore, SafeResolver

# An actual 1x1 PNG; this test does not use an external image service.
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9f8AAAAASUVORK5CYII=")


@asynccontextmanager
async def file_server():
    async def serve(request):
        assert "Authorization" not in request.headers
        route = request.match_info["route"]
        if route == "redirect-private":
            raise web.HTTPFound("http://127.0.0.1:1/secret")
        if route == "loop":
            raise web.HTTPFound("/loop")
        if route == "huge":
            return web.Response(body=b"x" * 3000, content_type="image/png")
        if route == "chunked":
            response = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
            await response.prepare(request)
            await response.write(b"x" * 1100)
            await response.write_eof()
            return response
        if route == "fake":
            return web.Response(body=b"<script>secret</script>", content_type="image/png")
        if route == "gzip":
            return web.Response(body=PNG, headers={"Content-Encoding": "gzip", "Content-Type": "image/png"})
        return web.Response(body=PNG, content_type="image/png")
    app = web.Application()
    app.router.add_get("/{route}", serve)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://127.0.0.1/a", "http://169.254.169.254/latest/meta-data",
    "http://localhost/a", "http://gchat.qpic.cn.evil.example/a", "https://x@gchat.qpic.cn/a",
    "https://gchat.qpic.cn/a#fragment", "https://gchat.qpic.cn/a b", "http://[::1]/a",
])
def test_media_url_rejections(tmp_path, url):
    store = MediaStore(MediaSettings(), tmp_path / "cache")
    with pytest.raises(MediaError):
        store.validate_url(url)


async def test_dns_rebinding_and_mixed_addresses_are_rejected():
    resolver = SafeResolver(set())
    resolver._resolver.resolve = AsyncMock(return_value=[{"host": "93.184.216.34"}, {"host": "10.1.2.3"}])
    try:
        with pytest.raises(MediaError):
            await resolver.resolve("allowed.example", 443)
        resolver._resolver.resolve = AsyncMock(return_value=[{"host": "93.184.216.34"}])
        assert len(await resolver.resolve("allowed.example", 443)) == 1
    finally:
        await resolver.close()


async def test_local_media_origin_download_and_permissions(tmp_path):
    async with file_server() as origin:
        store = MediaStore(MediaSettings(trusted_private_origins=[origin], max_bytes=1024), tmp_path / "cache")
        try:
            data = await store.download(origin + "/png", kind="image")
            assert data.path.read_bytes() == PNG
            assert data.mime == "image/png" and data.size == len(PNG)
            assert data.path.stat().st_mode & 0o777 == 0o600
            assert not list(store.root.glob("*.part"))
        finally:
            await store.close()


@pytest.mark.parametrize("route", ["redirect-private", "loop", "huge", "chunked", "fake", "gzip"])
async def test_download_bounds_redirect_checks_and_cleanup(tmp_path, route):
    async with file_server() as origin:
        store = MediaStore(MediaSettings(trusted_private_origins=[origin], max_bytes=1024), tmp_path / "cache")
        try:
            with pytest.raises(MediaError):
                await store.download(origin + "/" + route, kind="image")
            assert not list(store.root.iterdir())
        finally:
            await store.close()


def test_cache_prune_only_deletes_owned_expired_files(tmp_path):
    store = MediaStore(MediaSettings(), tmp_path / "cache")
    user_file = store.root / "keep.txt"
    user_file.write_bytes(b"keep")
    old_file = store.root / ("napcat_" + "a" * 32 + ".png")
    old_file.write_bytes(PNG)
    old = time.time() - 90000
    for path in [user_file, old_file]:
        os.utime(path, (old, old))
    assert store._prune_and_usage() == 0
    assert user_file.read_bytes() == b"keep" and not old_file.exists()


async def test_cache_quota_refuses_new_download_without_deleting_recent(tmp_path):
    store = MediaStore(MediaSettings(max_bytes=1024, cache_max_bytes=1024), tmp_path / "cache")
    current = store.root / ("napcat_" + "b" * 32 + ".png")
    current.write_bytes(PNG)
    with pytest.raises(MediaError, match="quota"):
        await store.download("https://gchat.qpic.cn/a", kind="image")
    assert current.read_bytes() == PNG
    assert store._session is None


def test_outbound_roots_symlinks_and_shared_mapping(tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    inside = root / "image.png"
    inside.write_bytes(PNG)
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    link = root / "escape"
    link.symlink_to(outside)
    store = MediaStore(MediaSettings(outbound_roots=[root]), tmp_path / "cache")
    assert base64.b64decode(store.outbound_reference(str(inside)).removeprefix("base64://")) == PNG
    for path in [outside, link, root / ".." / "secret.txt"]:
        with pytest.raises(MediaError):
            store.outbound_reference(str(path))
    mapped = MediaStore(MediaSettings(shared_paths=[{"hermes": root, "napcat": "D:/qq-share"}]), tmp_path / "cache2")
    assert mapped.shared_path(str(inside)) == "D:/qq-share/image.png"
    assert mapped.outbound_reference(str(inside)) == "file:///D:/qq-share/image.png"
    inside.write_bytes(b"x" * 1100)
    small = MediaStore(MediaSettings(outbound_roots=[root], inline_max_bytes=1024), tmp_path / "cache3")
    with pytest.raises(MediaError, match="inline"):
        small.outbound_reference(str(inside))
