"""Bounded, attributed QQ group observations. No Hermes imports or raw-media storage."""
from __future__ import annotations

import json
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from .config import GroupContextSettings, Settings, numeric_id
from .policy import Policy, RecentIDs
from .protocol import Incoming, Target, message_id


def _timestamp(raw: Any, fallback: float | None) -> tuple[float, bool]:
    if not isinstance(raw, bool) and isinstance(raw, (int, float)):
        value = float(raw)
        if math.isfinite(value) and 0 < value <= time.time() + 300:
            return value, False
    if fallback is None:
        raise ValueError("history message has no valid timestamp")
    return fallback, True


@dataclass(frozen=True)
class GroupMessage:
    message_id: str
    user_id: str
    name: str
    timestamp: float
    text: str
    mentions: tuple[str, ...] = ()
    reply_to: str | None = None
    role: str = "unknown"
    attachments: tuple[str, ...] = ()
    time_inferred: bool = False
    truncated: bool = False
    own: bool = False

    @classmethod
    def from_incoming(cls, incoming: Incoming, limit: int, *, history: bool = False):
        stamp, inferred = _timestamp(incoming.raw.get("time"), None if history else time.time())
        sender = incoming.raw.get("sender")
        sender = sender if isinstance(sender, dict) else {}
        if sender.get("user_id") is not None and numeric_id(sender["user_id"]) != incoming.user_id:
            raise ValueError("conflicting message author")
        mentions: list[str] = []
        attachments: list[str] = []
        for segment in incoming.segments:
            if segment["type"] == "at":
                target = str(segment["data"].get("qq", ""))
                if target == "all":
                    mentions.append(target)
                else:
                    try:
                        mentions.append(numeric_id(target))
                    except ValueError:
                        continue
            elif segment["type"] in ("image", "record", "video", "file", "forward"):
                attachments.append(segment["type"])
        role = sender.get("role")
        return cls(
            incoming.message_id, incoming.user_id, incoming.user_name[:100], stamp,
            incoming.text[:limit], tuple(mentions[:32]), incoming.reply_to,
            role if role in ("owner", "admin", "member") else "unknown",
            tuple(attachments[:16]), inferred, len(incoming.text) > limit,
            incoming.user_id == incoming.self_id,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender": {"user_id": self.user_id, "name": self.name, "group_role": self.role},
            "time": datetime.fromtimestamp(self.timestamp, timezone.utc).isoformat(),
            "time_inferred": self.time_inferred, "text": self.text,
            "mentions": list(self.mentions), "reply_to": self.reply_to,
            "attachments": list(self.attachments), "text_truncated": self.truncated,
            "bot": self.own,
        }


@dataclass
class RoomContext:
    messages: OrderedDict[str, GroupMessage] = field(default_factory=OrderedDict)
    revision: int = 0
    history_status: str = "live_only"
    gap: bool = False


class GroupContext:
    """Each instance belongs to one adapter/profile/account; keys are typed chat addresses."""

    def __init__(self, settings: Settings, policy: Policy):
        self.settings = settings
        self.config: GroupContextSettings = settings.group_context
        self.policy = policy
        self.rooms: OrderedDict[str, RoomContext] = OrderedDict()
        self.recalled = RecentIDs(settings.dedup_capacity, self.config.history_window_seconds)
        self._revision = 0

    def can_observe(self, incoming: Incoming) -> bool:
        return (
            self.config.enabled and incoming.self_id == self.settings.self_id
            and incoming.target.kind == "group"
            and incoming.target.id in self.settings.allowed_groups
            and (incoming.user_id == self.settings.self_id
                 or self.config.observe_all_members
                 or self.policy.authorized_user(incoming.user_id))
        )

    def room(self, address: str) -> RoomContext:
        room = self.rooms.pop(address, None) or RoomContext()
        self.rooms[address] = room
        while len(self.rooms) > self.config.max_groups:
            self.rooms.popitem(last=False)
        cutoff = time.time() - self.config.history_window_seconds
        room.messages = OrderedDict((key, msg) for key, msg in room.messages.items()
                                    if msg.timestamp >= cutoff)
        return room

    def put(self, incoming: Incoming, *, history: bool = False) -> bool:
        if not self.can_observe(incoming):
            return False
        key = (incoming.target.address, incoming.message_id)
        if self.recalled.contains(key):
            return False
        record = GroupMessage.from_incoming(incoming, self.config.max_message_chars, history=history)
        if record.timestamp < time.time() - self.config.history_window_seconds:
            return False
        room = self.room(incoming.target.address)
        if record.message_id in room.messages:
            return False  # Backfill and echoes never overwrite a live observation.
        room.messages[record.message_id] = record
        # Retain newest event-time records, not the last records ingested by a backfill.
        ordered = sorted(room.messages.values(), key=lambda item: item.timestamp)
        room.messages = OrderedDict((item.message_id, item)
                                    for item in ordered[-self.config.live_buffer_messages:])
        if not history:
            self._revision += 1
            room.revision = self._revision
        return True

    def mark_activity(self, address: str) -> None:
        self._revision += 1
        self.room(address).revision = self._revision

    def recall(self, address: str, identifier: str) -> None:
        identifier = message_id(identifier)
        self.recalled.add((address, identifier))
        room = self.room(address)
        room.messages.pop(identifier, None)
        self._revision += 1
        room.revision = self._revision

    def is_recalled(self, address: str, identifier: str) -> bool:
        return self.recalled.contains((address, identifier))

    def lookup(self, address: str, identifier: str | None) -> GroupMessage | None:
        if identifier is None or self.is_recalled(address, identifier):
            return None
        return self.room(address).messages.get(identifier)

    def parse_history(self, target: Target, raw: Any) -> Incoming | None:
        # Never inject a request's group ID into an unproven returned row.
        if not isinstance(raw, dict) or str(raw.get("group_id")) != target.id:
            return None
        if raw.get("message_type") != "group":
            return None
        if raw.get("self_id") is not None and str(raw["self_id"]) != self.settings.self_id:
            return None
        sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
        event = {**raw, "post_type": "message", "self_id": self.settings.self_id,
                 "user_id": raw.get("user_id", sender.get("user_id"))}
        try:
            incoming = Incoming.parse(event)
            if incoming is None or not self.can_observe(incoming):
                return None
            GroupMessage.from_incoming(incoming, self.config.max_message_chars, history=True)
            return incoming
        except (ValueError, TypeError, OverflowError):
            return None

    def records(self, address: str, *, before: str | None = None,
                limit: int | None = None) -> list[GroupMessage]:
        rows = list(self.room(address).messages.values())
        if before is not None:
            index = next((i for i, item in enumerate(rows) if item.message_id == before), None)
            if index is None:
                raise ValueError("anchor is outside the retained context window")
            rows = rows[:index]
        return rows[-(limit or self.config.history_limit):]

    def render(self, address: str, *, current: Incoming | None = None,
               quote: GroupMessage | None = None, before: str | None = None,
               limit: int | None = None) -> tuple[str, list[dict[str, Any]], bool]:
        room = self.room(address)
        if current is not None:
            stamp, _ = _timestamp(current.raw.get("time"), time.time())
            rows = [m for m in room.messages.values()
                    if m.message_id != current.message_id and m.timestamp <= stamp]
        else:
            rows = self.records(address, before=before, limit=self.config.live_buffer_messages)
        if self.config.stop_at_last_bot_message and current is not None:
            start = next((i for i in range(len(rows) - 1, -1, -1) if rows[i].own), 0)
            rows = rows[start:]
        count = min(limit or self.config.history_limit, self.config.history_limit)
        truncated = len(rows) > count
        rows = rows[-count:]
        if quote is not None and not self.is_recalled(address, quote.message_id):
            rows = [item for item in rows if item.message_id != quote.message_id]
            rows = rows[-max(0, count - 1):] if count > 1 else []
            rows.insert(0, quote)
        budget = self.config.max_context_chars
        header = {
            "kind": "untrusted_qq_group_context", "target": address,
            "history_status": room.history_status, "gap_detected": room.gap,
            "current_message_id": current.message_id if current else None,
            "quoted_message_id": quote.message_id if quote else None,
        }
        # Select quote first, then newest rows, while bounding the serialized text too.
        priority = ([quote] if quote is not None and quote in rows else [])
        priority.extend(item for item in reversed(rows) if item != quote)
        selected: dict[str, dict[str, Any]] = {}
        used = len(json.dumps(header, ensure_ascii=False)) + 100
        for row in priority:
            data = row.as_dict()
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            remaining = budget - used - 1
            if len(encoded) > remaining:
                if not selected and remaining > 600:
                    # JSON escaping can expand control characters; shrink until it fits.
                    text = row.text
                    while text and len(encoded) > remaining:
                        text = text[:len(text) // 2]
                        data = replace(row, text=text, truncated=True).as_dict()
                        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                if len(encoded) > remaining:
                    truncated = True
                    continue
            selected[row.message_id] = data
            used += len(encoded) + 1
        truncated = truncated or len(selected) < len(rows) or any(d["text_truncated"] for d in selected.values())
        header["truncated"] = truncated
        result = [selected[row.message_id] for row in rows if row.message_id in selected]
        text = "\n".join([json.dumps(header, ensure_ascii=False),
                          *(json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in result)])
        return text, result, truncated

    def clear(self) -> None:
        self.rooms.clear()
