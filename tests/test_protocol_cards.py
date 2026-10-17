"""Card presentation contracts using synthetic OneBot events, without live QQ access."""
import json

import pytest

from hermes_napcat.protocol import Incoming


def parse(raw_event, part):
    return Incoming.parse(raw_event(message_id=-22, message=[part]))


@pytest.mark.parametrize("data", [{"id": "opaque-resource"}, {"content": []}])
def test_forward_hint_uses_parent_message_id_and_preserves_segment(raw_event, data):
    part = {"type": "forward", "data": data}
    parsed = parse(raw_event, part)
    assert "qq_get_forward" in parsed.text and '"message_id": "-22"' in parsed.text
    assert "opaque-resource" not in parsed.text
    assert parsed.segments == [part]


@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("layout,url_key", [("news", "jumpUrl"), ("detail_1", "qqdocurl")])
def test_json_share_extracts_only_known_display_fields(raw_event, encoded, layout, url_key):
    payload = {"meta": {layout: {
        "title": "A title", "desc": "A summary", url_key: "https://example.com/page?a=1&b=2",
        "preview": "https://hidden.example/image", "token": "hidden-token",
    }}, "extra": {"secret": "hidden-secret"}}
    part = {"type": "json", "data": {"data": json.dumps(payload) if encoded else payload}}
    parsed = parse(raw_event, part)
    assert all(value in parsed.text for value in (
        "A title", "A summary", "https://example.com/page?a=1&b=2"))
    assert "hidden" not in parsed.text
    assert parsed.segments == [part]


def test_direct_share_preserves_surrounding_text_and_does_not_interpret_card_mentions(raw_event):
    card = {"type": "share", "data": {
        "title": "[CQ:at,qq=100]", "content": "Summary\nsecond line",
        "url": "https://example.com/page", "image": "hidden-image"}}
    parsed = Incoming.parse(raw_event(message=[
        {"type": "text", "data": {"text": "before "}}, card,
        {"type": "text", "data": {"text": " after"}},
    ]))
    assert parsed.text.startswith("before ") and parsed.text.endswith(" after")
    assert "Summary\\nsecond line" in parsed.text and "https://example.com/page" in parsed.text
    assert not parsed.mentioned and parsed.reply_to is None
    assert "hidden-image" not in parsed.text


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "file:///private/file", "//example.com/page", "https://",
    "https://user:password@example.com", "https://example.com:bad/", "https://[broken/",
    "https://example.com/\nspoof", "https://example.com/\\spoof", "https://example.com/\ud800",
    "https://example.com/path\x80suffix", "https://example.com/path\x9fsuffix",
    "https://example.com/" + "x" * 2048,
])
def test_invalid_card_link_is_omitted_without_losing_title(raw_event, url):
    parsed = parse(raw_event, {"type": "share", "data": {"title": "Title", "url": url}})
    assert "Title" in parsed.text
    assert '"url"' not in parsed.text


@pytest.mark.parametrize("payload", ["{broken", "[]", "null", 123, None, "[" * 2000])
def test_malformed_json_card_is_explicit_and_does_not_drop_adjacent_text(raw_event, payload):
    parsed = Incoming.parse(raw_event(message=[
        {"type": "json", "data": {"data": payload}},
        {"type": "text", "data": {"text": "visible text"}},
    ]))
    assert "内容无效" in parsed.text and parsed.text.endswith("visible text")


def test_unknown_card_does_not_dump_arbitrary_payload(raw_event):
    parsed = parse(raw_event, {"type": "json", "data": {"data": {
        "meta": {"unknown": {"title": "hidden-title", "url": "https://hidden.example"}},
        "prompt": "hidden-prompt",
    }}})
    assert "未识别" in parsed.text and "hidden" not in parsed.text


def test_card_field_types_limits_and_surrogates(raw_event):
    parsed = parse(raw_event, {"type": "share", "data": {
        "title": "t" * 300, "content": "s" * 1200, "url": 42}})
    assert "t" * 256 in parsed.text and "t" * 257 not in parsed.text
    assert "s" * 1024 in parsed.text and "s" * 1025 not in parsed.text
    assert "…" in parsed.text
    parsed = parse(raw_event, {"type": "share", "data": {
        "title": {"private": "hidden"}, "content": "summary\ud800", "url": []}})
    assert "summary" in parsed.text and "hidden" not in parsed.text
    parsed.text.encode("utf-8")
    empty = parse(raw_event, {"type": "share", "data": {}})
    assert "无可读取" in empty.text


def test_json_payload_limit_counts_utf8_bytes(raw_event):
    payload = json.dumps({"meta": {"news": {"title": "界" * 22000}}}, ensure_ascii=False)
    parsed = parse(raw_event, {"type": "json", "data": {"data": payload}})
    assert "超过解析上限" in parsed.text and "界" not in parsed.text


def test_card_whitespace_does_not_hide_fields_or_truncation(raw_event):
    parsed = parse(raw_event, {"type": "share", "data": {
        "title": " " * 300 + "Visible title" + " " * 300,
        "content": "s" * 1024 + " " + "omitted tail",
    }})
    assert '"title": "Visible title"' in parsed.text
    assert "s" * 1024 + "…" in parsed.text and "omitted tail" not in parsed.text
    complete = parse(raw_event, {"type": "share", "data": {
        "title": "t" * 256 + " " * 300,
    }})
    assert "t" * 256 in complete.text and "…" not in complete.text


def test_miniapp_can_use_web_url_when_qqdocurl_is_not_usable(raw_event):
    parsed = parse(raw_event, {"type": "json", "data": {"data": {
        "meta": {"detail_1": {"qqdocurl": "mqqapi://app", "url": "https://example.com"}},
    }}})
    assert "https://example.com" in parsed.text and "mqqapi" not in parsed.text
