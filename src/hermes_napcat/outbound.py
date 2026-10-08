"""Preflight compound host deliveries before sending any QQ message."""
from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from typing import Any

from .media import MediaError
from .protocol import Target, request_bytes, split_text, text_segments
from .transport import DeliveryUncertain, OneBotError


@dataclass(frozen=True)
class PreparedMedia:
    target: Target
    kind: str
    reference: str
    thumbnail: str | None = None
    caption: str | None = None
    file_name: str | None = None
    reply_to: str | None = None

    def requests(self) -> list[tuple[str, dict[str, Any]]]:
        """Build the same ordered wire plan used for preflight and delivery."""
        target = self.target
        if self.kind == "file":
            requests = [(f"upload_{target.kind}_file", {
                **target.params, "file": self.reference, "name": self.file_name,
                "upload_file": True,
            })]
        else:
            data = {"file": self.reference}
            if self.thumbnail is not None:
                data["thumb"] = self.thumbnail
            parts = ([{"type": "reply", "data": {"id": self.reply_to}}]
                     if self.reply_to else [])
            if self.kind == "image" and self.caption:
                parts.append({"type": "text", "data": {"text": self.caption}})
            parts.append({"type": "record" if self.kind == "audio" else self.kind, "data": data})
            requests = [(target.action, {**target.params, "message": parts})]
        if self.kind != "image" and self.caption:
            requests.append((target.action, {**target.params, "message": text_segments(
                self.caption, self.reply_to if self.kind == "file" else None)}))
        return requests


def validate_request_sizes(requests: list[tuple[str, dict[str, Any]]], maximum: int) -> None:
    for action, params in requests:
        if request_bytes(action, params) > maximum:
            raise MediaError("outbound request exceeds ws_max_bytes; use shared media storage")


async def send_bundle(adapter, target: Target, text: str, media_files: Any, *,
                      force_document: bool = False) -> dict[str, Any]:
    """Hermes supplies `(local_path, is_voice)` pairs; all use the normal outbound ACL."""
    ids: list[str] = []
    delivered = 0
    try:
        if not adapter.policy.can_send(target):
            raise PermissionError("target is not allowlisted")
        if not isinstance(text, str) or len(text) > adapter.settings.max_outbound_chars:
            raise ValueError("message exceeds the outbound text contract")
        if type(force_document) is not bool:
            raise ValueError("force_document must be a boolean")
        rows = [] if media_files is None else media_files
        if not isinstance(rows, (list, tuple)) or len(rows) > adapter.settings.qq_tools.max_media_items:
            raise ValueError("media_files must be a bounded list of path/voice pairs")
        if not text.strip() and not rows:
            raise ValueError("delivery requires text or media")
        if adapter.media is None and rows:
            raise MediaError("media store is not ready")
        # Validate every local input before even staging an upload on NapCat.
        files: list[tuple[str, str]] = []
        for row in rows:
            if (not isinstance(row, (list, tuple)) or len(row) != 2
                    or not isinstance(row[0], str) or type(row[1]) is not bool):
                raise ValueError("each media_files entry must be a local path and a voice boolean")
            path = adapter.media.local_path(row[0])
            if not 0 < path.stat().st_size <= adapter.settings.qq_tools.max_local_media_bytes:
                raise MediaError("local media exceeds the configured byte limit or is empty")
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            kind = ("file" if force_document else "audio" if row[1] else
                    "image" if mime.startswith("image/") else
                    "video" if mime.startswith("video/") else "file")
            files.append((str(path), kind))
        prepared = [await adapter.prepare_media(target, kind, path) for path, kind in files]
        text_requests = [(target.action, {**target.params, "message": text_segments(chunk)})
                         for chunk in split_text(text, adapter.settings.message_chars)
                         if text.strip()]
        validate_request_sizes(text_requests + [request for item in prepared
                                                for request in item.requests()],
                               adapter.settings.ws_max_bytes)
        # All references must remain valid together before the first send.
        for item in prepared:
            adapter.validate_outbound_media(target, item.reference)
        for action, params in text_requests:
            ids.append(await adapter._send_action_with_id(target, action, params))
            delivered += 1
        for item in prepared:
            result = await adapter.send_prepared_media(item)
            ids.extend(result.get("message_ids", []))
            if not result.get("success"):
                return {**result, "message_ids": ids, "partial": bool(delivered) or result.get("partial", False),
                        "delivered_items": delivered, "retryable": False}
            delivered += 1
        return {"success": True, "message_ids": ids,
                "message_id": ids[-1] if ids else None, "delivered_items": delivered}
    except (OneBotError, MediaError, ValueError, OSError, PermissionError) as exc:
        return {"success": False, "error": f"QQ delivery failed ({type(exc).__name__})",
                "message_ids": ids, "partial": delivered > 0, "delivered_items": delivered,
                "delivery_uncertain": isinstance(exc, DeliveryUncertain), "retryable": False}
