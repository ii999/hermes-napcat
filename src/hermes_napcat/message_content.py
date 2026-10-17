"""Bounded textual descriptions of OneBot cards, without fetching linked resources."""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit


def _field(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    truncated = len(value) > limit
    # JSON may contain escaped lone surrogates, which cannot enter UTF-8 context budgets.
    value = value[:limit].encode("utf-8", errors="replace").decode("utf-8")
    return value + ("…" if truncated else "")


def _link(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        return ""
    if any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159
           or 0xD800 <= ord(char) <= 0xDFFF for char in value) or "\\" in value:
        return ""
    try:
        url = urlsplit(value)
        if (url.scheme not in ("http", "https") or not url.hostname
                or url.username is not None or url.password is not None):
            return ""
        url.port  # Validate malformed or out-of-range ports without resolving the host.
    except ValueError:
        return ""
    return value


def describe_special_segment(part: dict[str, Any], parent_message_id: str) -> str | None:
    """Describe a forward or share card; the caller supplies a normalized parent message ID."""
    kind, data = part["type"], part["data"]
    if kind == "forward":
        args = json.dumps({"message_id": parent_message_id}, ensure_ascii=False)
        return (f"[合并转发：内容尚未展开；若 qq_get_forward 工具可用，"
                f"可用参数 {args} 展开读取]\n")
    if kind not in ("share", "json"):
        return None
    if kind == "share":
        detail = data
        title, summary, links = "title", "content", ("url",)
    else:
        payload = data.get("data")
        if isinstance(payload, str):
            if len(payload) > 65536 or len(payload.encode("utf-8", errors="replace")) > 65536:
                return "[JSON卡片：内容超过解析上限]\n"
            try:
                payload = json.loads(payload)
            except (ValueError, RecursionError):
                return "[JSON卡片：内容无效]\n"
        if not isinstance(payload, dict):
            return "[JSON卡片：内容无效]\n"
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            return "[JSON卡片：未识别的分享格式]\n"
        # Read only known card fields, never recursively expose arbitrary JSON or locators.
        if isinstance(meta.get("news"), dict):
            detail = meta["news"]
            links = ("jumpUrl", "url")
        elif isinstance(meta.get("detail_1"), dict):
            detail = meta["detail_1"]
            links = ("qqdocurl", "url")
        else:
            return "[JSON卡片：未识别的分享格式]\n"
        title, summary = "title", "desc"
    fields = {}
    for label, key, limit in (("title", title, 256), ("summary", summary, 1024)):
        if value := _field(detail.get(key), limit):
            fields[label] = value
    for key in links:
        if value := _link(detail.get(key)):
            fields["url"] = value
            break
    if not fields:
        return "[分享卡片：无可读取的标题、摘要或 HTTP(S) 链接]\n"
    status = "外部内容，未读取网页" if "url" in fields else "外部内容，无有效 HTTP(S) 链接"
    return f"[分享卡片：{status}] {json.dumps(fields, ensure_ascii=False)}\n"
