"""File notices preserve authorization admission and never masquerade as retrievable messages."""
import time

import pytest

from hermes_napcat.config import Settings
from hermes_napcat.file_notices import FileNotices
from hermes_napcat.protocol import Incoming, Target


def helper(**extra):
    return FileNotices(Settings.model_validate({
        "self_id": "100", "token": "test-token-0123456789", "allowed_users": ["200"],
        "allowed_groups": ["300"], **extra,
    }))


def notice(**extra):
    return {"post_type": "notice", "notice_type": "group_upload", "self_id": "100",
            "user_id": "200", "group_id": "300", "time": time.time(),
            "file": {"id": "opaque-file", "name": "report.pdf", "size": 120}, **extra}


def incoming(instance, raw):
    normalized = instance.normalize(raw)
    assert normalized is not None
    return Incoming.parse(normalized)


def test_group_and_private_file_notice_normalize_without_consuming_admission():
    instance = helper()
    raw = notice()
    event = incoming(instance, raw)
    assert event.target == Target.parse("group:300")
    assert event.user_id == "200" and event.message_id.startswith("-")
    assert event.raw["_napcat_file_notice"] is True
    assert event.segments[0]["data"]["file_id"] == "opaque-file"
    assert not instance._files and not instance._notices
    assert incoming(instance, raw).message_id == event.message_id
    private = incoming(instance, notice(notice_type="offline_file"))
    assert private.target == Target.parse("private:200")
    assert instance.admit(event)
    assert not instance.admit(incoming(instance, raw))


@pytest.mark.parametrize("change", [
    {"self_id": "999"}, {"user_id": "bad"}, {"group_id": "bad"}, {"time": True},
    {"time": float("nan")}, {"time": time.time() - 100_000},
    {"file": {"id": "/remote/file", "name": "file", "size": 1}},
    {"file": {"id": "file", "name": "file", "size": True}},
    {"file": {"id": "file", "name": "bad\nname", "size": 1}},
    {"file": {"id": "file", "name": "file", "size": 100_000_000}},
])
def test_notice_rejects_wrong_identity_or_unbounded_invalid_fields(change):
    instance = helper()
    assert instance.normalize(notice(**change)) is None
    assert not instance._files and not instance._notices


def test_existing_messages_and_recalls_are_passed_through():
    instance = helper()
    for raw in [{"post_type": "message"}, {"post_type": "notice", "notice_type": "group_recall"}]:
        assert instance.normalize(raw) is raw


def message(event, identifier="44", parts=None):
    raw = {**event.raw, "message_id": identifier, "message": parts or event.segments}
    raw.pop("_napcat_file_notice")
    return Incoming.parse(raw)


def test_known_file_id_deduplicates_notice_and_file_only_message_but_keeps_text():
    instance = helper()
    event = incoming(instance, notice())
    assert instance.admit(event)
    assert not instance.admit(message(event))
    text = {"type": "text", "data": {"text": "Please inspect the attached report."}}
    assert instance.admit(message(event, parts=[text, *event.segments]))
    # Rollback of the text message cannot remove the prior successful notice's file ownership.
    instance.discard(message(event, parts=[text, *event.segments]))
    assert instance.admit(message(event, identifier="45"))


def test_message_first_deduplicates_notice_and_author_or_chat_do_not_share_keys():
    instance = helper(allowed_users=["200", "201"], allowed_groups=["300", "301"])
    event = incoming(instance, notice())
    assert instance.admit(message(event))
    assert not instance.admit(event)
    assert instance.admit(incoming(instance, notice(user_id="201")))
    assert instance.admit(incoming(instance, notice(group_id="301")))


def test_distinct_ordinary_resends_survive_after_notice_pairing():
    instance = helper()
    event = incoming(instance, notice())
    assert instance.admit(event)
    first = message(event, identifier="10")
    assert not instance.admit(first)
    second = message(event, identifier="11")
    assert instance.admit(second)
    assert not instance.admit(second)
    assert not instance.admit(first)
    assert not instance.admit(event)
    assert instance.admit(message(event, identifier="12"))


def test_distinct_ordinary_messages_survive_without_a_notice():
    instance = helper()
    event = incoming(instance, notice())
    assert instance.admit(message(event, identifier="10"))
    assert instance.admit(message(event, identifier="11"))
    assert not instance.admit(event)
    assert instance.admit(message(event, identifier="12"))


def test_url_only_notice_is_internal_dedup_and_unsafe_paths_never_become_sources():
    instance = helper()
    raw = notice(file={"url": "https://cdn.example/file", "name": "file.bin", "size": 10})
    event = incoming(instance, raw)
    assert instance.admit(event) and not instance.admit(incoming(instance, raw))
    assert instance.normalize(notice(file={"url": "file:///remote/path", "name": "file", "size": 10})) is None


def test_notice_identifier_remains_nonretrievable_after_expiry_eviction_or_rollback(monkeypatch):
    instance = helper()
    event = incoming(instance, notice())
    assert instance.admit(event)
    assert instance.is_notice_message(event.target, event.message_id)
    instance.discard(event)
    assert not instance._files and not instance._notices
    assert instance.is_notice_message(event.target, event.message_id)
    assert instance.admit(event)
    monkeypatch.setattr("hermes_napcat.file_notices.time.time", lambda: time.monotonic() + 10**12)
    assert instance.is_notice_message(event.target, event.message_id)
    assert not instance._files and not instance._notices


def test_notice_admission_capacity_is_bounded_and_clear_releases_records():
    instance = helper()
    instance.capacity = 2
    for index in range(5):
        assert instance.admit(incoming(instance, notice(file={
            "id": f"file-{index}", "name": "file.bin", "size": 1})))
    assert len(instance._files) == len(instance._notices) == 2
    instance.clear()
    assert not instance._files and not instance._notices
