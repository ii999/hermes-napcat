"""Native Hermes gateway adapter. Hermes owns agent sessions and execution."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from hermes_constants import get_hermes_dir

from .media import MediaError, MediaStore, is_inline_source
from .media_adapter import MediaAdapterMixin
from .media_refs import MediaReferences
from .file_notices import FileNotices
from .outbound import PreparedMedia, validate_request_sizes
from .plugin import settings_from_platform
from .policy import Policy
from .protocol import (
    Incoming, ProtocolError, Target, message_batches, message_id, request_bytes,
    split_text, text_segments,
)
from .transport import DeliveryUncertain, OneBotError, OneBotTransport

log = logging.getLogger(__name__)


class NapCatAdapter(MediaAdapterMixin, BasePlatformAdapter):
    splits_long_messages = True
    supports_code_blocks = False

    def __init__(self, config: PlatformConfig, *, receive_events: bool = True):
        super().__init__(config, Platform("napcat"))
        self.settings = settings_from_platform(config)
        self.policy = Policy(self.settings)
        self.transport = OneBotTransport(self.settings, self._receive if receive_events else self._ignore)
        self.media: MediaStore | None = None
        self.media_refs = MediaReferences(self.settings.self_id, self.settings.media.references,
                                          per_message=self.settings.media.max_attachments)
        self._media_read_gate = asyncio.Lock()
        self._file_notices = FileNotices(self.settings)
        self._send_gate = asyncio.Lock()
        self._agent_actions: dict[str, asyncio.Task[Any]] = {}

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self.transport.running:
            return True
        try:
            self.media = MediaStore(
                self.settings.media, Path(get_hermes_dir("cache/documents", "document_cache")) / "napcat")
            await self.transport.start()
            self._mark_connected()
            return True
        except Exception as exc:
            log.error("Could not start NapCat adapter (%s)", type(exc).__name__)
            await self.disconnect()
            return False

    async def disconnect(self) -> None:
        await self._close_streams()
        await self.transport.stop()
        if self.media is not None:
            await self.media.close()
            self.media = None
        self.media_refs.clear()
        self._file_notices.clear()
        self._mark_disconnected()

    @staticmethod
    async def _ignore(raw: dict[str, Any]) -> None:
        return

    async def _verified_reply(self, incoming: Incoming) -> bool:
        if incoming.reply_to is None:
            return False
        key = (incoming.target.address, incoming.reply_to)
        if self.policy.own.contains(key):
            return True
        if incoming.target.kind != "group" or not self.settings.group_reply_to_bot:
            return False
        try:
            data = await self.transport.call("get_msg", {"message_id": int(incoming.reply_to)})
        except OneBotError:
            return False
        # Never trust a quote's supplied sender field; verify author AND group from NapCat.
        if not isinstance(data, dict) or not isinstance(data.get("sender"), dict):
            return False
        if (data.get("message_type") == "group" and
                str(data.get("group_id")) == incoming.target.id and
                str(data["sender"].get("user_id")) == self.settings.self_id):
            self.policy.own.add(key)
            return True
        return False

    async def _receive(self, raw: dict[str, Any]) -> None:
        raw = self._file_notices.normalize(raw)
        if raw is None:
            return
        if self._consume_media_recall(raw):
            return
        try:
            incoming = Incoming.parse(raw)
        except (ProtocolError, ValueError):
            log.warning("Invalid OneBot message rejected; check messagePostFormat=array")
            return
        if incoming is None or not self.policy.can_receive(incoming):
            return
        if self.media_refs.is_recalled(incoming.target.address, incoming.message_id):
            return
        key = (incoming.self_id, incoming.target.address, incoming.message_id)
        if self.policy.seen.contains(key):
            return
        # Limit even reply-verification API requests by authenticated users.
        if not self.policy.rate_allowed(incoming):
            log.warning("QQ sender exceeded configured admission rate")
            return
        own_reply = await self._verified_reply(incoming)
        text = self.policy.trigger(incoming, own_reply)
        if text is None:
            return
        if not self._file_notices.admit(incoming):
            return
        self.policy.seen.add(key)
        try:
            paths, mimes, problems = await self._attachments(incoming)
            if problems:
                text += "\n" + "\n".join(problems)
            if not text.strip() and not paths:
                text = "你好，请告诉我需要处理什么。"
            source = self.build_source(
                chat_id=incoming.target.address,
                chat_name=f"QQ群 {incoming.target.id}" if incoming.target.kind == "group" else incoming.user_name,
                chat_type="group" if incoming.target.kind == "group" else "dm",
                user_id=incoming.user_id, user_name=incoming.user_name,
                message_id=incoming.message_id,
                # scope_id prevents accidental reuse across different bot accounts in the same profile.
                scope_id=self.settings.self_id,
            )
            kind = MessageType.TEXT
            if mimes:
                first = mimes[0]
                kind = (MessageType.PHOTO if first.startswith("image/") else
                        MessageType.VOICE if first.startswith("audio/") else
                        MessageType.VIDEO if first.startswith("video/") else MessageType.DOCUMENT)
            event = MessageEvent(
                text=text, message_type=kind, source=source,
                user_id=incoming.user_id, user_name=incoming.user_name,
                message_id=incoming.message_id, raw_message=raw,
                media_urls=paths, media_types=mimes, media_text_inlined=[False] * len(paths),
                reply_to_message_id=incoming.reply_to,
                reply_to_is_own_message=own_reply,
                # Only explicitly configured admins can use gateway control commands/prompts.
                allow_gateway_control=incoming.user_id in self.settings.admins,
                metadata={"napcat_self_id": self.settings.self_id},
            )
            await self.handle_message(event)
        except BaseException:
            self.policy.seen.discard(key)
            self._file_notices.discard(incoming)
            raise

    def toolsets_for_source(self, source):
        # Safe default for multi-user groups; private sessions use operator-defined platform_toolsets.
        if source.chat_type == "group":
            return list(self.settings.group_toolsets)
        return None

    @staticmethod
    def _failure(exc: Exception, ids: list[str] | None = None) -> SendResult:
        uncertain = isinstance(exc, DeliveryUncertain)
        error = ("QQ delivery outcome unknown; check the conversation before retrying" if uncertain
                 else f"QQ delivery failed ({type(exc).__name__})")
        # No retryable flag: an action can have succeeded before the link was lost.
        return SendResult(success=False, error=error, retryable=False,
                          raw_response={"partial_message_ids": ids or [], "delivery_uncertain": uncertain})

    async def _send_parts(self, target: Target, parts: list[dict[str, Any]]) -> str:
        return await self._send_action_with_id(
            target, target.action, {**target.params, "message": parts})

    async def _send_action_with_id(
        self, target: Target, action: str, params: dict[str, Any],
    ) -> str:
        if not self.policy.can_send(target):
            raise PermissionError("target is not allowlisted")
        if request_bytes(action, params) > self.settings.ws_max_bytes:
            raise MediaError("outbound request exceeds ws_max_bytes; use shared media storage")
        async with self._send_gate:
            self.validate_outbound_media(target, params)
            result = await self.transport.call(action, params,
                                               **self.media_send_kwargs(target, params))
            if not isinstance(result, dict) or result.get("message_id") is None:
                raise DeliveryUncertain("OneBot acknowledged a send without a message ID")
            try:
                identifier = message_id(result["message_id"])
            except ValueError as exc:
                raise DeliveryUncertain("OneBot acknowledged a send with an invalid message ID") from exc
            self.policy.own.add((target.address, identifier))
            if self.settings.send_interval:
                await asyncio.sleep(self.settings.send_interval)
            return identifier

    async def run_agent_action(
        self, key: str, operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Coalesce the same in-flight model action without caching completed sends."""
        task = self._agent_actions.get(key)
        if task is None:
            task = asyncio.create_task(operation(), name="napcat-agent-action")
            self._agent_actions[key] = task

            def forget(done: asyncio.Task[Any]) -> None:
                if self._agent_actions.get(key) is done:
                    self._agent_actions.pop(key, None)
                try:
                    done.exception()
                except (asyncio.CancelledError, Exception):
                    pass

            task.add_done_callback(forget)
        # A cancelled tool worker must not cancel a send already accepted by the gateway loop.
        return await asyncio.shield(task)

    async def send_agent_media(
        self, target: Target, kind: str, source: str, *, caption: str | None = None,
        file_name: str | None = None, thumbnail: str | None = None,
        reply_to: str | None = None, requester_id: str | None = None,
    ) -> dict[str, Any]:
        prepared = await self.prepare_media(
            target, kind, source, caption=caption, file_name=file_name, thumbnail=thumbnail,
            reply_to=reply_to, requester_id=requester_id)
        return await self.send_prepared_media(prepared)

    async def prepare_media(
        self,
        target: Target,
        kind: str,
        source: str,
        *,
        caption: str | None = None,
        file_name: str | None = None,
        thumbnail: str | None = None,
        reply_to: str | None = None,
        requester_id: str | None = None,
    ) -> PreparedMedia:
        """Validate and stage one media item without sending a QQ message."""
        if kind not in ("image", "audio", "video", "file"):
            raise ValueError("unsupported media type")
        if not self.policy.can_send(target):
            raise PermissionError("target is not allowlisted")
        if caption is not None:
            if not isinstance(caption, str) or not caption.strip():
                caption = None
            elif len(caption) > self.settings.message_chars:
                raise ValueError("caption is too long")
        if reply_to is not None:
            reply_to = message_id(reply_to)
        name = file_name
        if kind == "file":
            name = name or ("attachment.bin" if is_inline_source(source)
                            else Path(urlsplit(source).path or source).name)
            if (not isinstance(name, str) or not name or "/" in name or "\\" in name
                    or len(name) > 200 or name in (".", "..")):
                raise ValueError("invalid attachment name")
            if reply_to is not None and caption is None:
                raise ValueError("a file reply requires caption text")

        reference = await self.outbound_reference(
            source, kind="record" if kind == "audio" else kind,
            target=target, requester_id=requester_id)
        thumb_reference = None
        if thumbnail is not None:
            if kind != "video":
                raise ValueError("thumbnail is only valid for video")
            thumb_reference = await self.outbound_reference(
                thumbnail, kind="image", target=target, requester_id=requester_id)

        return PreparedMedia(target, kind, reference, thumb_reference, caption, name, reply_to)

    async def send_prepared_media(self, prepared: PreparedMedia) -> dict[str, Any]:
        """Deliver a prepared item, preserving partial and uncertain acknowledgments."""
        target, kind = prepared.target, prepared.kind
        if not self.policy.can_send(target):
            raise PermissionError("target is not allowlisted")
        requests = prepared.requests()
        validate_request_sizes(requests, self.settings.ws_max_bytes)
        action, params = requests[0]
        ids: list[str] = []
        if kind == "file":
            async with self._send_gate:
                self.validate_outbound_media(target, params)
                upload = await self.transport.call(
                    action, params, **self.media_send_kwargs(target, params),
                )
            if len(requests) > 1:
                try:
                    ids.append(await self._send_action_with_id(target, *requests[1]))
                except (OneBotError, MediaError, ValueError, PermissionError) as exc:
                    return {
                        "success": False,
                        "partial": True,
                        "file_uploaded": True,
                        "file_id": upload.get("file_id") if isinstance(upload, dict) else None,
                        "message_ids": ids,
                        "delivery_uncertain": isinstance(exc, DeliveryUncertain),
                        "error": "file uploaded but caption delivery failed",
                    }
            return {
                "success": True,
                "file_uploaded": True,
                "file_id": upload.get("file_id") if isinstance(upload, dict) else None,
                "message_ids": ids,
            }

        ids.append(await self._send_action_with_id(target, action, params))
        # QQ clients handle voice/video captions more consistently as a separate message.
        if len(requests) > 1:
            try:
                ids.append(await self._send_action_with_id(target, *requests[1]))
            except (OneBotError, MediaError, ValueError, PermissionError) as exc:
                return {
                    "success": False,
                    "partial": True,
                    "media_delivered": True,
                    "message_ids": ids,
                    "delivery_uncertain": isinstance(exc, DeliveryUncertain),
                    "error": "media delivered but caption delivery failed",
                }
        return {"success": True, "message_ids": ids}

    async def send_agent_parts(
        self, target: Target, parts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not self.policy.can_send(target):
            raise PermissionError("target is not allowlisted")
        batches = message_batches(target, parts, self.settings.ws_max_bytes)
        ids: list[str] = []
        for batch in batches:
            try:
                ids.append(await self._send_parts(target, batch))
            except (OneBotError, MediaError, ValueError, PermissionError) as exc:
                if not ids:
                    raise
                return {"success": False, "partial": True, "message_ids": ids,
                        "delivery_uncertain": isinstance(exc, DeliveryUncertain),
                        "error": "QQ message partially delivered; do not retry the entire message"}
        result = {"success": True, "message_id": ids[-1]}
        if len(ids) > 1:
            result["message_ids"] = ids
        return result

    async def send_agent_forward(
        self,
        target: Target,
        nodes: list[dict[str, Any]],
        *,
        source: str | None = None,
        summary: str | None = None,
        prompt: str | None = None,
        preview: list[str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {**target.params, "messages": nodes}
        for key, value in (("source", source), ("summary", summary), ("prompt", prompt)):
            if value:
                params[key] = value
        if preview:
            params["news"] = [{"text": line} for line in preview]
        identifier = await self._send_action_with_id(
            target, f"send_{target.kind}_forward_msg", params)
        return {"success": True, "message_id": identifier}

    async def verified_message(
        self, target: Target, identifier: str, *, current_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a message and prove that it belongs to the authorized target."""
        if not self.policy.can_send(target):
            raise PermissionError("target is not allowlisted")
        identifier = message_id(identifier)
        if self._file_notices.is_notice_message(target, identifier):
            raise PermissionError("file notices have no retrievable QQ message; use their media reference")
        if self.media_refs.is_recalled(target.address, identifier):
            raise PermissionError("message was recalled")
        data = await self.transport.call("get_msg", {"message_id": int(identifier)})
        if self.media_refs.is_recalled(target.address, identifier):
            raise PermissionError("message was recalled during the read")
        if not isinstance(data, dict) or data.get("message_type") != target.kind:
            raise PermissionError("message does not belong to the target conversation")
        if ((data.get("self_id") is not None and str(data["self_id"]) != self.settings.self_id)
                or (data.get("message_id") is not None and message_id(data["message_id"]) != identifier)):
            raise PermissionError("message identity does not match the requested account and message")
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        author = str(data.get("user_id") or sender.get("user_id") or "")
        if sender.get("user_id") is not None and str(sender["user_id"]) != author:
            raise PermissionError("message sender attribution is inconsistent")
        if target.kind == "group":
            belongs = str(data.get("group_id")) == target.id
        else:
            destination = str(data.get("target_id") or "")
            belongs = (
                (author == target.id and destination in ("", target.id, self.settings.self_id))
                or (author == self.settings.self_id and (
                    destination == target.id or (not destination
                    and self.policy.own.contains((target.address, identifier)))))
            )
        if not belongs:
            raise PermissionError("message does not belong to the target conversation")
        return data

    async def send(self, chat_id: str, content: str, reply_to=None, metadata=None) -> SendResult:
        ids: list[str] = []
        try:
            target = Target.parse(chat_id)
            if not isinstance(content, str) or not content.strip():
                raise ValueError("message must contain text")
            if len(content) > self.settings.max_outbound_chars:
                raise ValueError("message exceeds total outbound length limit")
            for index, chunk in enumerate(split_text(content, self.settings.message_chars)):
                ids.append(await self._send_parts(target, text_segments(chunk, reply_to if index == 0 else None)))
            return SendResult(success=True, message_id=ids[-1], continuation_message_ids=tuple(ids[:-1]))
        except (OneBotError, MediaError, ValueError, PermissionError) as exc:
            return self._failure(exc, ids)

    async def _send_local_media(self, chat_id, path, kind, caption=None, reply_to=None):
        try:
            target = Target.parse(chat_id)
            if not self.policy.can_send(target):
                raise PermissionError("target is not allowlisted")
            if self.media is None:
                raise MediaError("media store is not ready")
            if caption and len(caption) > self.settings.message_chars:
                raise ValueError("caption is too long")
            if reply_to is not None:
                message_id(reply_to)
            reference = await self.outbound_reference(str(path), kind=kind)
            parts = text_segments(caption, reply_to) if caption else (
                [{"type": "reply", "data": {"id": message_id(reply_to)}}] if reply_to else [])
            parts.append({"type": kind, "data": {"file": reference}})
            identifier = await self._send_parts(target, parts)
            return SendResult(success=True, message_id=identifier)
        except (OneBotError, MediaError, ValueError, OSError, PermissionError) as exc:
            return self._failure(exc)

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None, **kwargs):
        return await self._send_local_media(chat_id, image_path, "image", caption, reply_to)

    async def send_image(self, chat_id, image_url, caption=None, reply_to=None, metadata=None):
        return await self._send_local_media(chat_id, image_url, "image", caption, reply_to)

    async def send_voice(self, chat_id, audio_path, caption=None, reply_to=None, metadata=None, **kwargs):
        return await self._send_local_media(chat_id, audio_path, "record", caption, reply_to)

    async def send_video(self, chat_id, video_path, caption=None, reply_to=None, metadata=None, **kwargs):
        return await self._send_local_media(chat_id, video_path, "video", caption, reply_to)

    async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                            reply_to=None, metadata=None, **kwargs):
        try:
            result = await self.send_agent_media(
                Target.parse(chat_id), "file", str(file_path), caption=caption,
                file_name=file_name, reply_to=reply_to)
            ids = result.get("message_ids", [])
            return SendResult(success=result["success"], message_id=ids[-1] if ids else None,
                              error=result.get("error"), raw_response=result, retryable=False)
        except (OneBotError, MediaError, ValueError, OSError, PermissionError) as exc:
            return self._failure(exc)

    async def get_chat_info(self, chat_id):
        target = Target.parse(chat_id)
        if not self.policy.can_send(target):
            raise PermissionError("target is not allowlisted")
        action = "get_group_info" if target.kind == "group" else "get_stranger_info"
        result = await self.transport.call(action, target.params)
        name = result.get("group_name") or result.get("nickname") if isinstance(result, dict) else None
        return {"name": name or target.address, "type": "group" if target.kind == "group" else "dm"}
