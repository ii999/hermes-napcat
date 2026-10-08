"""Bounded history and forward reads with conversation provenance, independent of Hermes."""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from typing import Any

from .config import numeric_id
from .protocol import Target, message_id, segments
from .transport import OneBotError

_FORWARD_ID = re.compile(r"[A-Za-z0-9_{}().+=\-]{1,256}\Z")


@dataclass(frozen=True)
class HistoryPage:
    messages: tuple[dict[str, Any], ...]
    truncated: bool
    filtered: int
    window_seconds: int


@dataclass(frozen=True)
class ForwardNode:
    path: tuple[int, ...]
    message: dict[str, Any]


@dataclass(frozen=True)
class ForwardExpansion:
    parent: dict[str, Any]
    nodes: tuple[ForwardNode, ...]
    truncated: bool
    unavailable: int


def _author(data: dict[str, Any]) -> str:
    sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    try:
        author = numeric_id(data.get("user_id") or sender.get("user_id"))
    except ValueError:
        return ""
    if sender.get("user_id") is not None and str(sender["user_id"]) != author:
        return ""
    return author


def _belongs(adapter: Any, target: Target, identifier: str, data: dict[str, Any]) -> bool:
    if data.get("message_type") != target.kind:
        return False
    if data.get("self_id") is not None and str(data["self_id"]) != adapter.settings.self_id:
        return False
    if data.get("message_id") is not None:
        try:
            if message_id(data["message_id"]) != identifier:
                return False
        except ValueError:
            return False
    author = _author(data)
    if not author:
        return False
    if target.kind == "group":
        return str(data.get("group_id")) == target.id
    destination = str(data.get("target_id") or "")
    if author == target.id:
        return destination in ("", target.id, adapter.settings.self_id)
    return author == adapter.settings.self_id and (
        destination == target.id or (not destination and
                                    adapter.policy.own.contains((target.address, identifier))))


def _visible_author(adapter: Any, target: Target, author: str) -> bool:
    return (author == adapter.settings.self_id or adapter.policy.authorized_user(author)
            or (target.kind == "group" and adapter.settings.group_context.enabled
                and adapter.settings.group_context.observe_all_members))


def _timestamp(data: dict[str, Any], window: int) -> float | None:
    stamp = data.get("time")
    now = time.time()
    if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
            or not math.isfinite(stamp) or not now - window <= stamp <= now + 300):
        return None
    return min(float(stamp), now)


def _recalled(adapter: Any, target: Target, identifier: str) -> bool:
    return adapter.media_refs.is_recalled(target.address, identifier)


def _reject_notice_anchor(adapter: Any, target: Target, identifier: str) -> None:
    notices = getattr(adapter, "_file_notices", None)
    if notices is not None and notices.is_notice_message(target, identifier):
        raise ValueError("file notices cannot be used as message history anchors")


async def read_private_history(
    adapter: Any, target: Target, *, limit: int, before: str | None = None,
    current_message_id: str | None = None,
) -> HistoryPage:
    """Fetch one bounded page; response rows independently prove contact/account ownership."""
    config = adapter.settings.qq_tools
    if target.kind != "private" or not adapter.policy.can_send(target):
        raise PermissionError("private history target is not authorized")
    if type(limit) is not int or not 1 <= limit <= config.history_limit:
        raise ValueError("limit exceeds qq_tools.history_limit")
    params: dict[str, Any] = {
        "user_id": target.id, "count": limit, "reverse_order": False,
        "disable_get_url": True, "parse_mult_msg": False,
    }
    if before is not None:
        before = message_id(before)
        _reject_notice_anchor(adapter, target, before)
        anchor = await adapter.verified_message(
            target, before, current_message_id=current_message_id)
        if (not _belongs(adapter, target, before, anchor)
                or not _visible_author(adapter, target, _author(anchor))
                or _timestamp(anchor, config.history_window_seconds) is None
                or _recalled(adapter, target, before)):
            raise PermissionError("history anchor is unavailable or belongs to another conversation")
        params["message_seq"] = before
    data = await adapter.transport.call("get_friend_msg_history", params)
    if not adapter.policy.can_send(target):
        raise PermissionError("private history target is no longer authorized")
    if before is not None and _recalled(adapter, target, before):
        raise PermissionError("history anchor was recalled during the read")
    rows = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("private history response has no message list")
    accepted, seen, filtered = [], set(), 0
    for row in rows[:limit]:
        if not isinstance(row, dict):
            filtered += 1
            continue
        try:
            identifier = message_id(row.get("message_id"))
            segments(row.get("message"))
        except (ValueError, TypeError):
            filtered += 1
            continue
        if (identifier in seen or not _belongs(adapter, target, identifier, row)
                or not _visible_author(adapter, target, _author(row))
                or _timestamp(row, config.history_window_seconds) is None
                or _recalled(adapter, target, identifier)):
            filtered += 1
            continue
        seen.add(identifier)
        accepted.append(row)
    accepted.sort(key=lambda row: float(row["time"]))
    return HistoryPage(tuple(accepted), len(rows) >= limit, filtered,
                       config.history_window_seconds)


def _node_message(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    if row.get("type") == "node":
        row = row.get("data")
        if not isinstance(row, dict):
            return None
    parts = row.get("message")
    if not isinstance(parts, list):
        parts = row.get("content")
    if not isinstance(parts, list):
        return None
    sender = row.get("sender") if isinstance(row.get("sender"), dict) else {}
    # Node authors are claimed attribution inside the verified parent's content, not principals.
    user = str(row.get("user_id") or sender.get("user_id") or "")[:20]
    name = str(row.get("nickname") or sender.get("card") or sender.get("nickname") or "")[:100]
    return {"message": parts, "user_id": user, "sender": {"user_id": user, "nickname": name},
            "time": row.get("time")}


async def expand_forward(
    adapter: Any, target: Target, identifier: str, *, current_message_id: str | None = None,
) -> ForwardExpansion:
    """Expand only IDs embedded in a verified message, with one shared traversal budget."""
    identifier = message_id(identifier)
    _reject_notice_anchor(adapter, target, identifier)
    parent = await adapter.verified_message(
        target, identifier, current_message_id=current_message_id)
    config = adapter.settings.qq_tools
    window = config.history_window_seconds
    if adapter.settings.media.references.enabled:
        window = min(window, adapter.settings.media.references.ttl_seconds)

    def check_parent() -> None:
        if (not adapter.policy.can_send(target) or not _belongs(adapter, target, identifier, parent)
                or not _visible_author(adapter, target, _author(parent))
                or _timestamp(parent, window) is None or _recalled(adapter, target, identifier)):
            raise PermissionError("forward parent is unavailable, expired, recalled or not visible")

    check_parent()
    parent_parts = segments(parent.get("message"))
    roots = [(index, part["data"]) for index, part in enumerate(parent_parts)
             if part["type"] == "forward"]
    if not roots:
        raise ValueError("verified message has no forward content")
    nodes: list[ForwardNode] = []
    visited: set[str] = set()
    examined = actions = text_bytes = unavailable = 0
    truncated = False

    async def walk(data: dict[str, Any], path: tuple[int, ...], depth: int) -> None:
        nonlocal examined, actions, text_bytes, unavailable, truncated
        check_parent()
        if depth > config.max_forward_depth or examined >= config.max_forward_nodes:
            truncated = True
            return
        rows = data.get("content")
        if not isinstance(rows, list):
            rows = data.get("message")
        if not isinstance(rows, list):
            source = data.get("id")
            if not isinstance(source, str) or not _FORWARD_ID.fullmatch(source):
                unavailable += 1
                return
            if source in visited or actions >= config.max_forward_nodes:
                truncated = True
                return
            visited.add(source)
            actions += 1
            try:
                response = await adapter.transport.call("get_forward_msg", {"id": source})
            except OneBotError:
                check_parent()
                unavailable += 1
                return
            check_parent()
            rows = response.get("messages") if isinstance(response, dict) else None
            if not isinstance(rows, list):
                unavailable += 1
                return
        remaining = config.max_forward_nodes - examined
        if len(rows) > remaining:
            truncated = True
        for index, row in enumerate(rows[:remaining]):
            if examined >= config.max_forward_nodes:
                truncated = True
                break
            examined += 1
            node = _node_message(row)
            if node is None:
                unavailable += 1
                continue
            try:
                parts = segments(node["message"])
            except (ValueError, TypeError):
                unavailable += 1
                continue
            kept = []
            child_path = (*path, index)
            for segment_index, part in enumerate(parts):
                if part["type"] in ("forward", "node"):
                    child = part["data"] if part["type"] == "forward" else {"content": [part]}
                    await walk(child, (*child_path, segment_index), depth + 1)
                    continue
                if part["type"] == "text":
                    text = part["data"].get("text")
                    if not isinstance(text, str):
                        unavailable += 1
                        continue
                    encoded = text.encode("utf-8")
                    room = max(0, config.max_forward_chars - text_bytes)
                    if len(encoded) > room:
                        text = encoded[:room].decode("utf-8", errors="ignore")
                        truncated = True
                    text_bytes += len(text.encode("utf-8"))
                    part = {"type": "text", "data": {"text": text}}
                kept.append(part)
            node["message"] = kept
            nodes.append(ForwardNode(child_path, node))

    for index, data in roots:
        await walk(data, (index,), 1)
    check_parent()
    nodes.sort(key=lambda node: node.path)
    return ForwardExpansion(parent, tuple(nodes), truncated, unavailable)
