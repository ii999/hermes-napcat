"""NapCat upload_file_stream over the existing authenticated OneBot connection.

This stages bytes on NapCat; it does not send a QQ message. Each chunk and the
explicit completion request has its own echo and validated acknowledgement.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import stat
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .config import Settings
from .media import MediaError, MediaStore
from .protocol import request_bytes
from .transport import ActionError, OneBotError, OneBotTransport

log = logging.getLogger(__name__)


class StreamedMedia(str):
    """Remote path capability, valid only for the connection that staged it."""

    def __new__(cls, value: str, epoch: int, expires: float):
        result = super().__new__(cls, value)
        result.epoch, result.expires = epoch, expires
        return result

    def validate(self, transport: OneBotTransport) -> None:
        if not transport.connected or transport.connection_epoch != self.epoch:
            raise MediaError("stream upload belongs to an expired connection; no QQ send attempted")
        # Leave enough time for a queued action to be written and processed.
        if time.monotonic() + transport.config.request_timeout + 1 >= self.expires:
            raise MediaError("streamed media retention expired; no QQ send attempted")


async def _io(function, *args):
    """Join a cancelled thread operation before its caller closes the file handle."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


def _hash(stream, size: int) -> str:
    digest, count = hashlib.sha256(), 0
    while chunk := stream.read(min(256 * 1024, size + 1 - count)):
        count += len(chunk)
        if count > size:
            raise MediaError("media changed before stream upload")
        digest.update(chunk)
    if count != size:
        raise MediaError("media changed before stream upload")
    stream.seek(0)
    return digest.hexdigest()


def _remote_path(value: Any, filename: str) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 4096:
        raise MediaError("stream response contains an invalid remote path")
    # Validate the same separators that will be used in the outgoing file URI.
    value = value.replace("\\", "/")
    if any(ord(c) < 32 for c in value) or value.startswith("//"):
        raise MediaError("stream response contains an unsafe remote path")
    path = PureWindowsPath(value) if re.match(r"^[A-Za-z]:[/\\]", value) else PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or path.name != filename:
        raise MediaError("stream response path does not match this upload")
    return MediaStore._file_uri(value)


class StreamUploader:
    def __init__(self, transport: OneBotTransport, settings: Settings):
        self.transport, self.settings = transport, settings
        self.config = settings.media.streaming
        self._slots = asyncio.Semaphore(self.config.max_concurrent)
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    async def close(self) -> None:
        self._closed = True
        tasks = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def upload(self, path: Path, size: int) -> StreamedMedia:
        if self._closed or self.config.mode == "disabled":
            raise MediaError("stream upload is disabled; configure shared storage or inline limits")
        if not 0 < size <= min(self.config.max_bytes, self.settings.qq_tools.max_local_media_bytes):
            raise MediaError("media exceeds stream upload byte limit or is empty")
        if len(self._tasks) >= self.config.max_pending:
            raise MediaError("stream upload queue is full")
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            async with asyncio.timeout(self.config.timeout), self._slots:
                if self._closed:
                    raise MediaError("stream uploader is closed")
                return await self._upload(path, size)
        except TimeoutError as exc:
            raise MediaError("stream upload timed out; no QQ send attempted and no replay") from exc
        except OneBotError as exc:
            suffix = f" (retcode={exc.retcode!r})" if isinstance(exc, ActionError) else ""
            raise MediaError(
                f"NapCat stream staging failed{suffix}; no QQ send attempted; "
                "check NapCat compatibility or use shared storage") from exc
        finally:
            self._tasks.discard(task)

    async def _upload(self, path: Path, size: int) -> StreamedMedia:
        epoch = self.transport.connection_epoch
        if not self.transport.connected:
            raise MediaError("NapCat is not connected; no stream upload started")
        identifier = uuid.uuid4().hex
        suffix = path.suffix.lower()
        suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ".bin"
        # Never pass a model-supplied filename to NapCat's filesystem join.
        filename = f"napcat_upload_{identifier}{suffix}"
        params = {"stream_id": identifier, "chunk_data": "", "chunk_index": 0,
                  "total_chunks": size, "file_size": size, "expected_sha256": "0" * 64,
                  "filename": filename, "file_retention": self.config.file_retention_seconds * 1000}
        overhead = request_bytes("upload_file_stream", params)
        chunk_bytes = min(self.config.chunk_bytes,
                          max(0, (self.settings.ws_max_bytes - overhead) // 4) * 3)
        if chunk_bytes <= 0:
            raise MediaError("ws_max_bytes cannot fit a stream upload request")
        chunks = (size + chunk_bytes - 1) // chunk_bytes
        params["total_chunks"] = chunks
        attempted, complete = False, False
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                    raise MediaError("stream source is not an unchanged regular file")
                expected = await _io(_hash, source, size)
                params["expected_sha256"] = expected
                sent_hash = hashlib.sha256()
                remaining = size
                for index in range(chunks):
                    chunk = await _io(source.read, min(chunk_bytes, remaining))
                    if len(chunk) != min(chunk_bytes, remaining):
                        raise MediaError("stream source changed during upload")
                    remaining -= len(chunk)
                    sent_hash.update(chunk)
                    params["chunk_index"] = index
                    params["chunk_data"] = base64.b64encode(chunk).decode("ascii")
                    attempted = True
                    result = await self.transport.call(
                        "upload_file_stream", dict(params), timeout=self.config.chunk_timeout,
                        expected_epoch=epoch)
                    self._ack(result, identifier, chunks, index + 1, "chunk_received", "stream")
                if await _io(source.read, 1) or sent_hash.hexdigest() != expected:
                    raise MediaError("stream source changed during upload")
                started = time.monotonic()
                result = await self.transport.call(
                    "upload_file_stream", {"stream_id": identifier, "is_complete": True},
                    timeout=self.config.finalize_timeout, expected_epoch=epoch)
                self._ack(result, identifier, chunks, chunks, "file_complete", "response")
                if (type(result.get("file_size")) is not int or result["file_size"] != size
                        or result.get("sha256") != expected):
                    raise MediaError("stream completion size or SHA-256 mismatch")
                value = _remote_path(result.get("file_path"), filename)
                complete = True
                return StreamedMedia(value, epoch, started + self.config.file_retention_seconds)
        finally:
            if attempted and not complete:
                await self._reset(identifier, epoch)

    @staticmethod
    def _ack(result: Any, identifier: str, total: int, count: int, status: str, kind: str) -> None:
        if (not isinstance(result, dict) or result.get("stream_id") != identifier
                or result.get("type") != kind or result.get("status") != status
                or type(result.get("total_chunks")) is not int or result["total_chunks"] != total
                or type(result.get("received_chunks")) is not int or result["received_chunks"] != count):
            raise MediaError("invalid NapCat stream acknowledgement; no QQ send attempted")

    async def _reset(self, identifier: str, epoch: int) -> None:
        if not self.transport.connected or self.transport.connection_epoch != epoch:
            return  # Do not send a reset into a replacement NapCat process.
        try:
            await self.transport.call("upload_file_stream", {"stream_id": identifier, "reset": True},
                                      timeout=min(5, self.config.chunk_timeout), expected_epoch=epoch)
        except OneBotError as exc:
            # NapCat currently throws even after a successful reset. Never interpret that
            # error as proof of cleanup or retry it; server-side expiry is the fallback.
            log.info("Stream reset unconfirmed (%s); NapCat expiry remains required", type(exc).__name__)
