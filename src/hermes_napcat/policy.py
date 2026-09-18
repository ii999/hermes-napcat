"""Local admission checks. The Hermes gateway still performs its own authorization."""
from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Hashable

from .config import Settings
from .protocol import Incoming, Target


class RecentIDs:
    def __init__(self, capacity: int, ttl: float):
        self.capacity = capacity
        self.ttl = ttl
        self._items: OrderedDict[Hashable, float] = OrderedDict()

    def contains(self, key: Hashable) -> bool:
        now = time.monotonic()
        while self._items and next(iter(self._items.values())) <= now:
            self._items.popitem(last=False)
        return key in self._items

    def add(self, key: Hashable) -> None:
        self._items.pop(key, None)
        self._items[key] = time.monotonic() + self.ttl
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)

    def discard(self, key: Hashable) -> None:
        self._items.pop(key, None)


class Policy:
    def __init__(self, settings: Settings):
        self.config = settings
        self.seen = RecentIDs(settings.dedup_capacity, settings.dedup_ttl)
        self.own = RecentIDs(settings.dedup_capacity, 86400)
        self._rates: OrderedDict[Hashable, deque[float]] = OrderedDict()

    def authorized_user(self, user: str) -> bool:
        return self.config.allow_all_users or user in self.config.allowed_users

    def can_receive(self, incoming: Incoming) -> bool:
        if incoming.self_id != self.config.self_id or incoming.user_id == incoming.self_id:
            return False
        if not self.authorized_user(incoming.user_id):
            return False
        return incoming.target.kind == "private" or incoming.target.id in self.config.allowed_groups

    def can_send(self, target: Target) -> bool:
        if target.kind == "private":
            return self.authorized_user(target.id)
        return target.id in self.config.allowed_groups

    def trigger(self, incoming: Incoming, verified_reply: bool = False) -> str | None:
        if incoming.target.kind == "private":
            return incoming.text
        text = incoming.text
        for prefix in self.config.group_prefixes:
            if text == prefix or (text.startswith(prefix) and text[len(prefix):len(prefix)+1].isspace()):
                return text[len(prefix):].lstrip()
        if self.config.group_mention and incoming.mentioned:
            return text
        if self.config.group_reply_to_bot and verified_reply:
            return text
        return None

    def rate_allowed(self, incoming: Incoming) -> bool:
        key = (incoming.target.address, incoming.user_id)
        now = time.monotonic()
        history = self._rates.pop(key, deque())
        while history and history[0] <= now - 60:
            history.popleft()
        allowed = len(history) < self.config.messages_per_minute
        if allowed:
            history.append(now)
        self._rates[key] = history
        while len(self._rates) > self.config.dedup_capacity:
            self._rates.popitem(last=False)
        return allowed
