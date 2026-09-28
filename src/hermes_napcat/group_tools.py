"""Group context read tool; reuses the existing current-session/profile/target boundary."""
from __future__ import annotations

from typing import Any

from .tools import TOOLSET, _on_gateway, _tool_handler
from .tools import register_tools as register_base_tools


@_tool_handler
async def qq_get_recent_messages(args: dict[str, Any], **kwargs) -> dict[str, Any]:
    async def operation(adapter, target, session):
        if not adapter.policy.authorized_user(session.user_id):
            raise PermissionError("current user is not authorized to query group context")
        groups = getattr(adapter, "groups", None)
        if groups is None:
            raise ValueError("the live adapter does not support group context")
        limit = args.get("limit", min(50, adapter.settings.group_context.history_limit))
        return await groups.recent_messages(target, limit=limit, before=args.get("before_message_id"))

    return await _on_gateway(
        "qq_get_recent_messages", args, operation,
        session_id=str(kwargs.get("session_id") or ""))


def register_tools(ctx) -> None:
    register_base_tools(ctx)
    ctx.register_tool(
        name="qq_get_recent_messages", toolset=TOOLSET, handler=qq_get_recent_messages,
        is_async=True, emoji="QQ",
        description="Read bounded, attributed recent messages from the authorized QQ group.",
        schema={
            "name": "qq_get_recent_messages",
            "description": (
                "Read recent group context without attachment URLs. Defaults to this QQ group. "
                "An optional before_message_id must come from this group's retained context. "
                "Cross-chat use requires the existing administrator opt-in and target ACL."
            ),
            "parameters": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "target": {"type": "string", "description": "Optional group:<group-id>."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    "before_message_id": {"type": "string"},
                },
            },
        },
    )
