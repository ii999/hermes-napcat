"""File-notice normalization and bounded deduplication after authorization admission."""
from __future__ import annotations

import hashlib
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .config import Settings, numeric_id
from .media_refs import media_file_id
from .protocol import Incoming, Target


@dataclass(frozen=True)
class _SeenFile:
    expires_at: float
    notice_id: str | None = None
    ordinary_id: str | None = None


class FileNotices:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ttl = max(settings.media.references.ttl_seconds,
                       settings.group_context.history_window_seconds,
                       settings.qq_tools.history_window_seconds)
        self.capacity = settings.dedup_capacity
        self._files: OrderedDict[tuple[str, str, str], _SeenFile] = OrderedDict()
        self._notices: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._messages: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._undo: OrderedDict[tuple[str, str], list[tuple[tuple[str, str, str], _SeenFile | None]]] = OrderedDict()

    def _prune(self) -> None:
        now = time.time()
        for key, seen in list(self._files.items()):
            if seen.expires_at <= now:
                self._files.pop(key, None)
        for key, expiry in list(self._notices.items()):
            if expiry <= now:
                self._notices.pop(key, None)
        for key, expiry in list(self._messages.items()):
            if expiry <= now:
                self._messages.pop(key, None)
                self._undo.pop(key, None)

    def normalize(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        """Validate shape only; this operation never grants ACL or consumes a dedup key."""
        if raw.get("post_type") != "notice":
            return raw
        kind = raw.get("notice_type")
        if kind not in ("offline_file", "group_upload"):
            return raw
        if not self.settings.media.enabled:
            return None
        try:
            account = numeric_id(raw.get("self_id"))
            author = numeric_id(raw.get("user_id"))
            target = Target("group", numeric_id(raw.get("group_id"))) if kind == "group_upload" else Target("private", author)
        except ValueError:
            return None
        if account != self.settings.self_id:
            return None
        file = raw.get("file")
        if not isinstance(file, dict):
            return None
        file_id = media_file_id(file.get("id") or file.get("file_id"))
        url = file.get("url")
        url = url if isinstance(url, str) and len(url) <= 8192 and url.startswith(("http://", "https://")) else None
        if file_id is None and url is None:
            return None
        size = file.get("size")
        if type(size) is not int or not 0 < size <= self.settings.media.max_bytes:
            return None
        name = file.get("name")
        if not isinstance(name, str) or not name or len(name) > 200 or any(ord(c) < 32 for c in name):
            return None
        stamp = raw.get("time")
        now = time.time()
        if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                or not math.isfinite(stamp) or not now - self.ttl <= stamp <= now + 300):
            return None
        locator = file_id or url
        identity = "\0".join((account, target.address, author, locator))
        # A reserved numeric namespace also prevents expired/evicted notice IDs reaching get_msg.
        identifier = "-8" + f"{int(hashlib.sha256(identity.encode()).hexdigest()[:24], 16):030d}"
        data: dict[str, Any] = {"name": name, "file_size": size}
        if file_id is not None:
            data["file_id"] = file_id
            data["file"] = file_id
        if url is not None:
            data["url"] = url
        normalized = {
            "post_type": "message", "message_type": target.kind,
            "self_id": account, "user_id": author, "message_id": identifier,
            "time": min(stamp, now), "message": [{"type": "file", "data": data}],
            "sender": {"user_id": author}, "_napcat_file_notice": True,
        }
        if target.kind == "group":
            normalized["group_id"] = target.id
        return normalized

    def _keys(self, incoming: Incoming) -> list[tuple[str, str, str]]:
        keys = []
        for part in incoming.segments:
            if part["type"] != "file":
                continue
            data = part["data"]
            identifier = media_file_id(data.get("file_id") or data.get("file"))
            if identifier is not None:
                keys.append((incoming.target.address, incoming.user_id, identifier))
            elif incoming.raw.get("_napcat_file_notice") is True:
                # URL-only notices deduplicate each other; URLs never deduplicate ordinary messages.
                url = data.get("url")
                if isinstance(url, str):
                    keys.append((incoming.target.address, incoming.user_id,
                                 "url:" + hashlib.sha256(url.encode()).hexdigest()))
            if len(keys) >= self.settings.media.max_attachments:
                break
        return keys

    def admit(self, incoming: Incoming) -> bool:
        """Call only after account, target, sender and rate policy admission."""
        self._prune()
        keys = self._keys(incoming)
        if not keys:
            return True
        has_content = any(part["type"] != "file" for part in incoming.segments)
        notice = incoming.raw.get("_napcat_file_notice") is True
        message_key = (incoming.target.address, incoming.message_id)
        duplicate = message_key in self._messages
        if keys and not duplicate:
            duplicate = all(key in self._files and (
                notice or self._files[key].ordinary_id in (None, incoming.message_id))
                for key in keys)
        expiry = time.time() + self.ttl
        changes = []
        for key in keys:
            previous = self._files.get(key)
            if previous is None:
                updated = _SeenFile(expiry, incoming.message_id if notice else None,
                                    None if notice else incoming.message_id)
            elif notice:
                updated = _SeenFile(previous.expires_at, incoming.message_id, previous.ordinary_id)
            else:
                updated = _SeenFile(previous.expires_at, previous.notice_id, incoming.message_id)
            changes.append((key, previous))
            self._files[key] = updated
        if keys:
            self._messages.setdefault(message_key, expiry)
        accepted = not duplicate or has_content
        if accepted:
            self._undo[message_key] = changes
        if notice:
            self._notices[message_key] = expiry
        while len(self._files) > self.capacity:
            self._files.popitem(last=False)
        while len(self._notices) > self.capacity:
            self._notices.popitem(last=False)
        while len(self._messages) > self.capacity:
            removed, _ = self._messages.popitem(last=False)
            self._undo.pop(removed, None)
        while len(self._undo) > self.capacity:
            self._undo.popitem(last=False)
        return accepted

    def is_notice_message(self, target: Target, identifier: str) -> bool:
        self._prune()
        reserved = (isinstance(identifier, str) and identifier.startswith("-8")
                    and len(identifier) == 32 and identifier[1:].isascii()
                    and identifier[1:].isdecimal())
        return reserved or (target.address, identifier) in self._notices

    def discard(self, incoming: Incoming) -> None:
        """Rollback only this admission's keys; preserve an earlier successful attachment."""
        message_key = (incoming.target.address, incoming.message_id)
        for key, previous in self._undo.pop(message_key, []):
            current = self._files.get(key)
            if current is None or incoming.message_id not in (current.notice_id, current.ordinary_id):
                continue
            if previous is None:
                self._files.pop(key, None)
            else:
                self._files[key] = previous
        self._messages.pop(message_key, None)
        if incoming.raw.get("_napcat_file_notice") is True:
            self._notices.pop((incoming.target.address, incoming.message_id), None)

    def clear(self) -> None:
        self._files.clear()
        self._notices.clear()
        self._messages.clear()
        self._undo.clear()
