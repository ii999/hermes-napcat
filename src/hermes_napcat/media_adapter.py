"""Media boundary shared by private/group delivery and model-callable QQ tools."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from .media import Downloaded, InlineTooLarge, MediaError, inline_info, is_inline_source
from .media_refs import MEDIA_KINDS, MediaReference, ReferencedMedia, media_file_id, media_url
from .protocol import Incoming, Target, message_id
from .transport import OneBotError
from .stream_upload import StreamedMedia, StreamUploader

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

    def _can_read_media_author(self, target: Target, author: str) -> bool:
        return self.policy.can_send(target) and (
            author == self.settings.self_id or self.policy.authorized_user(author)
            or (target.kind == "group" and self.settings.group_context.enabled
                and self.settings.group_context.observe_all_members))

    def _verified_media_incoming(self, target: Target, identifier: str,
                                 data: dict[str, Any]) -> Incoming | None:
        """The caller must first verify the message's conversation through get_msg."""
        if not self.settings.media.enabled or not self.settings.media.references.enabled:
            return None
        if data.get("self_id") is not None and str(data["self_id"]) != self.settings.self_id:
            return None
        try:
            if data.get("message_id") is not None and message_id(data["message_id"]) != identifier:
                return None
        except ValueError:
            return None
        if data.get("message_type") != target.kind:
            return None
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        author = str(data.get("user_id") or sender.get("user_id") or "")
        if not self._can_read_media_author(target, author) or "time" not in data:
            return None
        if sender.get("user_id") is not None and str(sender["user_id"]) != author:
            return None
        if target.kind == "group":
            if str(data.get("group_id")) != target.id:
                return None
        else:
            destination = str(data.get("target_id") or "")
            if author == target.id:
                if destination not in ("", target.id, self.settings.self_id):
                    return None
            elif author != self.settings.self_id or not (
                destination == target.id
                or (not destination and self.policy.own.contains((target.address, identifier)))
            ):
                return None
        try:
            incoming = Incoming.parse({**data, "post_type": "message",
                                       "self_id": self.settings.self_id,
                                       "user_id": author, "message_id": identifier})
        except (ValueError, TypeError):
            return None
        if incoming is None:
            return None
        return replace(incoming, target=target)

    def remember_verified_media(self, target: Target, identifier: str,
                                data: dict[str, Any]) -> tuple[MediaReference, ...]:
        incoming = self._verified_media_incoming(target, identifier, data)
        return self.media_refs.remember(incoming) if incoming is not None else ()

    def remember_forward_media(self, target: Target, parent_id: str,
                               parent_data: dict[str, Any], node_path: tuple[int, ...],
                               node_data: dict[str, Any]) -> tuple[MediaReference, ...]:
        """Bind verified forward-node locators to the enclosing message's author and lifetime."""
        incoming = self._verified_media_incoming(target, parent_id, parent_data)
        if incoming is None or not node_path:
            return ()
        content = node_data.get("content", node_data.get("message"))
        if not isinstance(content, list):
            return ()
        try:
            node = Incoming.parse({**incoming.raw, "post_type": "message",
                                   "self_id": incoming.self_id, "user_id": incoming.user_id,
                                   "sender": {"user_id": incoming.user_id},
                                   "message_id": incoming.message_id, "message": content})
        except (ValueError, TypeError):
            return ()
        return self.media_refs.remember(replace(node, target=target), node_path=node_path) if node else ()

    def _media_access(self, identifier: str, target: Target, requester_id: str, *, kind: str | None = None):
        if (not self.settings.media.enabled or not self.policy.authorized_user(requester_id)
                or not self.policy.can_send(target)):
            raise PermissionError("current user or conversation cannot access media")
        item = self.media_refs.get(identifier, target)
        if not self._can_read_media_author(target, item.user_id):
            raise PermissionError("media author is outside the observation policy")
        if kind is not None and item.kind != kind:
            raise MediaError("media reference kind does not match the requested segment")
        return item

    async def _download_http(self, *, kind: str, file_id: str | None, url: str | None,
                             access_check, max_bytes: int | None = None) -> Downloaded:
        if self.media is None:
            raise MediaError("media store is not ready")
        access_check()
        if url is None and kind == "image" and file_id is not None:
            info = await self.transport.call("get_image", {"file": file_id})
            access_check()
            url = media_url(info.get("url")) if isinstance(info, dict) else None
        if url is None:
            raise MediaError("media has no safe HTTP locator")
        result = await self.media.download(url, kind=kind, max_bytes=max_bytes)
        access_check()
        return result

    async def _download_media(self, *, kind: str, file_id: str | None, url: str | None,
                              access_check, max_bytes: int | None = None) -> Downloaded:
        from .stream_download import StreamDownloader, StreamDownloadUnsupported

        if self.media is None:
            raise MediaError("media store is not ready")
        mode = self.settings.media.download_mode
        if mode == "http":
            return await self._download_http(kind=kind, file_id=file_id, url=url,
                                             access_check=access_check, max_bytes=max_bytes)
        if file_id is not None:
            downloader = getattr(self, "_stream_downloader", None)
            if downloader is None:
                downloader = self._stream_downloader = StreamDownloader(
                    self.transport, self.settings, self.media)
            try:
                return await downloader.download(file_id, kind=kind, access_check=access_check,
                                                 max_bytes=max_bytes)
            except StreamDownloadUnsupported:
                if mode == "stream":
                    raise
                log.warning("NapCat media stream action is unsupported; using safe HTTP resolution")
        elif mode == "stream":
            raise MediaError("stream download requires a safe opaque file identifier")
        else:
            log.warning("Media has no safe opaque file identifier; using its safe HTTP locator")
        return await self._download_http(kind=kind, file_id=file_id, url=url,
                                         access_check=access_check, max_bytes=max_bytes)

    async def _refresh_media(self, item: MediaReference, target: Target, requester_id: str):
        if item.node_path or item.notice_only:
            raise MediaError("media locator is stale; refresh the verified forward or file history")
        data = await self.verified_message(target, item.message_id)
        self._media_access(item.media_id, target, requester_id)
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        author = str(data.get("user_id") or sender.get("user_id") or "")
        if author != item.user_id:
            raise PermissionError("refreshed media author does not match the original message")
        refs = self.remember_verified_media(target, item.message_id, data)
        updated = next((ref for ref in refs if ref.media_id == item.media_id), None)
        if updated is None or updated.kind != item.kind:
            raise MediaError("refreshed message does not contain the referenced attachment")
        return self._media_access(item.media_id, target, requester_id, kind=item.kind)

    async def resolve_media(self, identifier: str, target: Target, requester_id: str, *,
                            max_bytes: int | None = None) -> Downloaded:
        from .stream_download import StreamFileUnavailable

        if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
            raise MediaError("media byte budget must be a positive integer")
        self._media_access(identifier, target, requester_id)
        async with self._media_read_gate:
            item = self._media_access(identifier, target, requester_id)
            if self.media is None:
                raise MediaError("media store is not ready")
            if item.downloaded is not None:
                try:
                    self.media.validate_cached(item.downloaded)
                except (MediaError, OSError):
                    pass
                else:
                    if max_bytes is not None and item.downloaded.size > max_bytes:
                        raise MediaError("media exceeds remaining turn byte budget")
                    return item.downloaded
            def access_check():
                return self._media_access(identifier, target, requester_id, kind=item.kind)

            try:
                downloaded = await self._download_media(
                    kind=item.kind, file_id=item.file_id, url=item.url,
                    access_check=access_check, max_bytes=max_bytes)
            except MediaError as exc:
                stale_http = (self.settings.media.download_mode in ("auto", "http") and str(exc) in (
                    "media server returned HTTP 403", "media server returned HTTP 404",
                    "media server returned HTTP 410"))
                if not isinstance(exc, StreamFileUnavailable) and not stale_http:
                    raise
                updated = await self._refresh_media(item, target, requester_id)
                try:
                    downloaded = await self._download_media(
                        kind=updated.kind, file_id=updated.file_id, url=updated.url,
                        access_check=access_check, max_bytes=max_bytes)
                except StreamFileUnavailable:
                    if self.settings.media.download_mode != "auto":
                        raise
                    log.warning("NapCat cannot resolve the refreshed media ID; using safe HTTP resolution")
                    downloaded = await self._download_http(
                        kind=updated.kind, file_id=updated.file_id, url=updated.url,
                        access_check=access_check, max_bytes=max_bytes)
            access_check()
            if max_bytes is not None and downloaded.size > max_bytes:
                raise MediaError("media exceeds remaining turn byte budget")
            self.media_refs.set_downloaded(identifier, target, downloaded)
            return downloaded

    async def _attachments(self, incoming: Incoming):
        parts = [part for part in incoming.segments if part["type"] in MEDIA_KINDS]
        config = self.settings.media
        if self.media_refs.is_recalled(incoming.target.address, incoming.message_id):
            return [], [], ["[当前附件消息已撤回，未读取其内容]"]
        if not config.enabled:
            return [], [], ["[已关闭附件下载，本次未读取附件内容]"] if parts else []
        refs = self.media_refs.remember(incoming)
        by_index = {ref.attachment_index: ref for ref in refs}
        notices: list[str] = []
        received: list[tuple[Downloaded, Any]] = []
        remaining = config.max_turn_bytes
        attempted = 0
        limited = False

        def exhausted() -> bool:
            return attempted >= config.max_attachments or remaining <= 0

        def current_access():
            if (not self.policy.authorized_user(incoming.user_id)
                    or not self._can_read_media_author(incoming.target, incoming.user_id)):
                raise PermissionError("current user or conversation cannot access media")
            if self.media_refs.is_recalled(incoming.target.address, incoming.message_id):
                raise MediaError("current attachment message was recalled")

        async def attach(*, kind: str, ref=None, data=None):
            nonlocal remaining, attempted
            allowance = min(config.max_bytes, remaining)
            attempted += 1
            remaining -= allowance
            try:
                if ref is not None:
                    downloaded = await self.resolve_media(
                        ref.media_id, incoming.target, incoming.user_id, max_bytes=allowance)
                elif data is not None and not config.references.enabled:
                    downloaded = await self._download_media(
                        kind=kind, file_id=media_file_id(data.get("file_id")) or media_file_id(data.get("file")),
                        url=media_url(data.get("url")), access_check=current_access, max_bytes=allowance)
                else:
                    raise MediaError("attachment has no usable reference")
                if downloaded.size > allowance:
                    raise MediaError("media exceeds remaining turn byte budget")
                received.append((downloaded, ref))
                remaining += allowance - downloaded.size
            except (MediaError, OneBotError, PermissionError, OSError) as exc:
                log.warning("Inbound media rejected (%s)", type(exc).__name__)
                notices.append(f"[{kind} 附件未读取：下载失败或安全/大小限制]")
                # A failed transfer may already have read its whole allowance.
                # Keep that reservation rather than guessing how many bytes arrived.

        for index, part in enumerate(parts):
            if exhausted():
                limited = True
                break
            await attach(kind=part["type"], ref=by_index.get(index), data=part["data"])

        extra = ()
        if config.references.enabled:
            if incoming.reply_to and config.references.attach_quoted:
                if self.media_refs.is_recalled(incoming.target.address, incoming.reply_to):
                    notices.append("[引用附件已撤回，未读取其内容]")
                else:
                    extra = self.media_refs.for_message(incoming.target, incoming.reply_to)
                    if not extra and not exhausted():
                        try:
                            data = await self.verified_message(incoming.target, incoming.reply_to)
                            extra = self.remember_verified_media(incoming.target, incoming.reply_to, data)
                            segments = data.get("message")
                            if not extra and isinstance(segments, list) and any(
                                p.get("type") in MEDIA_KINDS for p in segments if isinstance(p, dict)
                            ):
                                notices.append("[引用附件已过期或不可见，未读取其内容]")
                        except (OneBotError, ValueError, PermissionError):
                            notices.append("[引用附件不可用，未读取其内容]")
            elif not incoming.reply_to and not parts and config.references.attach_recent:
                extra = self.media_refs.recent(incoming)
        current_ids = {ref.media_id for ref in refs}
        for ref in extra:
            if ref.media_id not in current_ids:
                if exhausted():
                    limited = True
                    break
                await attach(kind=ref.kind, ref=ref)
        if limited:
            notices.append("[附件数量或总字节数超过本次处理上限，其余附件未读取]")
        if self.media_refs.is_recalled(incoming.target.address, incoming.message_id):
            return [], [], ["[当前附件消息已撤回，未传入模型]"]
        paths, mimes = [], []
        for downloaded, ref in received:
            if ref is not None:
                try:
                    ref = self._media_access(ref.media_id, incoming.target, incoming.user_id, kind=ref.kind)
                except (MediaError, PermissionError):
                    notices.append("[附件引用已失效，本次未传入模型]")
                    continue
                notices.append(
                    f"[{ref.kind} 附件 {len(paths) + 1}: media:{ref.media_id}; "
                    f"message_id={ref.message_id}; sender_id={ref.user_id}; index={ref.attachment_index}]")
            else:
                current_access()
            paths.append(str(downloaded.path))
            mimes.append(downloaded.mime)
        return paths, mimes, notices

    async def _close_streams(self) -> None:
        self._stream_downloader = None
        uploader = getattr(self, "_stream_uploader", None)
        if uploader is not None:
            await uploader.close()
            self._stream_uploader = None

    def _inline_limit(self) -> int:
        # Preserve explicitly configured smaller WS/inline limits across upgrades.
        limit = min(self.settings.media.inline_max_bytes,
                    max(0, (self.settings.ws_max_bytes - 16384) // 4) * 3)
        return 0 if self.settings.media.streaming.mode == "always" else limit

    async def _media_reference(self, function, *args, **kwargs) -> str:
        limit = self._inline_limit()
        try:
            return await asyncio.to_thread(function, *args, inline_limit=limit, **kwargs)
        except InlineTooLarge as exc:
            uploader = getattr(self, "_stream_uploader", None)
            if uploader is None:
                uploader = self._stream_uploader = StreamUploader(self.transport, self.settings)
            return await uploader.upload(exc.path, exc.size)

    async def outbound_reference(self, source: str, *, kind: str,
                                 target: Target | None = None, requester_id: str | None = None) -> str:
        if self.media is None:
            raise MediaError("media store is not ready")
        if not isinstance(source, str) or not source.strip():
            raise MediaError("media source is required")
        source = source.strip()
        if source.startswith("media:"):
            if kind not in MEDIA_KINDS or target is None or requester_id is None:
                raise MediaError("media references require an authenticated same-chat tool call")
            identifier = source.removeprefix("media:")
            self._media_access(identifier, target, requester_id, kind=kind)
            downloaded = await self.resolve_media(identifier, target, requester_id)
            value = await self._media_reference(self.media.cached_reference, downloaded)
            self._media_access(identifier, target, requester_id, kind=kind)
            reference = ReferencedMedia(value, identifier, requester_id, kind)
            reference.transport_reference = value
            return reference
        if is_inline_source(source):
            _, size, _ = inline_info(source, min(self.settings.media.base64_max_bytes,
                                                 self.settings.media.max_bytes))
            if size <= self._inline_limit() and self.media.shared_root is None:
                return await asyncio.to_thread(self.media.inline_reference, source, kind=kind)
            downloaded = await asyncio.to_thread(self.media.import_inline, source, kind=kind)
            return await self._media_reference(self.media.cached_reference, downloaded)
        if len(source) > 8192:
            raise MediaError("media path or URL is too long")
        try:
            scheme = urlsplit(source).scheme.lower()
        except ValueError as exc:
            raise MediaError("invalid media source") from exc
        if scheme in ("http", "https"):
            downloaded = await self.media.download(source, kind=kind, direction="outbound")
            return await self._media_reference(self.media.cached_reference, downloaded)
        windows_drive = len(source) >= 3 and source[0].isalpha() and source[1:3] in (":/", ":\\")
        if scheme and not windows_drive:
            raise MediaError("media source must be an allowed local path, media reference or http(s) URL")
        return await self._media_reference(self.media.outbound_reference, source, kind=kind,
                                       max_bytes=self.settings.qq_tools.max_local_media_bytes)

    def validate_outbound_media(self, target: Target, value: Any) -> None:
        """Called inside the send gate: recall/expiry while queued must stop the send."""
        if isinstance(value, ReferencedMedia):
            self._media_access(value.media_id, target, value.requester_id, kind=value.kind)
            self.validate_outbound_media(target, getattr(value, "transport_reference", str(value)))
        elif isinstance(value, StreamedMedia):
            value.validate(self.transport)
        elif isinstance(value, dict):
            if value.get("type") == "node" and isinstance(value.get("data"), dict):
                if "id" in value["data"] and self.media_refs.is_recalled(
                    target.address, message_id(value["data"]["id"])
                ):
                    raise MediaError("forwarded source message was recalled")
            if value.get("type") in MEDIA_KINDS and isinstance(value.get("data"), dict):
                source = value["data"].get("file")
                if isinstance(source, ReferencedMedia) and source.kind != value["type"]:
                    raise MediaError("media reference kind does not match the send segment")
            for item in value.values():
                self.validate_outbound_media(target, item)
        elif isinstance(value, list):
            for item in value:
                self.validate_outbound_media(target, item)

    def media_send_kwargs(self, target: Target, value: Any) -> dict[str, Any]:
        """Recheck capabilities after the transport lock wait, immediately before writing."""
        return {**self.media_transport_kwargs(value),
                "before_write": lambda: self.validate_outbound_media(target, value)}

    def media_transport_kwargs(self, value: Any) -> dict[str, int]:
        """Pin sends of staged files to their original WS epoch, including lock wait."""
        if isinstance(value, ReferencedMedia):
            return self.media_transport_kwargs(getattr(value, "transport_reference", str(value)))
        if isinstance(value, StreamedMedia):
            return {"expected_epoch": value.epoch}
        result: dict[str, int] = {}
        values = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
        for item in values:
            found = self.media_transport_kwargs(item)
            if found and result and found != result:
                raise MediaError("media uploads belong to different connections")
            result.update(found)
        return result
