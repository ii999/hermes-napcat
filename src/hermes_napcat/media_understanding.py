"""Hermes media analysis boundary, run in the requesting tool's profile context."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .media import Downloaded, MediaError

_RESULT_CHARS = 16_000


def agent_visible_path(path: Path) -> str:
    from tools.credential_files import to_agent_visible_cache_path

    return to_agent_visible_cache_path(str(path))


def _bounded_result(value: Any, *, method: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {"content": value}
    if not isinstance(value, dict):
        raise MediaError(f"Hermes {method} returned an invalid result")
    if value.get("_multimodal") is True and isinstance(value.get("content"), list):
        # Hermes' registry owns native image encoding and model support checks.
        return value
    result = dict(value)
    truncated = False
    for key, item in result.items():
        if isinstance(item, str) and len(item) > _RESULT_CHARS:
            result[key] = item[:_RESULT_CHARS]
            truncated = True
    if truncated:
        result["truncated"] = True
        result["note"] = "Analysis output was truncated; ask a narrower question or read fewer lines."
        # Upstream next_offset points after its whole page, including any text cut here.
        result.pop("next_offset", None)
    result["method"] = method
    return result


def _dispatch(name: str, args: dict[str, Any], task_id: str) -> Any:
    from tools.registry import registry

    # Dispatch alone does not check the configured capability's availability.
    if not registry.get_definitions({name}, quiet=True):
        raise MediaError(f"Hermes {name} is unavailable; configure its required capability")
    return registry.dispatch(name, args, task_id=task_id)


async def read_media(downloaded: Downloaded, *, kind: str, question: str,
                     task_id: str, offset: int = 1, limit: int = 200) -> dict[str, Any]:
    """Use the same installed handlers Hermes uses for ordinary media tools."""
    mime = downloaded.mime
    if kind == "image" or mime.startswith("image/"):
        name = "vision_analyze"
        args = {"image_url": str(downloaded.path), "question": question}
    elif kind == "record" or mime.startswith("audio/"):
        from tools.transcription_tools import transcribe_audio, transcribe_audio_local_fallback

        result = await asyncio.to_thread(transcribe_audio, str(downloaded.path), source="gateway")
        if isinstance(result, dict) and not result.get("success"):
            fallback = await asyncio.to_thread(transcribe_audio_local_fallback, str(downloaded.path))
            if isinstance(fallback, dict) and fallback.get("success"):
                result = {**fallback, "fallback": True,
                          "fallback_reason": "Configured transcription failed; used an installed local backend."}
        return _bounded_result(result, method="transcription")
    elif kind == "video" or mime.startswith("video/"):
        name = "video_analyze"
        args = {"video_url": str(downloaded.path), "question": question}
    else:
        name = "read_file"
        args = {"path": agent_visible_path(downloaded.path), "offset": offset, "limit": limit}
    result = await asyncio.to_thread(_dispatch, name, args, task_id)
    return _bounded_result(result, method=name)
