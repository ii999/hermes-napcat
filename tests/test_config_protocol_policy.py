from __future__ import annotations

import pytest
from pydantic import ValidationError

from hermes_napcat.config import MediaSettings, SharedPath, numeric_id
from hermes_napcat.plugin import parse_target_ref, settings_from_extra, validate_target_ref
from hermes_napcat.policy import Policy, RecentIDs
from hermes_napcat.protocol import Incoming, ProtocolError, Target, message_id, segments, split_text, text_segments


@pytest.mark.parametrize("bad", [True, 0, -2, " 12", "１２", "1.0", None, "1" * 21])
def test_account_identifier_rejects_ambiguous_input(bad):
    with pytest.raises(ValueError):
        numeric_id(bad)


def test_targets_and_message_ids_have_distinct_domains():
    assert Target.parse("private:00200").address == "private:200"
    assert Target.parse("group:200").params == {"group_id": 200}
    assert message_id(-1234) == "-1234"
    assert message_id(0) == "0"
    assert Target.parse("private:200") != Target.parse("group:200")
    assert parse_target_ref(" group:0300 ") == ("group:300", None)
    assert parse_target_ref("300") is None
    assert validate_target_ref("group:300") is True
    assert isinstance(validate_target_ref("private:-3"), str)


@pytest.mark.parametrize("bad", ["300", "dm:3", "group:-3", "private:3:4", "group:", "group:１２"])
def test_target_rejection(bad):
    with pytest.raises(ProtocolError):
        Target.parse(bad)


def test_transport_secure_defaults_and_secret_redaction(settings):
    token = "a-test-secret-that-must-not-leak"
    with pytest.raises(ValidationError) as error:
        settings(token=token, ws_url="ws://example.com:3001")
    assert token not in str(error.value)
    assert settings(ws_url="wss://example.com/onebot").mode == "forward"
    assert settings(ws_url="ws://napcat:3001", allow_insecure_ws=True)
    with pytest.raises(ValidationError):
        settings(ws_url="ws://127.0.0.1:3001/?access_token=anything")
    with pytest.raises(ValidationError):
        settings(mode="reverse", listen_host="0.0.0.0")
    with pytest.raises(ValidationError):
        settings(admins=["999"])
    with pytest.raises(ValidationError):
        settings(event_queue_size=0)


def test_explicit_blank_allowlist_revokes_yaml_users(settings):
    base = settings(admins=[]).model_dump()
    env = {"NAPCAT_ALLOWED_USERS": ""}
    cfg = settings_from_extra(base, lambda key, default: env.get(key, default))
    assert cfg.allowed_users == ()
    env["NAPCAT_ALLOWED_USERS"] = "200, 200,201"
    assert settings_from_extra(base, lambda key, default: env.get(key, default)).allowed_users == ("200", "201")


def test_media_roots_are_explicit_and_dedicated(tmp_path):
    with pytest.raises(ValidationError):
        MediaSettings(outbound_roots=["/"])
    with pytest.raises(ValidationError):
        SharedPath(hermes=tmp_path, napcat="C:\\")
    with pytest.raises(ValidationError):
        MediaSettings(allowed_hosts=["*.qq.com"])
    with pytest.raises(ValidationError):
        MediaSettings(trusted_private_origins=["http://localhost:9/api"])


def test_segment_normalization_and_cq_injection(raw_event):
    parts = [
        {"type": "at", "data": {"qq": "100"}},
        {"type": "reply", "data": {"id": "-22"}},
        {"type": "text", "data": {"text": "hello [CQ:at,qq=all]"}},
        {"type": "image", "data": {"file": "../private.txt"}},
    ]
    parsed = Incoming.parse(raw_event(message=parts))
    assert parsed.mentioned and parsed.reply_to == "-22"
    assert parsed.text == "hello [CQ:at,qq=all][image]"
    out = text_segments(parsed.text, parsed.reply_to)
    assert [p["type"] for p in out] == ["reply", "text"]
    assert out[1]["data"]["text"].endswith("[image]")
    with pytest.raises(ProtocolError):
        segments("[CQ:at,qq=100]hello")
    with pytest.raises(ProtocolError):
        segments([{"type": "text", "data": "bad"}])


@pytest.mark.parametrize("text", ["中文" * 201, "x" * 100 + "\n" + "y" * 201, "a\n" * 600, "🙂" * 201])
def test_splitting_preserves_text_and_length(text):
    chunks = split_text(text, 100)
    assert "".join(chunks) == text
    assert all(0 < len(chunk) <= 100 for chunk in chunks)


def test_acl_is_conjunction_and_default_closed(settings, raw_event):
    denied = Policy(settings(allowed_users=[], admins=[]))
    assert not denied.can_receive(Incoming.parse(raw_event()))
    policy = Policy(settings())
    assert policy.can_receive(Incoming.parse(raw_event()))
    assert not policy.can_receive(Incoming.parse(raw_event(user_id=999)))
    assert not policy.can_receive(Incoming.parse(raw_event(self_id=101)))
    assert not policy.can_receive(Incoming.parse(raw_event(user_id=100)))
    assert policy.can_receive(Incoming.parse(raw_event(message_type="group", group_id=300)))
    assert not policy.can_receive(Incoming.parse(raw_event(message_type="group", group_id=301)))
    assert not policy.can_send(Target.parse("group:301"))


def test_triggers_require_prefix_boundary_or_verified_reply(settings, raw_event):
    policy = Policy(settings())
    def make(text):
        return Incoming.parse(raw_event(text, message_type="group", group_id=300))
    assert policy.trigger(make("/ai summarize")) == "summarize"
    assert policy.trigger(make("/aichat")) is None
    assert policy.trigger(make("hello")) is None
    assert policy.trigger(make("hello"), verified_reply=True) == "hello"
    mention = Incoming.parse(raw_event(message_type="group", group_id=300, message=[
        {"type": "at", "data": {"qq": "100"}}, {"type": "text", "data": {"text": "hi"}}]))
    assert policy.trigger(mention) == "hi"


def test_rate_dedup_limits_and_expiry(settings, raw_event, monkeypatch):
    import hermes_napcat.policy as module
    now = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    ids = RecentIDs(2, 10)
    ids.add("a")
    ids.add("b")
    ids.add("c")
    assert not ids.contains("a") and ids.contains("b")
    now[0] += 11
    assert not ids.contains("b")
    policy = Policy(settings(messages_per_minute=1))
    event = Incoming.parse(raw_event())
    assert policy.rate_allowed(event)
    assert not policy.rate_allowed(event)
    now[0] += 61
    assert policy.rate_allowed(event)
