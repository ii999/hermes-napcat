"""OneBot v11 message codec, independent of Hermes and the socket implementation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import numeric_id


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class Target:
    kind: str
    id: str

    @classmethod
    def parse(cls, address: str) -> Target:
        if not isinstance(address, str):
            raise ProtocolError("target must be a string")
        kind, sep, identifier = address.partition(":")
        if not sep or kind not in ("private", "group"):
            raise ProtocolError("target must be private:<QQ> or group:<group-id>")
        try:
            return cls(kind, numeric_id(identifier))
        except ValueError as exc:
            raise ProtocolError(str(exc)) from exc

    @property
    def address(self) -> str:
        return f"{self.kind}:{self.id}"

    @property
    def params(self) -> dict[str, int]:
        return {"group_id" if self.kind == "group" else "user_id": int(self.id)}

    @property
    def action(self) -> str:
        return f"send_{self.kind}_msg"


def message_id(value: Any) -> str:
    # OneBot message IDs may be negative; unlike QQ IDs they aren't positive account numbers.
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ProtocolError("invalid message ID")
    text = str(value)
    number = text.removeprefix("-")
    if not number or not number.isascii() or not number.isdecimal() or len(number) > 32:
        raise ProtocolError("invalid message ID")
    return str(int(text))


def segments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ProtocolError("OneBot message must be an array; set messagePostFormat=array")
    if len(value) > 256:
        raise ProtocolError("too many message segments")
    result = []
    for segment in value:
        if (not isinstance(segment, dict) or not isinstance(segment.get("type"), str) or
                not isinstance(segment.get("data"), dict)):
            raise ProtocolError("invalid OneBot segment")
        result.append({"type": segment["type"], "data": dict(segment["data"])})
    return result


@dataclass(frozen=True)
class Incoming:
    target: Target
    user_id: str
    self_id: str
    message_id: str
    user_name: str
    text: str
    segments: list[dict[str, Any]]
    mentioned: bool
    reply_to: str | None
    raw: dict[str, Any]

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Incoming | None:
        if raw.get("post_type") != "message":
            return None
        kind = raw.get("message_type")
        if kind not in ("private", "group"):
            return None
        try:
            user = numeric_id(raw.get("user_id"))
            self_id = numeric_id(raw.get("self_id"))
            target = Target(kind, numeric_id(raw.get("group_id") if kind == "group" else user))
        except ValueError as exc:
            raise ProtocolError(str(exc)) from exc
        parts = segments(raw.get("message"))
        text: list[str] = []
        mentioned = False
        reply = None
        for part in parts:
            data = part["data"]
            if part["type"] == "text":
                value = data.get("text", "")
                if not isinstance(value, str):
                    raise ProtocolError("text segment requires a string")
                text.append(value)
            elif part["type"] == "at":
                qq = str(data.get("qq", ""))
                if qq == self_id:
                    mentioned = True
                else:
                    text.append(f"@{qq}")
            elif part["type"] == "reply":
                reply = message_id(data.get("id"))
            elif part["type"] == "face":
                text.append(f"[QQ表情:{str(data.get('id', ''))[:20]}]")
            elif part["type"] in ("image", "record", "video", "file"):
                text.append(f"[{part['type']}]")
            else:
                text.append(f"[不支持的消息类型:{part['type'][:40]}]")
        sender = raw.get("sender") if isinstance(raw.get("sender"), dict) else {}
        name = str(sender.get("card") or sender.get("nickname") or user)[:100]
        return cls(target, user, self_id, message_id(raw.get("message_id")), name,
                   "".join(text).strip(), parts, mentioned, reply, raw)


def text_segments(text: str, reply_to: str | None = None) -> list[dict[str, Any]]:
    result = []
    if reply_to is not None:
        result.append({"type": "reply", "data": {"id": message_id(reply_to)}})
    # Deliberately don't interpret CQ codes, mentions or commands in model output.
    result.append({"type": "text", "data": {"text": text}})
    return result


def split_text(text: str, limit: int) -> list[str]:
    if limit <= 0:
        raise ValueError("limit must be positive")
    result = []
    while len(text) > limit:
        split = text.rfind("\n", 0, limit)
        end = split + 1 if split >= limit // 2 else limit
        result.append(text[:end])
        text = text[end:]
    if text:
        result.append(text)
    return result
