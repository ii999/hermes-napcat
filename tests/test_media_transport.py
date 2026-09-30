"""Exercise media recall admission over an actual loopback WebSocket."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from hermes_napcat.media import MediaStore
from hermes_napcat.protocol import Target
from hermes_napcat.transport import OneBotTransport
from test_adapter import make_adapter
from test_media_upgrade import image, owned
from test_transport import fake_napcat, ignore, wait_until


@pytest.mark.parametrize("kind", ["private", "group"])
async def test_recall_can_invalidate_image_while_only_message_worker_is_busy(
    hermes_doubles, settings, raw_event, tmp_path, kind,
):
    async with fake_napcat() as server:
        adapter = make_adapter(
            hermes_doubles, settings, ws_url=server["url"], event_workers=1,
            media={"references": {"enabled": True}},
        )
        adapter.media = MediaStore(adapter.settings.media, tmp_path / "cache")
        entered, release = asyncio.Event(), asyncio.Event()
        downloaded = owned(adapter.media)

        async def download(*args, **kwargs):
            entered.set()
            await release.wait()
            return downloaded

        adapter.media.download = AsyncMock(side_effect=download)
        adapter.transport = OneBotTransport(adapter.settings, adapter._receive)
        parts = [image()]
        if kind == "group":
            parts.insert(0, {"type": "at", "data": {"qq": "100"}})
        incoming = raw_event(message_type=kind, group_id=300, message=parts)
        target = Target.parse("group:300" if kind == "group" else "private:200")
        notice = {
            "post_type": "notice", "self_id": 100, "message_id": incoming["message_id"],
            "notice_type": "group_recall" if kind == "group" else "friend_recall",
            "group_id": 300, "user_id": 200,
        }
        try:
            await adapter.transport.start()
            await server["ws"].send_json(incoming)
            await asyncio.wait_for(entered.wait(), 1)
            await server["ws"].send_json(notice)
            await wait_until(lambda: adapter.media_refs.is_recalled(
                target.address, str(incoming["message_id"])))
            assert not release.is_set()  # Recall was handled during the read, not after it.
            release.set()
            await asyncio.wait_for(adapter.transport._queue.join(), 1)
            assert not adapter.media_refs.for_message(target, str(incoming["message_id"]))
            for call in adapter.handle_message.call_args_list:
                assert not call.args[0].media_urls
        finally:
            release.set()
            await adapter.disconnect()
        assert not adapter.transport._workers and adapter.transport._recalls.empty()


async def test_recall_filters_account_and_notice_type_on_wire(settings):
    received = []
    async def handler(event):
        received.append(event)
    async with fake_napcat() as server:
        transport = OneBotTransport(settings(
            ws_url=server["url"], media={"references": {"enabled": True}}), handler)
        notice = {"post_type": "notice", "notice_type": "friend_recall", "self_id": 100,
                  "user_id": 200, "message_id": 9}
        try:
            await transport.start()
            await server["ws"].send_json({**notice, "self_id": 101})
            await server["ws"].send_json({**notice, "notice_type": "friend_add"})
            await server["ws"].send_json(notice)
            await wait_until(lambda: len(received) == 1)
            assert received == [notice]
        finally:
            await transport.stop()


def test_recall_queue_is_bounded_and_media_opt_in_does_not_enable_other_notices(settings):
    transport = OneBotTransport(settings(
        event_queue_size=1, media={"references": {"enabled": True}}), ignore)
    notice = {"post_type": "notice", "notice_type": "friend_recall", "self_id": 100}
    assert transport._accept_recall(notice)
    transport._enqueue(notice)
    transport._enqueue(notice)
    assert transport._recalls.qsize() == 1 and transport.stats.dropped == 1
    assert transport._queue.empty()
    disabled = OneBotTransport(settings(), ignore)
    assert not disabled._accept_recall(notice)
    assert not disabled._accept_recall({**notice, "notice_type": "group_recall"})


async def test_original_group_recall_without_media_references_is_still_delivered(settings):
    received = []
    async def handler(event):
        received.append(event)
    async with fake_napcat() as server:
        transport = OneBotTransport(settings(
            ws_url=server["url"], group_context={"enabled": True}), handler)
        notice = {"post_type": "notice", "notice_type": "group_recall", "self_id": 100,
                  "group_id": 300, "message_id": 9}
        try:
            await transport.start()
            await server["ws"].send_json(notice)
            await wait_until(lambda: len(received) == 1)
            assert received == [notice]
        finally:
            await transport.stop()
