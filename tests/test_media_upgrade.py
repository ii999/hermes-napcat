"""Media migration/security regressions; network uses loopback fixtures, not real QQ."""
from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from hermes_napcat import tools
from hermes_napcat.config import MediaReferenceSettings, MediaSettings
from hermes_napcat.context import GroupContext
from hermes_napcat.group_chat import GroupTurn
from hermes_napcat.media import Downloaded, MediaError, MediaStore
from hermes_napcat.media_refs import MediaReferences
from hermes_napcat.policy import Policy
from hermes_napcat.protocol import Incoming, Target, message_batches, request_bytes
from hermes_napcat.transport import DeliveryUncertain
from test_adapter import make_adapter
import test_group_adapter
from test_group_context import config, event
from test_media import PNG, file_server
from test_tools import bind_session

group_adapter = test_group_adapter.group_adapter


def image(url="https://gchat.qpic.cn/image", file="opaque.image"):
    return {"type": "image", "data": {"url": url, "file": file}}


def owned(store, value=PNG):
    path = store.root / ("napcat_" + "e" * 32 + ".png")
    path.write_bytes(value)
    return Downloaded(path, "image/png", len(value))


def setup_adapter(hermes_doubles, settings, tmp_path, **kwargs):
    values = {"media": {"references": {"enabled": True}}, "qq_tools": {"enabled": True}}
    values.update(kwargs)
    values["media"] = {"download_mode": "http", **values["media"]}
    adapter = make_adapter(hermes_doubles, settings, **values)
    adapter.media = MediaStore(adapter.settings.media, tmp_path / "cache")
    return adapter


def test_policy_inheritance_and_explicit_public_opt_in(tmp_path):
    legacy = MediaSettings(allowed_hosts=["old.example"], trusted_private_origins=["http://local:8080"])
    for direction in ("inbound", "outbound"):
        assert legacy.download_policy(direction).allowed_hosts == ("old.example",)
        assert legacy.download_policy(direction).mode == "allowlist"
    config = MediaSettings(allowed_hosts=["old.example"], outbound={"mode": "public"})
    store = MediaStore(config, tmp_path / "cache")
    for url in ("http://new.example/a", "https://another.example/b"):
        assert store.validate_url(url, direction="outbound") == url
        with pytest.raises(MediaError):
            store.validate_url(url)
    empty = MediaStore(MediaSettings(allowed_hosts=[]), tmp_path / "empty")
    with pytest.raises(MediaError):
        empty.validate_url("https://gchat.qpic.cn/a")
    with pytest.raises(ValidationError):
        MediaSettings(outbound={"allowed_hosts": ["*.example"]})


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/a", "http://169.254.169.254/a", "http://10.1.2.3/a",
    "http://[::1]/a", "http://[::ffff:127.0.0.1]/a", "http://224.0.0.1/a",
    "file:///etc/passwd", "https://user@public.example/a", "https://example.org/a#f",
])
def test_public_mode_still_refuses_non_public_and_unsafe_urls(tmp_path, url):
    store = MediaStore(MediaSettings(outbound={"mode": "public"}), tmp_path / "cache")
    with pytest.raises(MediaError):
        store.validate_url(url, direction="outbound")


async def test_private_origin_trust_is_direction_specific(tmp_path):
    async with file_server() as origin:
        store = MediaStore(MediaSettings(
            inbound={"trusted_private_origins": [origin]}, outbound={"mode": "public"},
        ), tmp_path / "cache")
        try:
            assert (await store.download(origin + "/png", kind="image")).mime == "image/png"
            with pytest.raises(MediaError):
                await store.download(origin + "/png", kind="image", direction="outbound")
            assert store._session is not store._outbound_session
        finally:
            await store.close()


async def test_public_redirects_still_validate_each_origin(tmp_path):
    async with file_server() as origin:
        store = MediaStore(MediaSettings(outbound={
            "mode": "public", "trusted_private_origins": [origin],
        }), tmp_path / "cache")
        try:
            with pytest.raises(MediaError):
                await store.download(origin + "/redirect-private", kind="image", direction="outbound")
            assert not list(store.root.iterdir())
        finally:
            await store.close()


def test_downloaded_image_stages_without_allowlisting_inbound_cache(tmp_path):
    share = tmp_path / "share"
    store = MediaStore(MediaSettings(
        inline_max_bytes=1024, shared_paths=[{"hermes": share, "napcat": "D:/QQ shared"}],
        shared_cache_dir=share / "media",
    ), tmp_path / "cache")
    downloaded = owned(store, PNG + b"x" * 2048)
    reference = store.cached_reference(downloaded)
    assert reference.startswith("file:///D:/QQ%20shared/media/napcat_")
    staged = list((share / "media").glob("napcat_*"))
    assert len(staged) == 1 and staged[0].read_bytes() == downloaded.path.read_bytes()
    assert staged[0].stat().st_mode & 0o777 == 0o600
    with pytest.raises(MediaError):
        store.local_path(str(downloaded.path))


def test_shared_staging_quota_and_symlink_validation(tmp_path):
    share = tmp_path / "share"
    with pytest.raises(ValidationError):
        MediaSettings(shared_cache_dir=share)
    store = MediaStore(MediaSettings(
        max_bytes=1024, cache_max_bytes=1024,
        shared_paths=[{"hermes": share, "napcat": "/media"}], shared_cache_dir=share / "stage",
    ), tmp_path / "cache")
    downloaded = owned(store, PNG + b"x" * 600)
    keep = share / "stage" / "keep.txt"
    keep.write_text("operator content")
    with pytest.raises(MediaError, match="quota"):
        store.cached_reference(downloaded)
    assert keep.read_text() == "operator content" and not list(share.rglob("*.part"))
    escape = share / "escape"
    escape.symlink_to(tmp_path / "cache", target_is_directory=True)
    with pytest.raises(MediaError, match="symlink"):
        MediaStore(MediaSettings(shared_paths=[{"hermes": share, "napcat": "/media"}],
                                 shared_cache_dir=escape), tmp_path / "cache2")


async def test_both_gateway_image_entry_points_use_outbound_policy_and_staging(
    hermes_doubles, settings, tmp_path,
):
    share = tmp_path / "share"
    adapter = setup_adapter(hermes_doubles, settings, tmp_path, media={
        "outbound": {"mode": "public"}, "inline_max_bytes": 1024,
        "shared_paths": [{"hermes": share, "napcat": "/shared"}], "shared_cache_dir": share / "stage",
    })
    downloaded = owned(adapter.media, PNG + b"x" * 2000)
    adapter.media.download = AsyncMock(return_value=downloaded)
    for method in (adapter.send_image, adapter.send_image_file):
        result = await method("private:200", "https://new.example/image.png")
        assert result.success
        assert adapter.transport.call.call_args.args[1]["message"][-1]["data"]["file"].startswith("file:///")
    adapter.media.download.assert_awaited_with(
        "https://new.example/image.png", kind="image", direction="outbound")


async def test_expired_url_refreshes_once_through_inbound_downloader(
    hermes_doubles, settings, raw_event, tmp_path,
):
    adapter = setup_adapter(hermes_doubles, settings, tmp_path)
    downloaded = owned(adapter.media)
    adapter.media.download = AsyncMock(side_effect=[MediaError("media server returned HTTP 403"), downloaded])
    adapter.transport.call.return_value = raw_event(
        message=[image(url="https://gchat.qpic.cn/fresh", file="/napcat/private")], time=time.time())
    await adapter._receive(raw_event(message=[image()]))
    adapter.transport.call.assert_awaited_once_with("get_msg", {"message_id": -10})
    assert [c.args[0] for c in adapter.media.download.call_args_list] == [
        "https://gchat.qpic.cn/image", "https://gchat.qpic.cn/fresh"]
    delivered = adapter.handle_message.call_args.args[0]
    assert delivered.media_urls == [str(downloaded.path)]
    assert "media:qqimg_" in delivered.text


@pytest.mark.parametrize("file", ["/etc/passwd", "D:\\secret.png", "file:///tmp/image", "base64://abc", "../secret"])
async def test_received_paths_are_not_used_as_get_image_ids(
    hermes_doubles, settings, raw_event, tmp_path, file,
):
    adapter = setup_adapter(hermes_doubles, settings, tmp_path)
    adapter.media.download = AsyncMock()
    await adapter._receive(raw_event(message=[image(url=None, file=file)]))
    adapter.transport.call.assert_not_called()
    adapter.media.download.assert_not_called()
    assert not adapter.handle_message.call_args.args[0].media_urls


async def test_refresh_failure_does_not_retry_forever(hermes_doubles, settings, raw_event, tmp_path):
    adapter = setup_adapter(hermes_doubles, settings, tmp_path)
    adapter.media.download = AsyncMock(side_effect=[
        MediaError("media server returned HTTP 403"), MediaError("media resolves to a non-public address"),
    ])
    adapter.transport.call.return_value = raw_event(
        message=[image(url="http://169.254.169.254/secret")], time=time.time())
    await adapter._receive(raw_event(message=[image()]))
    assert adapter.media.download.await_count == 2 and adapter.transport.call.await_count == 1
    assert not adapter.handle_message.call_args.args[0].media_urls


def test_reference_scope_ttl_recall_and_capacity():
    references = MediaReferences("100", MediaReferenceSettings(enabled=True, max_entries=16, ttl_seconds=30))
    incoming = Incoming.parse(event(1, parts=[image()]))
    ref = references.remember(incoming)[0]
    assert references.remember(incoming)[0].media_id == ref.media_id
    assert "url" not in ref.summary() and "file" not in ref.summary()
    with pytest.raises(MediaError):
        references.get(ref.media_id, Target.parse("private:200"))
    with pytest.raises(MediaError):
        MediaReferences("101", references.config).get(ref.media_id, incoming.target)
    references._items[ref.media_id] = replace(ref, expires_at=time.time() - 1)
    with pytest.raises(MediaError):
        references.get(ref.media_id, incoming.target)
    assert not references.remember(Incoming.parse(event(2, stamp=time.time() - 40, parts=[image()])))
    for index in range(20):
        references.remember(Incoming.parse(event(100 + index, parts=[image()])))
    assert len(references._items) == len(references._keys) == 16
    references.recall(incoming.target, "119")
    assert not references.for_message(incoming.target, "119")
    assert not references.remember(Incoming.parse(event(119, parts=[image()])))


async def test_group_observation_is_lazy_and_recent_attachment_is_same_speaker(group_adapter, tmp_path):
    adapter = group_adapter(media={"download_mode": "http", "references": {"enabled": True, "attach_recent": True}},
                            group_context={"enabled": True, "history_backfill": False, "observe_all_members": True})
    adapter.media = MediaStore(adapter.settings.media, tmp_path / "cache")
    adapter.media.download = AsyncMock(return_value=owned(adapter.media))
    await adapter._receive(event(1, text="", parts=[image()]))
    await adapter._receive(event(2, user=201, text="", parts=[image(file="other")]))
    adapter.media.download.assert_not_called()
    records = adapter.groups.context.records("group:300")
    encoded = json.dumps([record.as_dict() for record in records])
    assert "qqimg_" in encoded and "gchat.qpic.cn" not in encoded and "opaque.image" not in encoded
    await adapter._receive(event(3, text="这是什么？", parts=[{"type": "at", "data": {"qq": "100"}}]))
    await adapter.groups.wait_idle()
    delivered = adapter.handle_message.call_args.args[0]
    assert len(delivered.media_urls) == 1 and "message_id=1" in delivered.text
    assert "message_id=2" not in delivered.text
    await adapter.disconnect()


async def test_group_quote_attaches_image_without_model_tools(group_adapter, tmp_path):
    adapter = group_adapter(media={"download_mode": "http", "references": {"enabled": True}},
                            group_context={"enabled": True, "history_backfill": False})
    adapter.media = MediaStore(adapter.settings.media, tmp_path / "cache")
    adapter.media.download = AsyncMock(return_value=owned(adapter.media))
    await adapter._receive(event(1, text="", parts=[image()]))
    await adapter._receive(event(2, text="解释这张图", parts=[
        {"type": "reply", "data": {"id": "1"}}, {"type": "at", "data": {"qq": "100"}}]))
    await adapter.groups.wait_idle()
    delivered = adapter.handle_message.call_args.args[0]
    assert delivered.media_urls and "message_id=1" in delivered.text
    assert not adapter.settings.qq_tools.enabled and not adapter.settings.group_toolsets
    await adapter.disconnect()


async def test_proactive_turn_does_not_read_images(group_adapter, tmp_path):
    adapter = group_adapter(media={"download_mode": "http", "references": {"enabled": True, "attach_recent": True}})
    adapter.media = MediaStore(adapter.settings.media, tmp_path / "cache")
    adapter.media.download = AsyncMock()
    incoming = Incoming.parse(event(1, parts=[image()]))
    adapter.groups.context.put(incoming)
    await adapter._dispatch_group(GroupTurn(incoming, "help?", proactive=True))
    adapter.media.download.assert_not_called()
    await adapter.disconnect()


async def test_private_quote_is_verified_and_foreign_chat_cannot_supply_image(
    hermes_doubles, settings, raw_event, tmp_path,
):
    adapter = setup_adapter(hermes_doubles, settings, tmp_path)
    adapter.media.download = AsyncMock(return_value=owned(adapter.media))
    adapter.transport.call.return_value = {"message_type": "private", "user_id": 201,
        "message_id": 1, "time": time.time(), "message": [image()]}
    await adapter._receive(raw_event(message=[{"type": "reply", "data": {"id": "1"}}]))
    adapter.media.download.assert_not_called()
    assert not adapter.handle_message.call_args.args[0].media_urls


async def test_media_tool_returns_one_path_and_resends_only_by_same_chat_capability(
    monkeypatch, hermes_doubles, settings, raw_event, tmp_path,
):
    adapter = setup_adapter(hermes_doubles, settings, tmp_path)
    adapter.media.download = AsyncMock(return_value=owned(adapter.media))
    incoming = Incoming.parse(raw_event(message=[image()]))
    ref = adapter.media_refs.remember(incoming)[0]
    bind_session(monkeypatch, adapter, target="private:200")
    result = json.loads(await tools.qq_get_media({"media_id": ref.media_id}))
    assert result["success"] and Path(result["path"]).read_bytes() == PNG
    with pytest.raises(MediaError):
        adapter.media.local_path(result["path"])
    response = json.loads(await tools.qq_send_message({"images": [result["source"]]}))
    assert response["success"] and adapter.media.download.await_count == 1
    payload = adapter.transport.call.call_args.args[1]["message"][0]["data"]["file"]
    assert base64.b64decode(payload.removeprefix("base64://")) == PNG
    bind_session(monkeypatch, adapter, target="group:300")
    denied = json.loads(await tools.qq_get_media({"media_id": ref.media_id}))
    assert not denied["success"] and adapter.media.download.await_count == 1


async def test_recall_during_download_and_queued_send_invalidates_capability(
    hermes_doubles, settings, raw_event, tmp_path,
):
    adapter = setup_adapter(hermes_doubles, settings, tmp_path)
    incoming = Incoming.parse(raw_event(message=[image()]))
    ref = adapter.media_refs.remember(incoming)[0]
    downloaded = owned(adapter.media)
    async def transfer(*args, **kwargs):
        adapter.media_refs.recall(incoming.target, incoming.message_id)
        return downloaded
    adapter.media.download = AsyncMock(side_effect=transfer)
    with pytest.raises(MediaError):
        await adapter.resolve_media(ref.media_id, incoming.target, incoming.user_id)
    adapter.media_refs.clear()
    ref = adapter.media_refs.remember(incoming)[0]
    adapter.media.download = AsyncMock(return_value=downloaded)
    source = await adapter.outbound_reference(f"media:{ref.media_id}", kind="image",
                                               target=incoming.target, requester_id=incoming.user_id)
    await adapter._send_gate.acquire()
    send = asyncio.create_task(adapter.send_agent_parts(incoming.target, [{"type": "image", "data": {"file": source}}]))
    await asyncio.sleep(0)
    adapter.media_refs.recall(incoming.target, incoming.message_id)
    adapter._send_gate.release()
    with pytest.raises(MediaError):
        await send
    adapter.transport.call.assert_not_called()


async def test_private_recall_before_message_prevents_download(
    hermes_doubles, settings, raw_event, tmp_path,
):
    adapter = setup_adapter(hermes_doubles, settings, tmp_path)
    adapter.media.download = AsyncMock()
    await adapter._receive({"post_type": "notice", "notice_type": "friend_recall",
                            "self_id": 100, "user_id": 200, "message_id": -10})
    await adapter._receive(raw_event(message=[image()]))
    adapter.media.download.assert_not_called()
    adapter.handle_message.assert_not_called()


def test_group_history_keeps_handles_without_urls_and_recall_cannot_rehydrate():
    settings = config(media={"references": {"enabled": True}})
    context = GroupContext(settings, Policy(settings))
    raw = event(1, parts=[image()])
    parsed = context.parse_history(Target.parse("group:300"), raw)
    assert context.put(parsed, history=True)
    record = context.lookup("group:300", "1")
    assert record.image_refs and "url" not in json.dumps(record.as_dict())
    context.recall("group:300", "1")
    assert not context.put(parsed, history=True)
    assert not context.media_refs.for_message(parsed.target, "1")


async def test_batches_bound_utf8_and_report_partial_delivery(hermes_doubles, settings, tmp_path):
    adapter = setup_adapter(hermes_doubles, settings, tmp_path,
                            ws_max_bytes=20000, media={"inline_max_bytes": 1024})
    target = Target.parse("group:300")
    parts = [{"type": "text", "data": {"text": "图" * 100}}, *[
        {"type": "image", "data": {"file": "base64://" + "A" * 10000}} for _ in range(3)]]
    batches = message_batches(target, parts, 20000)
    assert len(batches) == 3 and [p for batch in batches for p in batch] == parts
    assert all(request_bytes(target.action, {**target.params, "message": batch}) <= 20000 for batch in batches)
    adapter.transport.call.side_effect = [{"message_id": 1}, DeliveryUncertain("lost acknowledgment")]
    result = await adapter.send_agent_parts(target, parts)
    assert not result["success"] and result["partial"] and result["delivery_uncertain"]
    assert result["message_ids"] == ["1"] and adapter.transport.call.await_count == 2


async def test_oversize_later_segment_fails_before_any_send(hermes_doubles, settings, tmp_path):
    adapter = setup_adapter(hermes_doubles, settings, tmp_path,
                            ws_max_bytes=20000, media={"inline_max_bytes": 1024})
    with pytest.raises(ValueError, match="exceeds"):
        await adapter.send_agent_parts(Target.parse("private:200"), [
            {"type": "text", "data": {"text": "would otherwise send first"}},
            {"type": "image", "data": {"file": "base64://" + "A" * 30000}},
        ])
    adapter.transport.call.assert_not_called()


@pytest.mark.parametrize("extra", [
    {"user_id": 201, "sender": {"user_id": 201}, "target_id": 200},
    {"target_id": 201}, {"self_id": 101}, {"message_id": "invalid"},
    {"message_type": "group", "group_id": 300},
])
def test_image_registration_independently_checks_message_participants(
    hermes_doubles, settings, raw_event, tmp_path, extra,
):
    adapter = setup_adapter(hermes_doubles, settings, tmp_path, allowed_users=["200", "201"])
    data = raw_event(message=[image()], time=time.time())
    target = Target.parse("private:200")
    assert adapter.remember_verified_media(target, "-10", data)
    data.update(extra)
    assert not adapter.remember_verified_media(target, "-10", data)


async def test_malformed_quote_does_not_break_the_current_turn(
    hermes_doubles, settings, raw_event, tmp_path,
):
    adapter = setup_adapter(hermes_doubles, settings, tmp_path)
    adapter.transport.call.return_value = {
        "message_type": "private", "user_id": 200, "time": time.time(), "message": None,
    }
    await adapter._receive(raw_event(message=[
        {"type": "reply", "data": {"id": "8"}}, {"type": "text", "data": {"text": "图片呢？"}},
    ]))
    assert adapter.handle_message.await_count == 1
    assert not adapter.handle_message.call_args.args[0].media_urls


def test_local_images_respect_the_lower_of_image_and_tool_limits(tmp_path):
    root = tmp_path / "output"
    root.mkdir()
    picture = root / "picture.png"
    picture.write_bytes(PNG + b"x" * 2000)
    store = MediaStore(MediaSettings(max_bytes=4096, outbound_roots=[root]), tmp_path / "cache")
    with pytest.raises(MediaError, match="byte limit"):
        store.outbound_reference(str(picture), kind="image", max_bytes=1024)
    assert store.outbound_reference(str(picture), kind="image", max_bytes=4096).startswith("base64://")
