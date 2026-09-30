"""Media boundary shared by private/group delivery and model-callable QQ tools."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from .media import Downloaded, MediaError
from .media_refs import ReferencedMedia, image_file_id
from .protocol import Incoming, Target, message_id
from .transport import OneBotError

log = logging.getLogger(__name__)


class MediaAdapterMixin:
    def _consume_media_recall(self, raw: dict[str, Any]) -> bool:
        if raw.get("post_type") != "notice":
            return False
        if str(raw.get("self_id")) != self.settings.self_id:
            return True
        kind = {"group_recall": "group", "friend_recall": "private"}.get(raw.get("notice_type"))
        if kind is not None:
            try:
                target = Target.parse(f"{kind}:{raw.get('group_id' if kind == 'group' else 'user_id')}")
                identifier = message_id(raw.get("message_id"))
                if self.policy.can_send(target):
                    self.media_refs.recall(target, identifier)
            except ValueError:
                pass
        return True

    def _can_read_image_author(self, target: Target, author: str) -> bool:
        return self.policy.can_send(target) and (
            author == self.settings.self_id or self.policy.authorized_user(author)
            or (target.kind == "group" and self.settings.group_context.enabled
                and self.settings.group_context.observe_all_members))

    def remember_verified_images(self, target: Target, identifier: str, data: dict):
        """The caller must first verify the message's conversation through get_msg."""
        if not self.settings.media.enabled or not self.settings.media.references.enabled:
            return ()
        if data.get("self_id") is not None and str(data["self_id"]) != self.settings.self_id:
            return ()
        try:
            if data.get("message_id") is not None and message_id(data["message_id"]) != identifier:
                return ()
        except ValueError:
            return ()
        if data.get("message_type") != target.kind:
            return ()
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        author = str(data.get("user_id") or sender.get("user_id") or "")
        if not self._can_read_image_author(target, author) or "time" not in data:
            return ()
        if sender.get("user_id") is not None and str(sender["user_id"]) != author:
            return ()
        if target.kind == "group":
            if str(data.get("group_id")) != target.id:
                return ()
        else:
            destination = str(data.get("target_id") or "")
            if author == target.id:
                if destination not in ("", target.id, self.settings.self_id):
                    return ()
            elif author != self.settings.self_id or not (
                destination == target.id or self.policy.own.contains((target.address, identifier))
            ):
                return ()
        try:
            incoming = Incoming.parse({**data, "post_type": "message",
                                       "self_id": self.settings.self_id,
                                       "user_id": author, "message_id": identifier})
        except (ValueError, TypeError):
            return ()
        if incoming is None:
            return ()
        return self.media_refs.remember(replace(incoming, target=target))

    def _image_access(self, identifier: str, target: Target, requester_id: str):
        if (not self.settings.media.enabled or not self.policy.authorized_user(requester_id)
                or not self.policy.can_send(target)):
            raise PermissionError("current user or conversation cannot access media")
        item = self.media_refs.get(identifier, target)
        if not self._can_read_image_author(target, item.user_id):
            raise PermissionError("image author is outside the observation policy")
        return item

    async def _download_image(self, data: dict[str, Any]) -> Downloaded:
        if self.media is None:
            raise MediaError("media store is not ready")
        url = data.get("url")
        error = None
        if isinstance(url, str) and url:
            try:
                return await self.media.download(url, kind="image")
            except MediaError as exc:
                error = exc
        file_id = image_file_id(data.get("file"))
        if file_id is not None:
            try:
                info = await self.transport.call("get_image", {"file": file_id})
            except OneBotError as exc:
                raise MediaError("image URL refresh failed") from exc
            refreshed = info.get("url") if isinstance(info, dict) else None
            if isinstance(refreshed, str) and refreshed:
                # Exactly one refresh; even its redirects use the inbound safe downloader.
                return await self.media.download(refreshed, kind="image")
        if error is not None:
            raise error
        # A NapCat path is on another machine, and must never become a Hermes file read.
        raise MediaError("image has no usable URL or refreshable file identifier")

    async def resolve_media(self, identifier: str, target: Target, requester_id: str) -> Downloaded:
        self._image_access(identifier, target, requester_id)
        async with self._media_read_gate:
            item = self._image_access(identifier, target, requester_id)
            if self.media is None:
                raise MediaError("media store is not ready")
            if item.downloaded is not None:
                try:
                    self.media.validate_cached(item.downloaded)
                    return item.downloaded
                except (MediaError, OSError):
                    pass  # Re-fetch an evicted or changed cache object through the same policy.
            downloaded = await self._download_image({"url": item.url, "file": item.file_id})
            self._image_access(identifier, target, requester_id)
            self.media_refs.set_downloaded(identifier, target, downloaded)
            return downloaded

    async def _attachments(self, incoming: Incoming):
        parts = [p for p in incoming.segments if p["type"] in ("image", "record", "video", "file")]
        config = self.settings.media
        if self.media_refs.is_recalled(incoming.target.address, incoming.message_id):
            return [], [], ["[当前图片消息已撤回，未读取其内容]"]
        if not config.enabled:
            return [], [], ["[已关闭附件下载，本次未读取附件内容]"] if parts else []
        refs = self.media_refs.remember(incoming)
        by_index = {ref.image_index: ref for ref in refs}
        received: list[tuple[Downloaded, str | None]] = []
        notices: list[str] = []
        image_index = 0
        for part in parts[:config.max_attachments]:
            kind, data = part["type"], part["data"]
            ref = by_index.get(image_index) if kind == "image" else None
            if kind == "image":
                image_index += 1
            try:
                if self.media is None:
                    raise MediaError("media store is not ready")
                if ref is not None:
                    downloaded = await self.resolve_media(ref.media_id, incoming.target, incoming.user_id)
                elif kind == "image":
                    downloaded = await self._download_image(data)
                elif not data.get("url"):
                    raise MediaError("attachment has no accessible URL")
                else:
                    downloaded = await self.media.download(data["url"], kind=kind)
                received.append((downloaded, ref.media_id if ref is not None else None))
            except (MediaError, OneBotError, PermissionError, OSError) as exc:
                log.warning("Inbound media rejected (%s)", type(exc).__name__)
                notices.append(f"[{kind} 附件未读取：下载失败或安全/大小限制]")
        if len(parts) > config.max_attachments:
            notices.append("[附件数量超过本次处理上限，其余附件未读取]")

        # No automatic semantic guessing: quotes take precedence; recent fallback is explicit,
        # bounded by time and restricted to the current speaker's own images.
        extra = ()
        if config.references.enabled and not parts:
            if incoming.reply_to and config.references.attach_quoted:
                if self.media_refs.is_recalled(incoming.target.address, incoming.reply_to):
                    notices.append("[引用图片已撤回，未读取其内容]")
                extra = self.media_refs.for_message(incoming.target, incoming.reply_to)
                if not extra and not self.media_refs.is_recalled(incoming.target.address, incoming.reply_to):
                    try:
                        data = await self.verified_message(incoming.target, incoming.reply_to)
                        extra = self.remember_verified_images(incoming.target, incoming.reply_to, data)
                        segments = data.get("message")
                        if not extra and isinstance(segments, list) and any(
                            p.get("type") == "image" for p in segments if isinstance(p, dict)
                        ):
                            notices.append("[引用图片已过期或不可见，未读取其内容]")
                    except (OneBotError, ValueError, PermissionError):
                        notices.append("[引用图片不可用，未读取其内容]")
            elif config.references.attach_recent:
                extra = self.media_refs.recent(incoming)
        for ref in extra[:max(0, config.max_attachments - len(received))]:
            try:
                downloaded = await self.resolve_media(ref.media_id, incoming.target, incoming.user_id)
                received.append((downloaded, ref.media_id))
            except (MediaError, OneBotError, PermissionError, OSError):
                notices.append("[历史图片不可用、已过期或已撤回，未读取其内容]")
        if self.media_refs.is_recalled(incoming.target.address, incoming.message_id):
            return [], [], ["[当前图片消息已撤回，未传入模型]"]
        paths, mimes = [], []
        for downloaded, identifier in received:
            if identifier is not None:
                try:
                    ref = self._image_access(identifier, incoming.target, incoming.user_id)
                except (MediaError, PermissionError):
                    notices.append("[图片引用已失效，本次未传入模型]")
                    continue
                notices.append(
                    f"[图片 {len(paths) + 1}: media:{identifier}; "
                    f"message_id={ref.message_id}; sender_id={ref.user_id}; index={ref.image_index}]")
            paths.append(str(downloaded.path))
            mimes.append(downloaded.mime)
        return paths, mimes, notices

    async def outbound_reference(self, source: str, *, kind: str,
                                 target: Target | None = None, requester_id: str | None = None) -> str:
        if self.media is None:
            raise MediaError("media store is not ready")
        if not isinstance(source, str) or not source.strip():
            raise MediaError("media source is required")
        source = source.strip()
        if source.startswith("media:"):
            if kind != "image" or target is None or requester_id is None:
                raise MediaError("image references require an authenticated same-chat tool call")
            identifier = source.removeprefix("media:")
            downloaded = await self.resolve_media(identifier, target, requester_id)
            value = await asyncio.to_thread(self.media.cached_reference, downloaded)
            self._image_access(identifier, target, requester_id)
            return ReferencedMedia(value, identifier, requester_id)
        try:
            scheme = urlsplit(source).scheme.lower()
        except ValueError as exc:
            raise MediaError("invalid media source") from exc
        if scheme in ("http", "https"):
            downloaded = await self.media.download(source, kind=kind, direction="outbound")
            return await asyncio.to_thread(self.media.cached_reference, downloaded)
        windows_drive = len(source) >= 3 and source[0].isalpha() and source[1:3] in (":/", ":\\")
        if scheme and not windows_drive:
            raise MediaError("media source must be an allowed local path, media reference or http(s) URL")
        return await asyncio.to_thread(self.media.outbound_reference, source, kind=kind,
                                       max_bytes=self.settings.qq_tools.max_local_media_bytes)

    def validate_outbound_media(self, target: Target, value: Any) -> None:
        """Called inside the send gate: recall/expiry while queued must stop the send."""
        if isinstance(value, ReferencedMedia):
            self._image_access(value.media_id, target, value.requester_id)
        elif isinstance(value, dict):
            for item in value.values():
                self.validate_outbound_media(target, item)
        elif isinstance(value, list):
            for item in value:
                self.validate_outbound_media(target, item)
