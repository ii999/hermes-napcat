"""Short-lived, account/conversation-bound image handles; never expose source URLs."""
from __future__ import annotations

import math
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any

from .config import MediaReferenceSettings
from .media import Downloaded, MediaError
from .protocol import Incoming, Target

_FILE_ID = re.compile(r"[A-Za-z0-9_{}().+\-]{1,256}\Z")


def image_file_id(value: Any) -> str | None:
    """Accept opaque OneBot IDs, never a received local path, URI or inline payload."""
    if isinstance(value, str) and value not in (".", "..") and _FILE_ID.fullmatch(value):
        return value
    return None


@dataclass(frozen=True)
class ImageReference:
    media_id: str
    self_id: str
    address: str
    message_id: str
    user_id: str
    image_index: int
    file_id: str | None
    url: str | None
    timestamp: float
    expires_at: float
    downloaded: Downloaded | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id, "type": "image", "message_id": self.message_id,
            "image_index": self.image_index, "sender_id": self.user_id,
            "expires_at": self.expires_at,
        }


class MediaReferences:
    def __init__(self, self_id: str, config: MediaReferenceSettings, *, per_message: int = 4):
        self.self_id, self.config, self.per_message = self_id, config, per_message
        self._items: OrderedDict[str, ImageReference] = OrderedDict()
        self._keys: dict[tuple[str, str, int], str] = {}
        self._recalled: OrderedDict[tuple[str, str], float] = OrderedDict()
        # Overflow may reduce history availability, but must not resurrect an old recall.
        self._history_floor = 0.0

    def _drop(self, identifier: str) -> None:
        item = self._items.pop(identifier, None)
        if item is not None:
            self._keys.pop((item.address, item.message_id, item.image_index), None)

    def _prune(self) -> None:
        now = time.time()
        for identifier, item in list(self._items.items()):
            if item.expires_at <= now:
                self._drop(identifier)
        for key, expiry in list(self._recalled.items()):
            if expiry <= now:
                self._recalled.pop(key, None)

    def remember(self, incoming: Incoming, *, timestamp: float | None = None) -> tuple[ImageReference, ...]:
        if not self.config.enabled or incoming.self_id != self.self_id:
            return ()
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
        result = []
        images = [part for part in incoming.segments if part["type"] == "image"]
        for index, part in enumerate(images[:self.per_message]):
            key = (address, incoming.message_id, index)
            previous = self._keys.get(key)
            if previous in self._items:
                result.append(self._items[previous])
                continue
            data = part["data"]
            file_id = image_file_id(data.get("file"))
            url = data.get("url")
            url = url if isinstance(url, str) and 0 < len(url) <= 8192 else None
            if file_id is None and url is None:
                continue
            identifier = "qqimg_" + secrets.token_urlsafe(18)
            item = ImageReference(identifier, self.self_id, address, incoming.message_id,
                                  incoming.user_id, index, file_id, url, stamp,
                                  stamp + self.config.ttl_seconds)
            self._items[identifier] = item
            self._keys[key] = identifier
            result.append(item)
            while len(self._items) > self.config.max_entries:
                self._drop(next(iter(self._items)))
        return tuple(result)

    def get(self, identifier: str, target: Target) -> ImageReference:
        self._prune()
        item = self._items.get(identifier) if isinstance(identifier, str) else None
        if (not self.config.enabled or item is None or item.self_id != self.self_id
                or item.address != target.address
                or self.is_recalled(item.address, item.message_id)):
            raise MediaError("image reference is unavailable, expired, recalled or belongs to another chat")
        return item

    def set_downloaded(self, identifier: str, target: Target, downloaded: Downloaded) -> None:
        item = self.get(identifier, target)  # Recheck after an awaited transfer.
        self._items[identifier] = replace(item, downloaded=downloaded)

    def for_message(self, target: Target, message_id: str) -> tuple[ImageReference, ...]:
        self._prune()
        return tuple(item for item in self._items.values()
                     if item.address == target.address and item.message_id == message_id
                     and not self.is_recalled(item.address, item.message_id))

    def recent(self, incoming: Incoming) -> tuple[ImageReference, ...]:
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
        items.sort(key=lambda item: (item.timestamp, item.image_index), reverse=True)
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
        self._recalled.clear()
        self._history_floor = 0.0


class ReferencedMedia(str):
    """A string on the wire, with a local capability checked again at the send gate."""

    def __new__(cls, value: str, media_id: str, requester_id: str):
        result = super().__new__(cls, value)
        result.media_id = media_id
        result.requester_id = requester_id
        return result
