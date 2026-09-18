from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_napcat.media import MediaStore
from hermes_napcat.plugin import register
from hermes_napcat.transport import DeliveryUncertain


def make_adapter(hermes_doubles, settings, **kwargs):
    cfg = hermes_doubles.PlatformConfig(extra=settings(**kwargs).model_dump())
    adapter = hermes_doubles.module.NapCatAdapter(cfg)
    adapter.transport.call = AsyncMock(return_value={"message_id": 77})
    return adapter


async def test_unauthorized_event_never_downloads_or_calls_api(hermes_doubles, settings, raw_event):
    adapter = make_adapter(hermes_doubles, settings)
    adapter._attachments = AsyncMock()
    await adapter._receive(raw_event(user_id=999, message=[
        {"type": "reply", "data": {"id": "55"}},
        {"type": "image", "data": {"file": "id"}}]))
    adapter._attachments.assert_not_called()
    adapter.transport.call.assert_not_called()
    adapter.handle_message.assert_not_called()


async def test_source_identity_admin_control_and_dedup(hermes_doubles, settings, raw_event):
    adapter = make_adapter(hermes_doubles, settings)
    first = raw_event("/new")
    await adapter._receive(first)
    await adapter._receive(first)
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.call_args.args[0]
    assert event.source.chat_id == "private:200"
    assert event.source.scope_id == "100" and event.source.user_id == "200"
    assert event.allow_gateway_control is True
    await adapter._receive(raw_event("/new", user_id=201))
    assert adapter.handle_message.call_args.args[0].allow_gateway_control is False
    assert adapter.toolsets_for_source(SimpleNamespace(chat_type="group")) == []
    assert adapter.toolsets_for_source(SimpleNamespace(chat_type="dm")) is None


@pytest.mark.parametrize("reply_group,author,accepted", [(300, 100, True), (301, 100, False), (300, 201, False)])
async def test_reply_trigger_checks_server_author_and_chat(hermes_doubles, settings, raw_event, reply_group, author, accepted):
    adapter = make_adapter(hermes_doubles, settings)
    adapter.transport.call.return_value = {"message_type": "group", "group_id": reply_group, "sender": {"user_id": author}}
    await adapter._receive(raw_event(message_type="group", group_id=300, message=[
        {"type": "reply", "data": {"id": "-55", "qq": "100"}},
        {"type": "text", "data": {"text": "hello"}}]))
    assert bool(adapter.handle_message.await_count) is accepted
    if accepted:
        assert adapter.handle_message.call_args.args[0].source.chat_id == "group:300"


async def test_disabled_media_avoids_even_get_image(hermes_doubles, settings, raw_event):
    adapter = make_adapter(hermes_doubles, settings, media={"enabled": False})
    await adapter._receive(raw_event(message=[{"type": "image", "data": {"file": "opaque-id"}}]))
    adapter.transport.call.assert_not_called()
    event = adapter.handle_message.call_args.args[0]
    assert event.media_urls == [] and "未读取" in event.text


async def test_failed_admission_does_not_permanently_deduplicate(hermes_doubles, settings, raw_event):
    adapter = make_adapter(hermes_doubles, settings)
    adapter.handle_message.side_effect = RuntimeError("fixture failed")
    with pytest.raises(RuntimeError):
        await adapter._receive(raw_event())
    adapter.handle_message.side_effect = None
    await adapter._receive(raw_event())
    assert adapter.handle_message.await_count == 2


async def test_text_send_splits_and_never_interprets_cq(hermes_doubles, settings):
    adapter = make_adapter(hermes_doubles, settings, message_chars=100)
    adapter.transport.call.side_effect = [{"message_id": 1}, {"message_id": 2}, {"message_id": 3}]
    text = "[CQ:at,qq=all]" + "字" * 200
    result = await adapter.send("group:300", text, reply_to="-99")
    assert result.success and result.message_id == "3"
    assert result.continuation_message_ids == ("1", "2")
    calls = adapter.transport.call.call_args_list
    assert calls[0].args[0] == "send_group_msg"
    assert calls[0].args[1]["message"][0] == {"type": "reply", "data": {"id": "-99"}}
    assert "".join(c.args[1]["message"][-1]["data"]["text"] for c in calls) == text
    assert all(part["type"] in ("text", "reply") for c in calls for part in c.args[1]["message"])
    result = await adapter.send("group:999", "secret")
    assert not result.success and adapter.transport.call.await_count == 3


async def test_partial_send_failure_preserves_ids_and_is_not_retryable(hermes_doubles, settings):
    adapter = make_adapter(hermes_doubles, settings, message_chars=100)
    adapter.transport.call.side_effect = [{"message_id": 1}, DeliveryUncertain("uncertain")]
    result = await adapter.send("private:200", "x" * 201)
    assert not result.success and not result.retryable
    assert result.raw_response == {"partial_message_ids": ["1"], "delivery_uncertain": True}
    assert adapter.transport.call.await_count == 2


async def test_malformed_send_ack_is_uncertain(hermes_doubles, settings):
    adapter = make_adapter(hermes_doubles, settings)
    adapter.transport.call.return_value = {"message_id": "bad"}
    result = await adapter.send("private:200", "hello")
    assert not result.success and result.raw_response["delivery_uncertain"]


async def test_outbound_media_and_irreversible_upload_validation(hermes_doubles, settings, tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    file = root / "report.txt"
    file.write_text("hello")
    adapter = make_adapter(hermes_doubles, settings, media={"shared_paths": [{"hermes": root, "napcat": "/data/share"}]})
    adapter.media = MediaStore(adapter.settings.media, tmp_path / "cache")
    result = await adapter.send_document("group:300", str(file), caption="hello", reply_to="not-an-id")
    assert not result.success
    adapter.transport.call.assert_not_called()
    result = await adapter.send_document("group:300", str(file))
    assert result.success and result.raw_response["file_uploaded"]
    adapter.transport.call.assert_awaited_once_with("upload_group_file", {"group_id": 300, "file": "/data/share/report.txt", "name": "report.txt"})
    adapter.transport.call.reset_mock()
    result = await adapter.send_image_file("private:200", str(file))
    assert result.success
    assert adapter.transport.call.call_args.args[1]["message"][-1]["data"]["file"] == "file:///data/share/report.txt"


def test_plugin_registration_exposes_gateway_hooks_only(hermes_doubles):
    captured = {}
    register(SimpleNamespace(register_platform=lambda **kwargs: captured.update(kwargs)))
    assert captured["name"] == "napcat"
    assert captured["allowed_users_env"] == "NAPCAT_ALLOWED_USERS"
    assert captured["allow_update_command"] is False
    assert callable(captured["standalone_sender_fn"])
    assert captured["parse_target_ref_fn"]("private:200") == ("private:200", None)
