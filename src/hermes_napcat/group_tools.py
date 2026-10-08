"""Conversation history tools bound to the current session/profile/target boundary."""
from __future__ import annotations

from typing import Any

from .history import expand_forward, read_private_history
from .protocol import message_id
from .tools import READ_TOOLSET, _current_anchor, _message_summary, _on_gateway, _tool_handler
from .tools import register_tools as register_base_tools


@_tool_handler
async def qq_get_recent_messages(args: dict[str, Any], **kwargs) -> dict[str, Any]:
    async def operation(adapter, target, session):
        if not adapter.policy.authorized_user(session.user_id):
            raise PermissionError("current user is not authorized to query conversation history")
        if target.kind == "private":
            page = await read_private_history(
                adapter, target, limit=args.get("limit", adapter.settings.qq_tools.history_limit),
                before=args.get("before_message_id"),
                current_message_id=_current_anchor(session, target))
            rows = []
            for data in page.messages:
                identifier = message_id(data["message_id"])
                if adapter.media_refs.is_recalled(target.address, identifier):
                    continue
                row = _message_summary(data, target, identifier)
                row["timestamp"] = data["time"]
                refs = adapter.remember_verified_media(target, identifier, data)
                if refs:
                    row["media_refs"] = [ref.summary() for ref in refs]
                rows.append(row)
            return {"success": True, "target": target.address, "messages": rows,
                    "truncated": page.truncated, "filtered": page.filtered,
                    "window_seconds": page.window_seconds, "history_status": "fetched"}
        groups = getattr(adapter, "groups", None)
        if groups is None:
            raise ValueError("the live adapter does not support group context")
        limit = args.get("limit", min(50, adapter.settings.group_context.history_limit))
        return await groups.recent_messages(target, limit=limit, before=args.get("before_message_id"))

    return await _on_gateway(
        "qq_get_recent_messages", args, operation,
        session_id=str(kwargs.get("session_id") or ""))


@_tool_handler
async def qq_get_forward(args: dict[str, Any], **kwargs) -> dict[str, Any]:
    async def operation(adapter, target, session):
        identifier = message_id(args.get("message_id"))
        expansion = await expand_forward(
            adapter, target, identifier, current_message_id=_current_anchor(session, target))
        rows = []
        media_count = 0
        refs_truncated = False
        for node in expansion.nodes:
            if adapter.media_refs.is_recalled(target.address, identifier):
                raise PermissionError("forward parent was recalled during the read")
            row = _message_summary(node.message, target, identifier)
            row["node_path"] = list(node.path)
            row["sender_attribution"] = "claimed_forward_author"
            remaining = max(0, adapter.settings.media.max_attachments - media_count)
            kept = []
            for part in node.message["message"]:
                if part["type"] in ("image", "record", "video", "file"):
                    if remaining <= 0:
                        refs_truncated = True
                        continue
                    remaining -= 1
                kept.append(part)
            refs = adapter.remember_forward_media(
                target, identifier, expansion.parent, node.path, {**node.message, "message": kept})
            media_count += len(refs)
            if refs:
                row["media_refs"] = [ref.summary() for ref in refs]
            rows.append(row)
        return {"success": True, "target": target.address, "message_id": identifier,
                "nodes": rows, "truncated": expansion.truncated,
                "unavailable": expansion.unavailable,
                "media_refs_truncated": refs_truncated,
                "visibility": "verified_parent_content"}

    return await _on_gateway(
        "qq_get_forward", args, operation, session_id=str(kwargs.get("session_id") or ""))


def register_tools(ctx) -> None:
    register_base_tools(ctx)
    ctx.register_tool(
        name="qq_get_recent_messages", toolset=READ_TOOLSET, handler=qq_get_recent_messages,
        is_async=True, emoji="QQ",
        description="Read bounded, attributed recent messages from the authorized QQ conversation.",
        schema={
            "name": "qq_get_recent_messages",
            "description": (
                "Read recent private or group context without attachment URLs. Defaults to this QQ chat. "
                "An optional before_message_id must belong to this conversation; group anchors use retained context. "
                "Cross-chat use requires the existing administrator opt-in and target ACL."
            ),
            "parameters": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "target": {"type": "string", "description": "Optional private:<QQ> or group:<group-id>."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "before_message_id": {"type": "string"},
                },
            },
        },
    )
    ctx.register_tool(
        name="qq_get_forward", toolset=READ_TOOLSET, handler=qq_get_forward,
        is_async=True, emoji="QQ",
        description="Read bounded forward content from a verified message in the authorized QQ conversation.",
        schema={
            "name": "qq_get_forward",
            "description": (
                "Expand a forwarded conversation embedded in a same-chat verified message. "
                "Returns bounded attributed text and controlled media references, without attachment URLs. "
                "Forward node authors are claimed attribution; access follows the visible parent message."
            ),
            "parameters": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "target": {"type": "string", "description": "Optional private:<QQ> or group:<group-id>."},
                    "message_id": {"type": "string"},
                },
                "required": ["message_id"],
            },
        },
    )
