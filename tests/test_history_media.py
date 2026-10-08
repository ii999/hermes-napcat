"""History/forward provenance and resource contracts using controlled OneBot responses."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_napcat.config import Settings
from hermes_napcat.history import expand_forward, read_private_history
from hermes_napcat.media_refs import MediaReferences
from hermes_napcat.policy import Policy
from hermes_napcat.protocol import Target
from hermes_napcat.transport import ActionError


def settings(**extra):
    return Settings.model_validate({
        "self_id": "100", "token": "test-token-0123456789", "allowed_users": ["200"],
        "allowed_groups": ["300"], **extra,
    })


def record(identifier=1, user="200", **extra):
    return {
        "message_id": identifier, "message_type": "private", "user_id": user,
        "sender": {"user_id": user}, "self_id": "100", "time": time.time(),
        "message": [{"type": "text", "data": {"text": f"message {identifier}"}}], **extra,
    }


def adapter(rows=(), **extra):
    config = settings(**extra)
    return SimpleNamespace(
        settings=config, policy=Policy(config),
        media_refs=MediaReferences(config.self_id, config.media.references),
        transport=SimpleNamespace(call=AsyncMock(return_value={"messages": list(rows)})),
        verified_message=AsyncMock(return_value=record()),
    )


async def test_private_history_filters_account_contact_author_time_recall_and_duplicates():
    rows = [record(1), record(2, self_id="999"), record(3, user="999"),
            record(4, time=time.time() - 3600), record(5, user="100"),
            record(6, user="100", target_id="200"), record(7), record(1),
            record(8, sender={"user_id": "999"}), record(9, time=True)]
    instance = adapter(rows)
    instance.media_refs.recall(Target.parse("private:200"), "7")
    page = await read_private_history(instance, Target.parse("private:200"), limit=20)
    assert [row["message_id"] for row in page.messages] == [1, 6]
    assert page.filtered == 8
    action, params = instance.transport.call.await_args.args
    assert action == "get_friend_msg_history"
    assert params == {"user_id": "200", "count": 20, "reverse_order": False,
                      "disable_get_url": True, "parse_mult_msg": False}


async def test_private_history_robot_authorship_needs_destination_or_known_own_id():
    instance = adapter([record(1, user="100"), record(2, user="100"),
                        record(3, user="100", target_id="999")])
    instance.policy.own.add(("private:200", "2"))
    instance.policy.own.add(("private:200", "3"))
    page = await read_private_history(instance, Target.parse("private:200"), limit=10)
    assert [row["message_id"] for row in page.messages] == [2]


async def test_history_anchor_is_verified_before_fetch_and_foreign_or_notice_anchor_is_denied():
    target = Target.parse("private:200")
    instance = adapter()
    instance.verified_message.return_value = record(10)
    await read_private_history(instance, target, limit=10, before="10", current_message_id="10")
    assert instance.transport.call.await_args.args[1]["message_seq"] == "10"
    instance.verified_message.assert_awaited_once_with(target, "10", current_message_id="10")
    instance.transport.call.reset_mock()
    instance.verified_message.return_value = record(10, user="999")
    with pytest.raises(PermissionError):
        await read_private_history(instance, target, limit=10, before="10")
    instance.transport.call.assert_not_awaited()
    instance._file_notices = SimpleNamespace(is_notice_message=lambda *_: True)
    instance.verified_message.reset_mock()
    with pytest.raises(ValueError):
        await read_private_history(instance, target, limit=10, before="10")
    instance.verified_message.assert_not_awaited()


async def test_private_history_recall_during_query_invalidates_anchor():
    instance = adapter()
    target = Target.parse("private:200")
    async def query(*_):
        instance.media_refs.recall(target, "1")
        return {"messages": [record()]}
    instance.transport.call.side_effect = query
    with pytest.raises(PermissionError):
        await read_private_history(instance, target, limit=10, before="1")


def forward_parent(identifier=1, **extra):
    return record(identifier, message=[{"type": "forward", "data": {"id": "forward-a"}}], **extra)


def node(text="forwarded", **extra):
    return {"user_id": "999", "sender": {"user_id": "999", "nickname": "claimed"},
            "message": [{"type": "text", "data": {"text": text}}], **extra}


async def test_forward_derives_ids_from_verified_parent_preserves_claimed_attribution_and_paths():
    instance = adapter()
    instance.verified_message.return_value = forward_parent()
    image = {"type": "image", "data": {"file": "opaque-image", "url": "https://hidden.example/a"}}
    instance.transport.call.side_effect = [
        {"messages": [node(message=[image, {"type": "forward", "data": {"id": "nested"}}])]},
        {"messages": [node("nested text")]},
    ]
    expanded = await expand_forward(instance, Target.parse("private:200"), "1")
    assert [call.args for call in instance.transport.call.await_args_list] == [
        ("get_forward_msg", {"id": "forward-a"}), ("get_forward_msg", {"id": "nested"})]
    assert len(expanded.nodes) == 2
    assert expanded.nodes[0].message["user_id"] == "999"
    assert expanded.nodes[0].message["message"] == [image]
    assert expanded.nodes[1].path[:2] == expanded.nodes[0].path
    assert not expanded.truncated and not expanded.unavailable


async def test_forward_limits_nodes_depth_text_bytes_and_cycles():
    instance = adapter(qq_tools={"max_forward_nodes": 2, "max_forward_depth": 2,
                                 "max_forward_chars": 100})
    instance.verified_message.return_value = forward_parent()
    instance.transport.call.return_value = {"messages": [
        node("界" * 100, message=[{"type": "text", "data": {"text": "界" * 100}},
                                  {"type": "forward", "data": {"id": "forward-a"}}]),
        node("second"), node("excluded"),
    ]}
    expanded = await expand_forward(instance, Target.parse("private:200"), "1")
    text = "".join(part["data"]["text"] for n in expanded.nodes for part in n.message["message"]
                   if part["type"] == "text")
    assert len(text.encode()) <= 100
    assert len(expanded.nodes) <= 2 and expanded.truncated
    instance.transport.call.assert_awaited_once()


async def test_forward_handles_embedded_nodes_and_stops_at_depth_limit():
    child = {"type": "node", "data": {"user_id": "400", "nickname": "child",
                                         "content": [{"type": "text", "data": {"text": "inside"}}]}}
    instance = adapter(qq_tools={"max_forward_depth": 1})
    instance.verified_message.return_value = forward_parent()
    instance.transport.call.return_value = {"messages": [node(message=[child])]}
    expanded = await expand_forward(instance, Target.parse("private:200"), "1")
    assert len(expanded.nodes) == 1 and expanded.truncated
    instance = adapter(qq_tools={"max_forward_depth": 2})
    instance.verified_message.return_value = forward_parent()
    instance.transport.call.return_value = {"messages": [node(message=[child])]}
    expanded = await expand_forward(instance, Target.parse("private:200"), "1")
    assert len(expanded.nodes) == 2
    assert expanded.nodes[1].message["message"][0]["data"]["text"] == "inside"


async def test_forward_reports_partial_unavailable_and_rechecks_recall_after_await():
    instance = adapter()
    target = Target.parse("private:200")
    instance.verified_message.return_value = forward_parent()
    instance.transport.call.side_effect = ActionError("get_forward_msg", 1)
    expanded = await expand_forward(instance, target, "1")
    assert expanded.unavailable == 1 and not expanded.nodes
    async def recalled(*_):
        await asyncio.sleep(0)
        instance.media_refs.recall(target, "1")
        return {"messages": [node()]}
    instance.transport.call.side_effect = recalled
    with pytest.raises(PermissionError):
        await expand_forward(instance, target, "1")


async def test_forward_rejects_foreign_or_expired_parent_before_any_expansion():
    instance = adapter()
    target = Target.parse("private:200")
    for parent in [forward_parent(user="999"), forward_parent(time=time.time() - 3600),
                   forward_parent(self_id="999")]:
        instance.verified_message.return_value = parent
        with pytest.raises(PermissionError):
            await expand_forward(instance, target, "1")
    instance.transport.call.assert_not_awaited()


async def test_forward_tool_bounds_total_refs_and_binds_claimed_authors_to_parent(
    hermes_doubles, monkeypatch,
):
    from hermes_napcat import group_tools
    from hermes_napcat.media import MediaError
    target = Target.parse("private:200")
    config = settings(media={"max_attachments": 2, "references": {"enabled": True}})
    instance = hermes_doubles.module.NapCatAdapter(
        hermes_doubles.PlatformConfig(extra=config.model_dump()))
    images = [{"type": "image", "data": {"file": f"image-{index}",
               "url": "https://secret-locator.example/image"}} for index in range(3)]
    instance.transport.call = AsyncMock(side_effect=[
        forward_parent(), {"messages": [node(message=images), node(message=images)]},
    ])
    session = SimpleNamespace(current_message_id="1", target=target, user_id="200")
    async def operation_boundary(_name, _args, operation, **_kwargs):
        return await operation(instance, target, session)
    monkeypatch.setattr(group_tools, "_on_gateway", operation_boundary)
    result = json.loads(await group_tools.qq_get_forward({"message_id": "1"}))
    assert result["success"] and result["media_refs_truncated"]
    refs = [ref for row in result["nodes"] for ref in row.get("media_refs", [])]
    assert len(refs) == 2
    assert "secret-locator.example" not in json.dumps(result)
    assert result["nodes"][0]["sender"]["user_id"] == "999"
    for ref in refs:
        item = instance.media_refs.get(ref["media_id"], target)
        assert item.user_id == "200" and item.message_id == "1"
    instance.media_refs.recall(target, "1")
    with pytest.raises(MediaError):
        instance.media_refs.get(refs[0]["media_id"], target)
    await instance.disconnect()


async def test_private_history_tool_returns_controlled_media_refs_without_locators(
    hermes_doubles, monkeypatch,
):
    from hermes_napcat import group_tools
    target = Target.parse("private:200")
    config = settings(media={"references": {"enabled": True}})
    instance = hermes_doubles.module.NapCatAdapter(
        hermes_doubles.PlatformConfig(extra=config.model_dump()))
    row = record(message=[{"type": "file", "data": {
        "file_id": "history-file", "url": "https://secret-locator.example/file", "name": "report.pdf"}}])
    instance.transport.call = AsyncMock(return_value={"messages": [row]})
    session = SimpleNamespace(current_message_id="1", target=target, user_id="200")
    async def operation_boundary(_name, _args, operation, **_kwargs):
        return await operation(instance, target, session)
    monkeypatch.setattr(group_tools, "_on_gateway", operation_boundary)
    result = json.loads(await group_tools.qq_get_recent_messages({"limit": 10}))
    assert result["success"] and result["messages"][0]["media_refs"][0]["type"] == "file"
    assert "secret-locator.example" not in json.dumps(result)
    await instance.disconnect()
