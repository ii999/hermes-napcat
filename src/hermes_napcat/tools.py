"""Model-callable QQ tools bound to the current NapCat gateway session."""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import numeric_id
from .media import MediaError
from .protocol import ProtocolError, Target, message_id, segments
from .transport import ActionError, DeliveryUncertain, NotConnected, OneBotError

log = logging.getLogger(__name__)
TOOLSET = "napcat_qq"


class ToolRequestError(ValueError):
    pass


@dataclass(frozen=True)
class ToolSession:
    target: Target
    user_id: str
    profile: str
    session_id: str
    current_message_id: str | None


def _current_session(session_id: str = "") -> ToolSession:
    from gateway.session_context import get_session_env

    platform = str(get_session_env("HERMES_SESSION_PLATFORM", "") or "").strip().lower()
    if platform != "napcat":
        raise ToolRequestError("QQ tools require a live NapCat chat session")
    try:
        target = Target.parse(str(get_session_env("HERMES_SESSION_CHAT_ID", "") or ""))
        user_id = numeric_id(get_session_env("HERMES_SESSION_USER_ID", ""))
    except (ProtocolError, ValueError) as exc:
        raise ToolRequestError("current QQ session identity is unavailable") from exc
    current = str(get_session_env("HERMES_SESSION_MESSAGE_ID", "") or "").strip()
    if current:
        try:
            current = message_id(current)
        except ProtocolError:
            current = ""
    return ToolSession(
        target=target,
        user_id=user_id,
        profile=str(get_session_env("HERMES_SESSION_PROFILE", "") or "").strip(),
        session_id=str(session_id or get_session_env("HERMES_SESSION_ID", "") or ""),
        current_message_id=current or None,
    )


def _live_adapter(profile: str):
    try:
        from gateway.config import Platform
        from gateway.run import _gateway_runner_ref

        runner = _gateway_runner_ref()
    except Exception as exc:
        raise ToolRequestError("Hermes gateway is not available") from exc
    if runner is None:
        raise ToolRequestError("Hermes gateway is not running")
    platform = Platform("napcat")
    resolver = getattr(runner, "_authorization_adapter", None)
    if callable(resolver):
        adapter = resolver(platform, profile or None)
    else:
        adapter = (getattr(runner, "adapters", None) or {}).get(platform)
    if adapter is None:
        raise ToolRequestError("the current profile has no live NapCat adapter")
    return runner, adapter


def _authorized_target(adapter, session: ToolSession, raw: Any) -> Target:
    if raw in (None, ""):
        target = session.target
    else:
        if not isinstance(raw, str):
            raise ToolRequestError("target must be private:<QQ> or group:<group-id>")
        target = Target.parse(raw.strip())
    if target != session.target:
        if not adapter.settings.qq_tools.allow_cross_chat:
            raise PermissionError("cross-chat QQ actions are disabled")
        if session.user_id not in adapter.settings.admins:
            raise PermissionError("only a configured QQ administrator may use cross-chat actions")
    if not adapter.policy.can_send(target):
        raise PermissionError("target is not allowlisted")
    return target


def _action_key(tool: str, session: ToolSession, args: dict[str, Any]) -> str:
    try:
        body = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        body = repr(args)
    identity = (
        f"{tool}\0{session.session_id}\0{session.target.address}\0"
        f"{session.current_message_id or ''}\0{body}"
    )
    return hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()


async def _on_gateway(tool: str, args: dict[str, Any], operation, *, session_id: str = ""):
    session = _current_session(session_id)
    runner, adapter = _live_adapter(session.profile)
    if not adapter.settings.qq_tools.enabled:
        raise PermissionError("model-callable QQ tools are disabled for this profile")
    key = _action_key(tool, session, args)

    async def execute():
        target = _authorized_target(adapter, session, args.get("target"))
        return await adapter.run_agent_action(
            key, lambda: operation(adapter, target, session))

    coro = execute()
    gateway_loop = getattr(runner, "_gateway_loop", None)
    current_loop = asyncio.get_running_loop()
    if gateway_loop is current_loop:
        return await coro
    if gateway_loop is None or not gateway_loop.is_running():
        coro.close()
        raise ToolRequestError("Hermes gateway loop is not running")
    try:
        future = asyncio.run_coroutine_threadsafe(coro, gateway_loop)
    except Exception as exc:
        coro.close()
        raise ToolRequestError("could not schedule the QQ action on the gateway") from exc
    # Once scheduled, cancellation must not turn into an automatic duplicate send.
    return await asyncio.shield(asyncio.wrap_future(future))


def _bounded_text(value: Any, name: str, limit: int, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ToolRequestError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise ToolRequestError(f"{name} must be text")
    value = value.strip() if required else value
    if required and not value:
        raise ToolRequestError(f"{name} is required")
    if len(value) > limit:
        raise ToolRequestError(f"{name} is too long")
    return value


def _safe_file_name(value: Any, source: str) -> str:
    name = value if value is not None else Path(urlsplit(source).path or source).name
    if (not isinstance(name, str) or not name or "/" in name or "\\" in name
            or name in (".", "..") or len(name) > 200):
        raise ToolRequestError("invalid attachment name")
    return name


async def _direct_parts(adapter, args: dict[str, Any]) -> list[dict[str, Any]]:
    raw = args.get("segments")
    text = args.get("text")
    images = args.get("images")
    if raw is not None and (text is not None or images is not None):
        raise ToolRequestError("use either segments or text/images, not both")
    if raw is None:
        raw = []
        if text is not None:
            raw.append({"type": "text", "text": text})
        if images is not None:
            if not isinstance(images, list):
                raise ToolRequestError("images must be a list")
            raw.extend({"type": "image", "source": source} for source in images)
    if not isinstance(raw, list) or not raw:
        raise ToolRequestError("message requires text, images, or segments")
    if len(raw) > adapter.settings.qq_tools.max_segments:
        raise ToolRequestError("message has too many segments")

    parts: list[dict[str, Any]] = []
    chars = 0
    media_count = 0
    for spec in raw:
        if not isinstance(spec, dict):
            raise ToolRequestError("each message segment must be an object")
        kind = spec.get("type")
        if kind == "text":
            value = _bounded_text(spec.get("text"), "segment text", adapter.settings.max_outbound_chars,
                                  required=True)
            chars += len(value)
            parts.append({"type": "text", "data": {"text": value}})
        elif kind == "image":
            source = _bounded_text(spec.get("source"), "image source", 8192, required=True)
            reference = await adapter.outbound_reference(source, kind="image")
            data = {"file": reference}
            summary = _bounded_text(spec.get("summary"), "image summary", 100)
            if summary:
                data["summary"] = summary
            parts.append({"type": "image", "data": data})
            media_count += 1
        elif kind == "at":
            if spec.get("qq") == "all":
                raise PermissionError("@all is not exposed to the model")
            parts.append({"type": "at", "data": {"qq": numeric_id(spec.get("qq"))}})
        elif kind == "face":
            face_id = str(spec.get("id", ""))
            if not face_id.isascii() or not face_id.isdecimal() or len(face_id) > 10:
                raise ToolRequestError("face id must be a decimal string")
            parts.append({"type": "face", "data": {"id": face_id}})
        else:
            raise ToolRequestError("direct message segments support text, image, at, and face")
    if chars > adapter.settings.max_outbound_chars:
        raise ToolRequestError("message exceeds total outbound length limit")
    if media_count > adapter.settings.qq_tools.max_media_items:
        raise ToolRequestError("message has too many media items")
    return parts


async def _forward_content(
    adapter, node: dict[str, Any], totals: dict[str, int],
) -> list[dict[str, Any]]:
    raw = node.get("segments")
    text = node.get("text")
    if raw is not None and text is not None:
        raise ToolRequestError("a forward node must use text or segments, not both")
    if raw is None:
        raw = [{"type": "text", "text": text}] if text is not None else []
    if not isinstance(raw, list) or not raw:
        raise ToolRequestError("a custom forward node requires text or segments")
    totals["segments"] += len(raw)
    if totals["segments"] > adapter.settings.qq_tools.max_segments:
        raise ToolRequestError("forward contains too many segments")

    result: list[dict[str, Any]] = []
    for spec in raw:
        if not isinstance(spec, dict):
            raise ToolRequestError("each forward segment must be an object")
        kind = spec.get("type")
        if kind == "text":
            value = _bounded_text(spec.get("text"), "forward text",
                                  adapter.settings.qq_tools.max_forward_chars, required=True)
            totals["chars"] += len(value)
            result.append({"type": "text", "data": {"text": value}})
            continue
        if kind == "face":
            face_id = str(spec.get("id", ""))
            if not face_id.isascii() or not face_id.isdecimal() or len(face_id) > 10:
                raise ToolRequestError("face id must be a decimal string")
            result.append({"type": "face", "data": {"id": face_id}})
            continue
        if kind not in ("image", "audio", "video", "file"):
            raise ToolRequestError(
                "forward segments support text, image, audio, video, file, and face")
        source = _bounded_text(spec.get("source"), f"{kind} source", 8192, required=True)
        media_kind = "record" if kind == "audio" else kind
        data = {"file": await adapter.outbound_reference(source, kind=media_kind)}
        if kind == "file":
            data["name"] = _safe_file_name(spec.get("name"), source)
        if kind == "image":
            summary = _bounded_text(spec.get("summary"), "image summary", 100)
            if summary:
                data["summary"] = summary
        if kind == "video" and spec.get("thumbnail") is not None:
            thumb = _bounded_text(spec.get("thumbnail"), "video thumbnail", 8192, required=True)
            data["thumb"] = await adapter.outbound_reference(thumb, kind="image")
        result.append({"type": media_kind, "data": data})
        totals["media"] += 1
    if totals["chars"] > adapter.settings.qq_tools.max_forward_chars:
        raise ToolRequestError("forward text exceeds configured limit")
    if totals["media"] > adapter.settings.qq_tools.max_media_items:
        raise ToolRequestError("forward contains too many media items")
    return result


def _current_anchor(session: ToolSession, target: Target) -> str | None:
    return session.current_message_id if target == session.target else None


def _public_failure(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, DeliveryUncertain):
        return {
            "success": False,
            "error": "QQ delivery outcome is unknown; inspect the conversation before retrying",
            "delivery_uncertain": True,
        }
    if isinstance(exc, NotConnected):
        detail = "NapCat is not connected"
    elif isinstance(exc, ActionError):
        detail = f"NapCat rejected the action (retcode={exc.retcode!r})"
    elif isinstance(exc, (ToolRequestError, PermissionError, ProtocolError, MediaError, ValueError)):
        detail = str(exc)
    elif isinstance(exc, OneBotError):
        detail = f"OneBot action failed ({type(exc).__name__})"
    else:
        log.exception("Unexpected QQ tool failure")
        detail = f"QQ tool failed ({type(exc).__name__})"
    return {"success": False, "error": detail[:500]}


def _tool_handler(function):
    @functools.wraps(function)
    async def wrapped(args: dict[str, Any], **kwargs) -> str:
        try:
            if not isinstance(args, dict):
                raise ToolRequestError("tool arguments must be an object")
            result = await function(args, **kwargs)
            return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return json.dumps(_public_failure(exc), ensure_ascii=False, separators=(",", ":"))

    return wrapped


@_tool_handler
async def qq_send_message(args: dict[str, Any], **kwargs) -> dict[str, Any]:
    async def operation(adapter, target, session):
        reply_to = args.get("reply_to")
        if reply_to is not None:
            reply_to = message_id(reply_to)
            await adapter.verified_message(
                target, reply_to, current_message_id=_current_anchor(session, target))
        parts = await _direct_parts(adapter, args)
        if reply_to is not None:
            parts.insert(0, {"type": "reply", "data": {"id": reply_to}})
        return await adapter.send_agent_parts(target, parts)

    return await _on_gateway(
        "qq_send_message", args, operation, session_id=str(kwargs.get("session_id") or ""))


@_tool_handler
async def qq_send_media(args: dict[str, Any], **kwargs) -> dict[str, Any]:
    async def operation(adapter, target, session):
        kind = args.get("media_type")
        if kind not in ("image", "audio", "video", "file"):
            raise ToolRequestError("media_type must be image, audio, video, or file")
        source = _bounded_text(args.get("source"), "source", 8192, required=True)
        reply_to = args.get("reply_to")
        if reply_to is not None:
            reply_to = message_id(reply_to)
            await adapter.verified_message(
                target, reply_to, current_message_id=_current_anchor(session, target))
        return await adapter.send_agent_media(
            target,
            kind,
            source,
            caption=_bounded_text(args.get("caption"), "caption", adapter.settings.message_chars),
            file_name=args.get("file_name"),
            thumbnail=args.get("thumbnail"),
            reply_to=reply_to,
        )

    return await _on_gateway(
        "qq_send_media", args, operation, session_id=str(kwargs.get("session_id") or ""))


@_tool_handler
async def qq_send_forward(args: dict[str, Any], **kwargs) -> dict[str, Any]:
    async def operation(adapter, target, session):
        raw_nodes = args.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise ToolRequestError("nodes must be a non-empty list")
        if len(raw_nodes) > adapter.settings.qq_tools.max_forward_nodes:
            raise ToolRequestError("forward contains too many nodes")

        prepared: list[dict[str, Any] | None] = []
        # Verify every reference before downloading custom-node media or sending anything.
        for node in raw_nodes:
            if not isinstance(node, dict):
                raise ToolRequestError("each forward node must be an object")
            existing = node.get("message_id")
            has_custom = node.get("text") is not None or node.get("segments") is not None
            if existing is not None:
                if has_custom:
                    raise ToolRequestError("a forward node cannot mix message_id with custom content")
                existing = message_id(existing)
                await adapter.verified_message(
                    target, existing, current_message_id=_current_anchor(session, target))
                prepared.append({"type": "node", "data": {"id": existing}})
            else:
                prepared.append(None)

        totals = {"chars": 0, "media": 0, "segments": 0}
        for index, node in enumerate(raw_nodes):
            if prepared[index] is not None:
                continue
            label = _bounded_text(node.get("label") or "Hermes", "node label", 64, required=True)
            content = await _forward_content(adapter, node, totals)
            prepared[index] = {
                "type": "node",
                "data": {
                    "user_id": adapter.settings.self_id,
                    "nickname": label,
                    "content": content,
                },
            }

        source = _bounded_text(args.get("source"), "source title", 100)
        summary = _bounded_text(args.get("summary"), "summary", 200)
        prompt = _bounded_text(args.get("prompt"), "prompt", 100)
        preview = args.get("preview")
        if preview is not None:
            if not isinstance(preview, list) or len(preview) > 8:
                raise ToolRequestError("preview must contain at most eight lines")
            preview = [
                _bounded_text(line, "preview line", 200, required=True) for line in preview
            ]
        return await adapter.send_agent_forward(
            target,
            [node for node in prepared if node is not None],
            source=source,
            summary=summary,
            prompt=prompt,
            preview=preview,
        )

    return await _on_gateway(
        "qq_send_forward", args, operation, session_id=str(kwargs.get("session_id") or ""))


def _message_summary(data: dict[str, Any], target: Target, identifier: str) -> dict[str, Any]:
    raw = data.get("message")
    text_values: list[str] = []
    attachments: list[dict[str, str]] = []
    types: list[str] = []
    reply_to = None
    if isinstance(raw, str):
        text_values.append(raw)
        types.append("text")
    else:
        for part in segments(raw):
            kind = part["type"]
            types.append(kind)
            detail = part["data"]
            if kind == "text" and isinstance(detail.get("text"), str):
                text_values.append(detail["text"])
            elif kind == "at":
                text_values.append("@" + str(detail.get("qq", ""))[:20])
            elif kind == "face":
                text_values.append("[QQ表情]")
            elif kind == "reply" and detail.get("id") is not None:
                try:
                    reply_to = message_id(detail["id"])
                except ProtocolError:
                    pass
            elif kind in ("image", "record", "video", "file"):
                item = {"type": "audio" if kind == "record" else kind}
                name = detail.get("name")
                if isinstance(name, str) and name:
                    item["name"] = name[:200]
                attachments.append(item)
    sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    text = "".join(text_values)
    return {
        "success": True,
        "target": target.address,
        "message_id": identifier,
        "sender": {
            "user_id": str(data.get("user_id") or sender.get("user_id") or "")[:20],
            "name": str(sender.get("card") or sender.get("nickname") or "")[:100],
        },
        "text": text[:8000],
        "text_truncated": len(text) > 8000,
        "attachments": attachments[:32],
        "segment_types": types[:64],
        "reply_to": reply_to,
    }


@_tool_handler
async def qq_get_message(args: dict[str, Any], **kwargs) -> dict[str, Any]:
    async def operation(adapter, target, session):
        identifier = message_id(args.get("message_id"))
        data = await adapter.verified_message(
            target, identifier, current_message_id=_current_anchor(session, target))
        return _message_summary(data, target, identifier)

    return await _on_gateway(
        "qq_get_message", args, operation, session_id=str(kwargs.get("session_id") or ""))


@_tool_handler
async def qq_get_chat_info(args: dict[str, Any], **kwargs) -> dict[str, Any]:
    async def operation(adapter, target, _session):
        info = await adapter.get_chat_info(target.address)
        return {"success": True, "target": target.address, **info}

    return await _on_gateway(
        "qq_get_chat_info", args, operation, session_id=str(kwargs.get("session_id") or ""))


_TARGET = {
    "type": "string",
    "description": (
        "Optional private:<QQ> or group:<group-id>. Omit for the current chat. "
        "Cross-chat use requires explicit administrator opt-in."
    ),
}
_REPLY = {
    "type": "string",
    "description": "Optional message ID from the same authorized conversation to quote.",
}
_SEGMENT = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["text", "image", "at", "face"]},
        "text": {"type": "string"},
        "source": {"type": "string", "description": "Allowed local path or http(s) URL."},
        "summary": {"type": "string"},
        "qq": {"type": "string", "description": "Numeric QQ ID. @all is forbidden."},
        "id": {"type": "string", "description": "Numeric QQ face ID."},
    },
    "required": ["type"],
    "additionalProperties": False,
}
_FORWARD_SEGMENT = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["text", "image", "audio", "video", "file", "face"],
        },
        "text": {"type": "string"},
        "source": {"type": "string", "description": "Allowed local path or http(s) URL."},
        "name": {"type": "string", "description": "Display name for a file."},
        "thumbnail": {"type": "string", "description": "Optional video thumbnail source."},
        "summary": {"type": "string"},
        "id": {"type": "string", "description": "Numeric QQ face ID."},
    },
    "required": ["type"],
    "additionalProperties": False,
}


def _schema(name: str, description: str, properties: dict[str, Any], required=()) -> dict:
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        parameters["required"] = list(required)
    return {"name": name, "description": description, "parameters": parameters}


_TOOLS = {
    "qq_send_message": (
        qq_send_message,
        "Send an authorized QQ message with ordered text, images, mentions, faces, or a quote.",
        _schema(
            "qq_send_message",
            "Send rich content to the current QQ chat. Use segments for interleaved content.",
            {
                "target": _TARGET,
                "text": {"type": "string"},
                "images": {"type": "array", "items": {"type": "string"}},
                "segments": {"type": "array", "items": _SEGMENT},
                "reply_to": _REPLY,
            },
        ),
    ),
    "qq_send_media": (
        qq_send_media,
        "Send one authorized QQ image, audio clip, video, or file from a controlled source.",
        _schema(
            "qq_send_media",
            "Send a native QQ media item. Audio, video, and file captions may arrive separately.",
            {
                "target": _TARGET,
                "media_type": {
                    "type": "string",
                    "enum": ["image", "audio", "video", "file"],
                },
                "source": {"type": "string", "description": "Allowed local path or http(s) URL."},
                "caption": {"type": "string"},
                "file_name": {"type": "string"},
                "thumbnail": {"type": "string", "description": "Video thumbnail source."},
                "reply_to": _REPLY,
            },
            ("media_type", "source"),
        ),
    ),
    "qq_send_forward": (
        qq_send_forward,
        "Send a QQ merged-forward card containing verified messages and custom multimedia nodes.",
        _schema(
            "qq_send_forward",
            "Send a folded QQ conversation card. A node uses message_id or custom text/segments.",
            {
                "target": _TARGET,
                "nodes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "message_id": {
                                "type": "string",
                                "description": "Existing message in the same authorized chat.",
                            },
                            "label": {"type": "string", "description": "Display label; bot QQ stays the sender."},
                            "text": {"type": "string"},
                            "segments": {"type": "array", "items": _FORWARD_SEGMENT},
                        },
                        "additionalProperties": False,
                    },
                },
                "source": {"type": "string", "description": "Card title/source."},
                "summary": {"type": "string"},
                "prompt": {"type": "string"},
                "preview": {"type": "array", "items": {"type": "string"}},
            },
            ("nodes",),
        ),
    ),
    "qq_get_message": (
        qq_get_message,
        "Read bounded text and attachment types for one verified message in an authorized QQ chat.",
        _schema(
            "qq_get_message",
            "Inspect one message without exposing attachment URLs or arbitrary chat history.",
            {"target": _TARGET, "message_id": {"type": "string"}},
            ("message_id",),
        ),
    ),
    "qq_get_chat_info": (
        qq_get_chat_info,
        "Read basic information for the current or explicitly authorized QQ chat.",
        _schema(
            "qq_get_chat_info",
            "Get the name and type of an authorized QQ conversation.",
            {"target": _TARGET},
        ),
    ),
}


def register_tools(ctx) -> None:
    for name, (handler, description, schema) in _TOOLS.items():
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            is_async=True,
            description=description,
            emoji="QQ",
        )
