"""Short-lived, account/conversation-bound media handles; never expose locators."""
from __future__ import annotations

import math
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from .config import MediaReferenceSettings
from .media import Downloaded, MediaError
from .protocol import Incoming, Target

_FILE_ID = re.compile(r"[A-Za-z0-9_{}().+\-]{1,256}\Z")


MEDIA_KINDS = frozenset(("image", "record", "video", "file"))


def media_file_id(value: Any) -> str | None:
    """Accept opaque OneBot IDs, never a received local path, URI or inline payload."""
    if isinstance(value, str) and value not in (".", "..") and _FILE_ID.fullmatch(value):
        return value
    return None


def media_url(value: Any) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= 8192:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() in ("http", "https") and parsed.hostname:
            return value
    except ValueError:
        pass
    return None


def _metadata(value: Any, limit: int) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= limit and all(
        ord(char) >= 32 and ord(char) != 127 for char in value) else None


@dataclass(frozen=True)
class MediaReference:
    media_id: str
    self_id: str
    address: str
    message_id: str
    user_id: str
    kind: str
    attachment_index: int
    file_id: str | None
    url: str | None
    timestamp: float
    expires_at: float
    downloaded: Downloaded | None = None
    name: str | None = None
    mime: str | None = None
    image_index: int | None = None
    node_path: tuple[int, ...] = ()
    notice_only: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id, "type": self.kind, "message_id": self.message_id,
            "attachment_index": self.attachment_index, "sender_id": self.user_id,
            "expires_at": self.expires_at,
            **({"image_index": self.image_index} if self.kind == "image" else {}),
            **({"name": self.name} if self.name else {}),
            **({"mime": self.mime} if self.mime else {}),
            **({"node_path": list(self.node_path)} if self.node_path else {}),
            **({"notice_only": True} if self.notice_only else {}),
        }


class MediaReferences:
    def __init__(self, self_id: str, config: MediaReferenceSettings, *, per_message: int = 4):
        self.self_id, self.config, self.per_message = self_id, config, per_message
        self._items: OrderedDict[str, MediaReference] = OrderedDict()
        self._keys: dict[tuple[str, str, tuple[int, ...], int], str] = {}
        self._seen: OrderedDict[tuple[str, str], tuple[str, float]] = OrderedDict()
        self._recalled: OrderedDict[tuple[str, str], float] = OrderedDict()
        # Overflow may reduce history availability, but must not resurrect an old recall.
        self._history_floor = 0.0

    def _drop(self, identifier: str) -> None:
        item = self._items.pop(identifier, None)
        if item is not None:
            self._keys.pop((item.address, item.message_id, item.node_path, item.attachment_index), None)

    def _prune(self) -> None:
        now = time.time()
        for identifier, item in list(self._items.items()):
            if item.expires_at <= now:
                self._drop(identifier)
        for key, expiry in list(self._recalled.items()):
            if expiry <= now:
                self._recalled.pop(key, None)

    def remember(self, incoming: Incoming, *, timestamp: float | None = None,
                 node_path: tuple[int, ...] = (), notice_only: bool = False) -> tuple[MediaReference, ...]:
        if not self.config.enabled or incoming.self_id != self.self_id:
            return ()
        if (not isinstance(node_path, tuple) or len(node_path) > 16
                or any(type(index) is not int or index < 0 for index in node_path)):
            raise MediaError("invalid forwarded attachment path")
        notice_only = notice_only or incoming.raw.get("_napcat_file_notice") is True
        self._prune()
        now = time.time()
        stamp = timestamp if timestamp is not None else incoming.raw.get("time", now)
        if (isinstance(stamp, bool) or not isinstance(stamp, (float, int))
                or not math.isfinite(stamp) or stamp <= 0 or stamp > now + 300):
            return ()
        stamp = min(float(stamp), now)
        if stamp + self.config.ttl_seconds <= now or stamp < self._history_floor:
            return ()
        address = incoming.target.address
        if self.is_recalled(address, incoming.message_id):
            return ()
        message_key = (address, incoming.message_id)
        seen = self._seen.get(message_key)
        if seen is not None:
            author, stamp = seen
            if author != incoming.user_id or stamp + self.config.ttl_seconds <= now:
                return ()
        else:
            self._seen[message_key] = (incoming.user_id, stamp)
            while len(self._seen) > self.config.max_entries:
                self._seen.popitem(last=False)
                self._history_floor = max(self._history_floor, now - self.config.ttl_seconds)
        result = []
        parts = [part for part in incoming.segments if part["type"] in MEDIA_KINDS]
        image_index = -1
        for index, part in enumerate(parts[:self.per_message]):
            kind, data = part["type"], part["data"]
            if kind == "image":
                image_index += 1
            key = (address, incoming.message_id, node_path, index)
            file_id = media_file_id(data.get("file_id")) or media_file_id(data.get("file"))
            url = media_url(data.get("url"))
            name = _metadata(data.get("name") or data.get("file_name"), 256)
            if name is not None and any(char in name for char in ("/", "\\")):
                name = None
            mime = _metadata(data.get("mime") or data.get("content_type"), 128)
            if mime is not None:
                mime = mime.lower() if re.fullmatch(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", mime) else None
            previous = self._keys.get(key)
            if previous in self._items:
                item = self._items[previous]
                if item.kind != kind or item.user_id != incoming.user_id:
                    continue
                changed = file_id is not None and file_id != item.file_id
                item = replace(item, file_id=file_id or item.file_id, url=url or item.url,
                               name=name or item.name, mime=mime or item.mime,
                               downloaded=None if changed else item.downloaded)
                self._items[previous] = item
                result.append(item)
                continue
            if file_id is None and url is None:
                continue
            identifier = ("qqimg_" if kind == "image" else "qqmedia_") + secrets.token_urlsafe(18)
            item = MediaReference(identifier, self.self_id, address, incoming.message_id,
                                  incoming.user_id, kind, index, file_id, url, stamp,
                                  stamp + self.config.ttl_seconds, name=name, mime=mime,
                                  image_index=image_index if kind == "image" else None,
                                  node_path=node_path, notice_only=notice_only)
            self._items[identifier] = item
            self._keys[key] = identifier
            result.append(item)
            while len(self._items) > self.config.max_entries:
                self._drop(next(iter(self._items)))
        return tuple(result)

    def get(self, identifier: str, target: Target) -> MediaReference:
        self._prune()
        item = self._items.get(identifier) if isinstance(identifier, str) else None
        if (not self.config.enabled or item is None or item.self_id != self.self_id
                or item.address != target.address
                or self.is_recalled(item.address, item.message_id)):
            raise MediaError("media reference is unavailable, expired, recalled or belongs to another chat")
        return item

    def set_downloaded(self, identifier: str, target: Target, downloaded: Downloaded) -> None:
        item = self.get(identifier, target)  # Recheck after an awaited transfer.
        self._items[identifier] = replace(item, downloaded=downloaded)

    def for_message(self, target: Target, message_id: str) -> tuple[MediaReference, ...]:
        self._prune()
        return tuple(item for item in self._items.values()
                     if item.address == target.address and item.message_id == message_id
                     and not self.is_recalled(item.address, item.message_id))

    def recent(self, incoming: Incoming) -> tuple[MediaReference, ...]:
        self._prune()
        now = time.time()
        current_stamp = incoming.raw.get("time", now)
        if not isinstance(current_stamp, (float, int)) or isinstance(current_stamp, bool):
            return ()
        items = [item for item in self._items.values()
                 if item.address == incoming.target.address and item.user_id == incoming.user_id
                 and item.message_id != incoming.message_id
                 and now - self.config.recent_seconds <= item.timestamp <= current_stamp
                 and not self.is_recalled(item.address, item.message_id)]
        items.sort(key=lambda item: (item.timestamp, item.attachment_index), reverse=True)
        return tuple(reversed(items[:self.config.recent_limit]))

    def is_recalled(self, address: str, message_id: str) -> bool:
        return self._recalled.get((address, message_id), 0) > time.time()

    def recall(self, target: Target, message_id: str) -> None:
        self._prune()
        key = (target.address, message_id)
        now = time.time()
        self._recalled[key] = now + self.config.ttl_seconds
        for identifier, item in list(self._items.items()):
            if (item.address, item.message_id) == key:
                self._drop(identifier)
        while len(self._recalled) > self.config.max_entries:
            self._recalled.popitem(last=False)
            self._history_floor = now

    def clear(self) -> None:
        self._items.clear()
        self._keys.clear()
        self._seen.clear()
        self._recalled.clear()
        self._history_floor = 0.0


class ReferencedMedia(str):
    """A string on the wire, with a local capability checked again at the send gate."""

    def __new__(cls, value: str, media_id: str, requester_id: str, kind: str):
        result = super().__new__(cls, value)
        result.media_id = media_id
        result.requester_id = requester_id
        result.kind = kind
        return result
