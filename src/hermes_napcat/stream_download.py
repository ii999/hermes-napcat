"""Authenticated NapCat downloads with bounded protocol validation and cache writes."""
from __future__ import annotations

import asyncio
import base64
import binascii
import math
import re
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing

from .config import Settings
from .media import Downloaded, MediaError, MediaStore
from .transport import OneBotError, OneBotTransport, StreamActionError, UnsupportedAction

_FILE_ID = re.compile(r"[A-Za-z0-9_{}().+\-]{1,256}\Z")
_ACTIONS = {
    "image": "download_file_image_stream", "record": "download_file_record_stream",
    "file": "download_file_stream", "video": "download_file_stream",
}


class StreamDownloadError(MediaError):
    """Stream transfer failed; no fallback or automatic retry is authorized."""


class StreamDownloadUnsupported(MediaError):
    """The authenticated peer explicitly does not implement this stream action."""


class StreamFileUnavailable(MediaError):
    """NapCat explicitly reported that the supplied file identifier was unavailable."""


def _integer(packet: dict, key: str, *, minimum: int = 0) -> int:
    value = packet.get(key)
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise StreamDownloadError(f"invalid stream {key}")
    return value


class StreamDownloader:
    def __init__(self, transport: OneBotTransport, settings: Settings, store: MediaStore):
        self.transport, self.settings, self.store = transport, settings, store

    async def download(self, file_id: str, *, kind: str,
                       access_check: Callable[[], None] | None = None,
                       max_bytes: int | None = None) -> Downloaded:
        if not self.settings.media.enabled:
            raise MediaError("media download is disabled")
        if (not isinstance(file_id, str) or file_id in (".", "..")
                or _FILE_ID.fullmatch(file_id) is None):
            raise StreamDownloadError("stream download requires an opaque file identifier")
        action = _ACTIONS.get(kind)
        if action is None:
            raise StreamDownloadError("unknown stream media kind")
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
            raise StreamDownloadError("stream byte allowance must be a positive integer")
        limit = min(self.settings.media.max_bytes, max_bytes) if max_bytes is not None else self.settings.media.max_bytes
        # Leave space for the JSON envelope and chunk metadata, including a large echo.
        chunk_size = min(256 * 1024, ((self.settings.ws_max_bytes - 512) // 4) * 3)
        if chunk_size <= 0:
            raise StreamDownloadError("WebSocket frame allowance is too small for downloads")
        epoch = self.transport.connection_epoch
        params = {"file": file_id, "chunk_size": chunk_size}
        if kind == "record":
            params["out_format"] = "mp3"
        info: dict = {}

        def check_access() -> None:
            if access_check is not None:
                access_check()
            if self.transport.connection_epoch != epoch or not self.transport.connected:
                raise StreamDownloadError("authenticated connection changed during media download")

        async def decoded_chunks() -> AsyncIterator[bytes]:
            check_access()
            count, total, short_chunk = 0, 0, False
            complete = False
            responses = self.transport.stream_call(
                action, params, timeout=self.settings.media.timeout,
                max_frames=math.ceil(limit / chunk_size) + 2,
                max_buffer_bytes=min(self.settings.ws_max_bytes * 4, 8 * 1024 * 1024),
                expected_epoch=epoch,
            )
            async with aclosing(responses):
                async for packet in responses:
                    check_access()
                    packet_type, data_type = packet.get("type"), packet.get("data_type")
                    if not info:
                        if packet_type != "stream" or data_type != "file_info":
                            raise StreamDownloadError("media stream must begin with file_info")
                        declared = _integer(packet, "file_size", minimum=1)
                        announced_chunk = _integer(packet, "chunk_size", minimum=1)
                        if announced_chunk != chunk_size:
                            raise StreamDownloadError("media stream chunk size differs from request")
                        name = packet.get("file_name")
                        if not isinstance(name, str) or len(name) > 4096:
                            raise StreamDownloadError("invalid stream file metadata")
                        if kind != "record" and declared > limit:
                            raise StreamDownloadError("media exceeds configured byte limit")
                        if kind == "record" and packet.get("out_format") != "mp3":
                            raise StreamDownloadError("media stream did not confirm MP3 conversion")
                        info.update(file_size=declared, file_name=name)
                        continue
                    if packet_type == "stream" and data_type == "file_chunk":
                        if short_chunk or _integer(packet, "index") != count:
                            raise StreamDownloadError("media stream chunk sequence is invalid")
                        size = _integer(packet, "size", minimum=1)
                        if size > chunk_size or total + size > limit:
                            raise StreamDownloadError("media stream exceeds its chunk or byte limit")
                        encoded = packet.get("data")
                        expected_size = 4 * ((size + 2) // 3)
                        if (not isinstance(encoded, str) or len(encoded) != expected_size
                                or _integer(packet, "base64_size") != expected_size):
                            raise StreamDownloadError("media stream base64 size is invalid")
                        try:
                            decoded = base64.b64decode(encoded, validate=True)
                        except (ValueError, binascii.Error) as exc:
                            raise StreamDownloadError("media stream base64 encoding is invalid") from exc
                        if len(decoded) != size or base64.b64encode(decoded).decode("ascii") != encoded:
                            raise StreamDownloadError("media stream base64 encoding is not canonical")
                        progress = _integer(packet, "progress")
                        if progress > 100:
                            raise StreamDownloadError("media stream progress is invalid")
                        count += 1
                        total += size
                        short_chunk = size < chunk_size
                        yield decoded
                    elif packet_type == "response" and data_type == "file_complete":
                        if (not count or _integer(packet, "total_chunks") != count
                                or _integer(packet, "total_bytes") != total
                                or (kind != "record" and info["file_size"] != total)):
                            raise StreamDownloadError("media stream completion totals do not match")
                        complete = True
                    else:
                        raise StreamDownloadError("unexpected media stream packet")
                if not complete:
                    raise StreamDownloadError("media stream ended without completion")
                check_access()

        try:
            async with asyncio.timeout(self.settings.media.timeout):
                check_access()
                async with aclosing(decoded_chunks()) as chunks:
                    return await self.store.import_stream(chunks, kind=kind, max_bytes=limit,
                                                          file_name=lambda: info.get("file_name"))
        except UnsupportedAction as exc:
            raise StreamDownloadUnsupported("NapCat does not support this download stream action") from exc
        except StreamActionError as exc:
            if exc.unavailable:
                raise StreamFileUnavailable("NapCat could not find this file identifier") from exc
            raise StreamDownloadError("NapCat rejected the media stream request") from exc
        except (OneBotError, TimeoutError, OSError) as exc:
            raise StreamDownloadError(f"media stream transfer failed ({type(exc).__name__})") from exc
