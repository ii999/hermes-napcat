"""Media capability tests with controlled transfers; these are not real QQ integration tests."""
from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_napcat.context import GroupContext
from hermes_napcat.media import Downloaded, MediaError, MediaStore
from hermes_napcat.media_refs import MediaReferences, media_file_id
from hermes_napcat.policy import Policy
from hermes_napcat.protocol import Incoming, Target
from hermes_napcat.stream_download import StreamDownloadUnsupported, StreamFileUnavailable
from test_adapter import make_adapter
from test_group_context import config, event
from test_media import PNG


def part(kind, identifier=None, **data):
    return {"type": kind, "data": {"file": identifier or f"opaque.{kind}", **data}}


@pytest.mark.parametrize("mode,file_id", [("auto", "old-id"), ("http", "old-id"), ("auto", None)])
async def test_stale_http_refreshes_in_compatibility_paths(hermes_doubles, settings, tmp_path, mode, file_id):
    adapter = adapter_for(hermes_doubles, settings, tmp_path, download_mode=mode)
    data = {"url": "https://gchat.qpic.cn/old"}
    if file_id:
        data["file"] = file_id
    incoming = Incoming.parse(event(parts=[{"type": "image", "data": data}]))
    ref = adapter.media_refs.remember(incoming)[0]
    adapter._stream_downloader = SimpleNamespace(download=AsyncMock(
        side_effect=StreamDownloadUnsupported("explicit unsupported action")))
    result = cached(adapter.media)
    adapter.media.download = AsyncMock(side_effect=[MediaError("media server returned HTTP 403"), result])
    adapter.transport.call.return_value = event(parts=[
        part("image", "fresh-id", url="https://gchat.qpic.cn/fresh")])
    assert await adapter.resolve_media(ref.media_id, incoming.target, incoming.user_id) == result
    adapter.transport.call.assert_awaited_once_with("get_msg", {"message_id": 1})
    assert [call.args[0] for call in adapter.media.download.call_args_list] == [
        "https://gchat.qpic.cn/old", "https://gchat.qpic.cn/fresh"]


def cached(store, size=len(PNG), mime="image/png"):
    path = store.root / ("napcat_" + "a" * 32 + ".png")
    path.write_bytes(PNG + b"x" * max(0, size - len(PNG)))
    return Downloaded(path, mime, size)


def adapter_for(hermes_doubles, settings, tmp_path, **media):
    adapter = make_adapter(hermes_doubles, settings, media={
        "references": {"enabled": True}, "download_mode": "http", **media,
    })
    adapter.media = MediaStore(adapter.settings.media, tmp_path / "cache")
    return adapter


def test_all_kinds_have_scoped_handles_and_safe_locators():
    settings = config(media={"references": {"enabled": True}})
    store = MediaReferences("100", settings.media.references)
    incoming = Incoming.parse(event(parts=[part(kind) for kind in ("image", "record", "video", "file")]))
    refs = store.remember(incoming)
    assert [(ref.kind, ref.attachment_index) for ref in refs] == [
        ("image", 0), ("record", 1), ("video", 2), ("file", 3),
    ]
    assert all("file_id" not in ref.summary() and "url" not in ref.summary() for ref in refs)
    for ref in refs:
        with pytest.raises(MediaError):
            store.get(ref.media_id, Target.parse("private:200"))
    for unsafe in ("/etc/passwd", "../file", "D:\\secret", "file:///tmp/a", "base64://AA=="):
        assert media_file_id(unsafe) is None


def test_duplicate_enriches_locator_without_extending_ttl_or_changing_author(monkeypatch):
    settings = config(media={"references": {"enabled": True, "ttl_seconds": 30}})
    store = MediaReferences("100", settings.media.references)
    now = time.time()
    first = Incoming.parse(event(stamp=now, parts=[part("record", url="https://gchat.qpic.cn/old")]))
    original = store.remember(first)[0]
    richer = Incoming.parse(event(stamp=now + 5, parts=[part("record", "fresh", url="https://gchat.qpic.cn/new")]))
    enriched = store.remember(richer)[0]
    assert enriched.media_id == original.media_id and enriched.expires_at == original.expires_at
    assert enriched.file_id == "fresh" and enriched.url.endswith("/new")
    assert not store.remember(Incoming.parse(event(user=201, parts=[part("record", "other")])) )
    monkeypatch.setattr("hermes_napcat.media_refs.time.time", lambda: now + 31)
    assert not store.remember(Incoming.parse(event(stamp=now + 31, parts=[part("record", "fresh")])) )
    assert not store.for_message(first.target, first.message_id)


def test_context_summaries_remove_expired_handles_on_read(monkeypatch):
    settings = config(media={"references": {"enabled": True, "ttl_seconds": 30}})
    ctx = GroupContext(settings, Policy(settings))
    now = time.time()
    incoming = Incoming.parse(event(stamp=now, parts=[part("image"), part("file")]))
    assert ctx.put(incoming)
    record = ctx.lookup(incoming.target.address, incoming.message_id)
    assert len(record.as_dict()["media_refs"]) == 2
    monkeypatch.setattr("hermes_napcat.media_refs.time.time", lambda: now + 31)
    assert record.as_dict()["media_refs"] == []
    _, rows, _ = ctx.render(incoming.target.address)
    assert rows[0]["media_refs"] == [] and rows[0]["image_refs"] == []


def test_duplicate_context_preserves_text_and_enriches_media_locators():
    settings = config(media={"references": {"enabled": True}})
    ctx = GroupContext(settings, Policy(settings))
    incoming = Incoming.parse(event(text="original", parts=[part("file")]))
    assert ctx.put(incoming)
    before = ctx.lookup(incoming.target.address, incoming.message_id).media_refs[0]
    duplicate = Incoming.parse(event(text="backfill", parts=[part("file", "fresh")]))
    assert not ctx.put(duplicate, history=True)
    record = ctx.lookup(incoming.target.address, incoming.message_id)
    assert record.text == incoming.text
    assert record.media_refs[0].file_id == "fresh"
    assert record.media_refs[0].expires_at == before.expires_at


async def test_current_and_quote_share_count_and_byte_budget(hermes_doubles, settings, tmp_path):
    adapter = adapter_for(hermes_doubles, settings, tmp_path, max_turn_bytes=1024, max_attachments=3)
    quote = Incoming.parse(event(1, parts=[
        part("record", url="https://gchat.qpic.cn/record"),
        part("file", url="https://gchat.qpic.cn/file"),
    ]))
    current = Incoming.parse(event(2, parts=[part("image", url="https://gchat.qpic.cn/image"),
                                            {"type": "reply", "data": {"id": "1"}}]))
    adapter.media_refs.remember(quote)
    adapter.media.download = AsyncMock(return_value=cached(adapter.media, 600))
    paths, _, notices = await adapter._attachments(current)
    assert len(paths) == 1 and "message_id=2" in "\n".join(notices)
    assert [call.kwargs["max_bytes"] for call in adapter.media.download.call_args_list] == [1024, 424]
    adapter.settings = adapter.settings.model_copy(update={"media": adapter.settings.media.model_copy(
        update={"max_turn_bytes": 4096, "max_attachments": 2})})
    adapter.media.download.reset_mock()
    paths, _, notices = await adapter._attachments(current)
    assert len(paths) == 2 and "message_id=1" in "\n".join(notices)
    assert adapter.media.download.await_count == 1  # Current attachment came from verified cache.


@pytest.mark.parametrize("turn_bytes,max_attachments,expected_attempts", [
    (2048, 4, 2), (8192, 2, 2),
])
async def test_failed_current_and_forward_quote_downloads_keep_budget_reservations(
    hermes_doubles, settings, tmp_path, turn_bytes, max_attachments, expected_attempts,
):
    adapter = adapter_for(hermes_doubles, settings, tmp_path, max_bytes=1024,
                          max_turn_bytes=turn_bytes, max_attachments=max_attachments)
    parent = event(1, parts=[{"type": "forward", "data": {"id": "opaque.forward"}}])
    target = Target.parse("group:300")
    for node in range(8):
        adapter.remember_forward_media(target, "1", parent, (node,), {
            "content": [part("file", f"node{node}", url="https://gchat.qpic.cn/file")],
        })
    incoming = Incoming.parse(event(2, parts=[part("image", url="https://gchat.qpic.cn/image"),
                                              {"type": "reply", "data": {"id": "1"}}]))
    adapter.media.download = AsyncMock(side_effect=MediaError("transfer interrupted"))
    paths, _, notices = await adapter._attachments(incoming)
    assert not paths
    allowances = [call.kwargs["max_bytes"] for call in adapter.media.download.call_args_list]
    assert len(allowances) == expected_attempts and sum(allowances) <= turn_bytes
    assert notices.count("[附件数量或总字节数超过本次处理上限，其余附件未读取]") == 1


def test_known_own_private_message_id_cannot_override_explicit_foreign_destination(
    hermes_doubles, settings, tmp_path,
):
    adapter = adapter_for(hermes_doubles, settings, tmp_path)
    target = Target.parse("private:200")
    adapter.policy.own.add((target.address, "77"))
    data = {"message_type": "private", "message_id": 77, "user_id": 100,
            "sender": {"user_id": 100}, "time": time.time(), "message": [part("file")]}
    assert adapter.remember_verified_media(target, "77", data)
    assert not adapter.remember_verified_media(target, "77", {**data, "target_id": 201})


async def test_stream_first_falls_back_only_for_known_unsupported_action(hermes_doubles, settings, tmp_path):
    adapter = adapter_for(hermes_doubles, settings, tmp_path, download_mode="auto")
    incoming = Incoming.parse(event(parts=[part("video", url="https://gchat.qpic.cn/media")]))
    ref = adapter.media_refs.remember(incoming)[0]
    adapter._stream_downloader = SimpleNamespace(download=AsyncMock(side_effect=StreamDownloadUnsupported("unsupported")))
    result = cached(adapter.media)
    adapter.media.download = AsyncMock(return_value=result)
    assert await adapter.resolve_media(ref.media_id, incoming.target, incoming.user_id) == result
    adapter.media.download.assert_awaited_once_with(ref.url, kind="video", max_bytes=None)
    adapter.media_refs._items[ref.media_id] = replace(ref, downloaded=None)
    adapter.media.download.reset_mock()
    adapter._stream_downloader.download.side_effect = MediaError("invalid stream chunks")
    with pytest.raises(MediaError):
        await adapter.resolve_media(ref.media_id, incoming.target, incoming.user_id)
    adapter.media.download.assert_not_called()
    adapter.settings = adapter.settings.model_copy(update={"media": adapter.settings.media.model_copy(
        update={"download_mode": "stream"})})
    adapter._stream_downloader.download.side_effect = StreamDownloadUnsupported("unsupported")
    with pytest.raises(StreamDownloadUnsupported):
        await adapter.resolve_media(ref.media_id, incoming.target, incoming.user_id)
    adapter.media.download.assert_not_called()


async def test_stale_id_refreshes_once_in_verified_conversation(hermes_doubles, settings, tmp_path):
    adapter = adapter_for(hermes_doubles, settings, tmp_path, download_mode="auto")
    incoming = Incoming.parse(event(parts=[part("file", "stale")]))
    ref = adapter.media_refs.remember(incoming)[0]
    adapter.transport.call.return_value = event(parts=[part("file", "fresh")])
    result = cached(adapter.media)
    transfer = AsyncMock(side_effect=[StreamFileUnavailable("not found"), result])
    adapter._stream_downloader = SimpleNamespace(download=transfer)
    assert await adapter.resolve_media(ref.media_id, incoming.target, incoming.user_id) == result
    assert [call.args[0] for call in transfer.call_args_list] == ["stale", "fresh"]
    adapter.transport.call.assert_awaited_once_with("get_msg", {"message_id": 1})
    assert adapter.media_refs.get(ref.media_id, incoming.target).expires_at == ref.expires_at


async def test_explicit_file_unavailable_after_one_refresh_allows_safe_http(
    hermes_doubles, settings, tmp_path,
):
    adapter = adapter_for(hermes_doubles, settings, tmp_path, download_mode="auto")
    incoming = Incoming.parse(event(parts=[part("file", "stale", url="https://gchat.qpic.cn/old")]))
    ref = adapter.media_refs.remember(incoming)[0]
    adapter.transport.call.return_value = event(parts=[
        part("file", "fresh", url="https://gchat.qpic.cn/fresh"),
    ])
    adapter._stream_downloader = SimpleNamespace(download=AsyncMock(
        side_effect=StreamFileUnavailable("not found")))
    result = cached(adapter.media)
    adapter.media.download = AsyncMock(return_value=result)
    assert await adapter.resolve_media(ref.media_id, incoming.target, incoming.user_id) == result
    adapter.media.download.assert_awaited_once_with(
        "https://gchat.qpic.cn/fresh", kind="file", max_bytes=None)
    assert adapter.transport.call.await_count == 1 and adapter._stream_downloader.download.await_count == 2


@pytest.mark.parametrize("change", [{"group_id": 301}, {"user_id": 201, "sender": {"user_id": 201}}])
async def test_stale_refresh_cannot_change_scope_or_author(hermes_doubles, settings, tmp_path, change):
    adapter = adapter_for(hermes_doubles, settings, tmp_path, download_mode="auto")
    incoming = Incoming.parse(event(parts=[part("file", "stale")]))
    ref = adapter.media_refs.remember(incoming)[0]
    adapter.transport.call.return_value = event(parts=[part("file", "fresh")], **change)
    adapter._stream_downloader = SimpleNamespace(download=AsyncMock(side_effect=StreamFileUnavailable("not found")))
    with pytest.raises(PermissionError):
        await adapter.resolve_media(ref.media_id, incoming.target, incoming.user_id)
    assert adapter._stream_downloader.download.await_count == 1


async def test_recall_during_stream_invalidates_every_kind(hermes_doubles, settings, tmp_path):
    adapter = adapter_for(hermes_doubles, settings, tmp_path, download_mode="stream")
    incoming = Incoming.parse(event(parts=[part("file")]))
    ref = adapter.media_refs.remember(incoming)[0]

    async def transfer(*args, **kwargs):
        adapter.media_refs.recall(incoming.target, incoming.message_id)
        return cached(adapter.media)

    adapter._stream_downloader = SimpleNamespace(download=AsyncMock(side_effect=transfer))
    with pytest.raises(MediaError):
        await adapter.resolve_media(ref.media_id, incoming.target, incoming.user_id)
    assert not adapter.media_refs.remember(incoming)


async def test_outbound_reference_rechecks_kind_and_recall(hermes_doubles, settings, tmp_path):
    adapter = adapter_for(hermes_doubles, settings, tmp_path)
    incoming = Incoming.parse(event(parts=[part("record", url="https://gchat.qpic.cn/audio")]))
    ref = adapter.media_refs.remember(incoming)[0]
    adapter.media.download = AsyncMock(return_value=cached(adapter.media, mime="audio/mpeg"))
    with pytest.raises(MediaError):
        await adapter.outbound_reference(f"media:{ref.media_id}", kind="video",
                                         target=incoming.target, requester_id=incoming.user_id)
    adapter.media.download.assert_not_called()
    source = await adapter.outbound_reference(f"media:{ref.media_id}", kind="record",
                                              target=incoming.target, requester_id=incoming.user_id)
    with pytest.raises(MediaError):
        adapter.validate_outbound_media(incoming.target, {"type": "image", "data": {"file": source}})
    adapter.media_refs.recall(incoming.target, incoming.message_id)
    with pytest.raises(MediaError):
        adapter.validate_outbound_media(incoming.target, source)


async def test_forward_node_refs_use_parent_acl_ttl_and_recall(hermes_doubles, settings, tmp_path):
    adapter = adapter_for(hermes_doubles, settings, tmp_path)
    parent = event(parts=[{"type": "forward", "data": {"id": "forward-opaque"}}])
    target = Target.parse("group:300")
    refs = adapter.remember_forward_media(target, "1", parent, (0,), {
        "user_id": 999, "message_id": 123, "content": [part("file")],
    })
    assert refs and refs[0].user_id == "200" and refs[0].message_id == "1"
    assert refs[0].node_path == (0,)
    adapter.media_refs.recall(target, "1")
    assert not adapter.remember_forward_media(target, "1", parent, (0,), {"content": [part("file")]})


@pytest.mark.parametrize("provenance", ["forward", "notice"])
async def test_stale_forward_or_notice_does_not_lookup_synthetic_attachment_ids(
    hermes_doubles, settings, tmp_path, provenance,
):
    adapter = adapter_for(hermes_doubles, settings, tmp_path, download_mode="auto")
    raw = event(-55, parts=[part("file")], _napcat_file_notice=provenance == "notice")
    target = Target.parse("group:300")
    if provenance == "forward":
        ref = adapter.remember_forward_media(target, "-55", raw, (0,), {"content": [part("file")]})[0]
    else:
        ref = adapter.media_refs.remember(Incoming.parse(raw))[0]
    adapter._stream_downloader = SimpleNamespace(download=AsyncMock(
        side_effect=StreamFileUnavailable("not found")))
    with pytest.raises(MediaError, match="refresh the verified"):
        await adapter.resolve_media(ref.media_id, target, "200")
    adapter.transport.call.assert_not_called()


async def test_existing_quote_prevents_recent_background_downloads(
    hermes_doubles, settings, tmp_path,
):
    adapter = adapter_for(hermes_doubles, settings, tmp_path, references={
        "enabled": True, "attach_recent": True, "attach_quoted": False,
    })
    adapter.media_refs.remember(Incoming.parse(event(1, parts=[part("image")])) )
    current = Incoming.parse(event(2, parts=[{"type": "reply", "data": {"id": "99"}}]))
    adapter.media.download = AsyncMock()
    paths, _, _ = await adapter._attachments(current)
    assert paths == []
    adapter.media.download.assert_not_called()
    adapter.transport.call.assert_not_called()
