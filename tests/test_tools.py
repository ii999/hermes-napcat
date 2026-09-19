from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from hermes_napcat.media import MediaStore
from hermes_napcat.protocol import Target
from hermes_napcat.tools import ToolSession
from hermes_napcat import tools
from hermes_napcat.transport import DeliveryUncertain


def make_adapter(hermes_doubles, settings, tmp_path, **kwargs):
    root = tmp_path / "outbound"
    root.mkdir(exist_ok=True)
    values = {
        "qq_tools": {"enabled": True},
        "media": {"outbound_roots": [root], "inline_max_bytes": 64 * 1024},
    }
    values.update(kwargs)
    config = hermes_doubles.PlatformConfig(extra=settings(**values).model_dump())
    adapter = hermes_doubles.module.NapCatAdapter(config)
    adapter.media = MediaStore(adapter.settings.media, tmp_path / "cache")
    adapter.transport.call = AsyncMock(return_value={"message_id": 77})
    return adapter, root


def bind_session(monkeypatch, adapter, target="group:300", user="200", current="11"):
    session = ToolSession(Target.parse(target), user, "", "session-1", current)
    monkeypatch.setattr(tools, "_current_session", lambda _session_id="": session)
    runner = SimpleNamespace(_gateway_loop=asyncio.get_running_loop())
    monkeypatch.setattr(tools, "_live_adapter", lambda _profile: (runner, adapter))
    return session


async def test_rich_message_preserves_order_and_verifies_reply(
    monkeypatch, hermes_doubles, settings, tmp_path,
):
    adapter, root = make_adapter(hermes_doubles, settings, tmp_path)
    bind_session(monkeypatch, adapter)
    image = root / "chart.png"
    image.write_bytes(b"image bytes")
    adapter.transport.call.side_effect = [
        {"message_type": "group", "group_id": 300, "user_id": 200, "message": []},
        {"message_id": 77},
    ]

    result = json.loads(await tools.qq_send_message({
        "reply_to": "11",
        "segments": [
            {"type": "text", "text": "报告："},
            {"type": "image", "source": str(image)},
            {"type": "text", "text": "完成"},
        ],
    }, session_id="session-1"))

    assert result == {"success": True, "message_id": "77"}
    calls = adapter.transport.call.call_args_list
    assert calls[0].args == ("get_msg", {"message_id": 11})
    action, payload = calls[1].args
    assert action == "send_group_msg"
    assert [part["type"] for part in payload["message"]] == ["reply", "text", "image", "text"]
    assert payload["message"][2]["data"]["file"].startswith("base64://")


async def test_cross_chat_requires_opt_in_admin_and_target_acl(
    monkeypatch, hermes_doubles, settings, tmp_path,
):
    adapter, _ = make_adapter(hermes_doubles, settings, tmp_path)
    bind_session(monkeypatch, adapter)
    denied = json.loads(await tools.qq_send_message(
        {"target": "private:201", "text": "hello"}, session_id="session-1"))
    assert not denied["success"] and "cross-chat" in denied["error"]
    adapter.transport.call.assert_not_called()

    adapter, _ = make_adapter(
        hermes_doubles, settings, tmp_path,
        qq_tools={"enabled": True, "allow_cross_chat": True},
    )
    bind_session(monkeypatch, adapter, user="201")
    denied = json.loads(await tools.qq_send_message(
        {"target": "private:200", "text": "hello"}, session_id="session-1"))
    assert not denied["success"] and "administrator" in denied["error"]
    adapter.transport.call.assert_not_called()

    bind_session(monkeypatch, adapter, user="200")
    denied = json.loads(await tools.qq_send_message(
        {"target": "private:999", "text": "hello"}, session_id="session-1"))
    assert not denied["success"] and "allowlisted" in denied["error"]
    adapter.transport.call.assert_not_called()


async def test_file_caption_failure_reports_partial_delivery(
    monkeypatch, hermes_doubles, settings, tmp_path,
):
    adapter, root = make_adapter(hermes_doubles, settings, tmp_path)
    bind_session(monkeypatch, adapter, target="private:200")
    report = root / "report.pdf"
    report.write_bytes(b"pdf")
    adapter.transport.call.side_effect = [
        {"file_id": "file-1"},
        DeliveryUncertain("ack lost"),
    ]

    result = json.loads(await tools.qq_send_media({
        "media_type": "file",
        "source": str(report),
        "caption": "完整报告",
    }, session_id="session-1"))

    assert result["success"] is False and result["partial"] is True
    assert result["file_uploaded"] is True and result["delivery_uncertain"] is True
    action, payload = adapter.transport.call.call_args_list[0].args
    assert action == "upload_private_file"
    assert payload["name"] == "report.pdf" and payload["file"].startswith("base64://")


async def test_forward_mixes_verified_message_and_multimedia_node(
    monkeypatch, hermes_doubles, settings, tmp_path,
):
    adapter, root = make_adapter(hermes_doubles, settings, tmp_path)
    bind_session(monkeypatch, adapter)
    image = root / "chart.png"
    image.write_bytes(b"chart")
    adapter.transport.call.side_effect = [
        {"message_type": "group", "group_id": 300, "user_id": 201, "message": []},
        {"message_id": 88},
    ]

    result = json.loads(await tools.qq_send_forward({
        "nodes": [
            {"message_id": "12"},
            {
                "label": "分析助手",
                "segments": [
                    {"type": "text", "text": "图表"},
                    {"type": "image", "source": str(image)},
                ],
            },
        ],
        "source": "研究报告",
        "summary": "共 2 条",
        "prompt": "查看详情",
        "preview": ["原始消息", "图表"],
    }, session_id="session-1"))

    assert result == {"success": True, "message_id": "88"}
    action, payload = adapter.transport.call.call_args_list[1].args
    assert action == "send_group_forward_msg"
    assert payload["messages"][0] == {"type": "node", "data": {"id": "12"}}
    custom = payload["messages"][1]["data"]
    assert custom["user_id"] == "100" and custom["nickname"] == "分析助手"
    assert [part["type"] for part in custom["content"]] == ["text", "image"]
    assert payload["source"] == "研究报告"
    assert payload["news"] == [{"text": "原始消息"}, {"text": "图表"}]


async def test_forward_rejects_foreign_message_before_any_send(
    monkeypatch, hermes_doubles, settings, tmp_path,
):
    adapter, _ = make_adapter(hermes_doubles, settings, tmp_path)
    bind_session(monkeypatch, adapter)
    adapter.transport.call.return_value = {
        "message_type": "group", "group_id": 999, "user_id": 201, "message": [],
    }

    result = json.loads(await tools.qq_send_forward(
        {"nodes": [{"message_id": "12"}]}, session_id="session-1"))

    assert result["success"] is False and "target conversation" in result["error"]
    adapter.transport.call.assert_awaited_once_with("get_msg", {"message_id": 12})


async def test_identical_concurrent_send_is_coalesced(
    monkeypatch, hermes_doubles, settings, tmp_path,
):
    adapter, _ = make_adapter(hermes_doubles, settings, tmp_path)
    bind_session(monkeypatch, adapter)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def call(action, params):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"message_id": 77}

    adapter.transport.call = call
    args = {"text": "send once"}
    first = asyncio.create_task(tools.qq_send_message(args, session_id="session-1"))
    await entered.wait()
    second = asyncio.create_task(tools.qq_send_message(args, session_id="session-1"))
    await asyncio.sleep(0)
    assert calls == 1
    release.set()
    results = [json.loads(item) for item in await asyncio.gather(first, second)]
    assert results == [
        {"success": True, "message_id": "77"},
        {"success": True, "message_id": "77"},
    ]


async def test_get_message_omits_media_urls(
    monkeypatch, hermes_doubles, settings, tmp_path,
):
    adapter, _ = make_adapter(hermes_doubles, settings, tmp_path)
    bind_session(monkeypatch, adapter)
    adapter.transport.call.return_value = {
        "message_type": "group",
        "group_id": 300,
        "user_id": 201,
        "sender": {"user_id": 201, "nickname": "User"},
        "message": [
            {"type": "text", "data": {"text": "see image"}},
            {"type": "image", "data": {"url": "https://secret.example/token", "file": "x"}},
        ],
    }

    raw = await tools.qq_get_message({"message_id": "12"}, session_id="session-1")
    result = json.loads(raw)
    assert result["text"] == "see image"
    assert result["attachments"] == [{"type": "image"}]
    assert "secret.example" not in raw


async def test_tools_fail_closed_when_profile_switch_is_off(
    monkeypatch, hermes_doubles, settings, tmp_path,
):
    adapter, _ = make_adapter(
        hermes_doubles, settings, tmp_path, qq_tools={"enabled": False})
    bind_session(monkeypatch, adapter)
    result = json.loads(await tools.qq_get_chat_info({}, session_id="session-1"))
    assert result["success"] is False and "disabled" in result["error"]
    adapter.transport.call.assert_not_called()
