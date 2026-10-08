from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hermes_napcat.outbound import send_bundle
from hermes_napcat.protocol import Target
from hermes_napcat.transport import DeliveryUncertain
from test_media import PNG
from test_tools import make_adapter


@pytest.mark.parametrize("force_document", [False, True])
async def test_bundle_rejects_oversized_prepared_request_before_caption(
    hermes_doubles, settings, tmp_path, force_document,
):
    adapter, root = make_adapter(hermes_doubles, settings, tmp_path, ws_max_bytes=1024,
                                media={"shared_paths": [{"hermes": tmp_path / "outbound",
                                                          "napcat": "/" + "r" * 1300}]})
    image = root / "photo.png"
    image.write_bytes(PNG)
    result = await send_bundle(adapter, Target.parse("private:200"), "caption", [(str(image), False)],
                               force_document=force_document)
    assert not result["success"] and not result["partial"]
    adapter.transport.call.assert_not_awaited()


async def test_bundle_rejects_oversized_later_text_before_first_send(hermes_doubles, settings, tmp_path):
    adapter, _ = make_adapter(hermes_doubles, settings, tmp_path, ws_max_bytes=1024, message_chars=800)
    result = await send_bundle(adapter, Target.parse("private:200"), "a" * 800 + "界" * 800, [])
    assert not result["success"] and not result["partial"]
    adapter.transport.call.assert_not_awaited()


@pytest.mark.parametrize("kind", ["audio", "video", "file"])
async def test_media_rejects_oversized_caption_before_media_action(hermes_doubles, settings, tmp_path, kind):
    adapter, root = make_adapter(hermes_doubles, settings, tmp_path, ws_max_bytes=1024,
                                media={"shared_paths": [{"hermes": tmp_path / "outbound",
                                                          "napcat": "/media"}]})
    source = root / "attachment.bin"
    source.write_bytes(b"media data")
    from hermes_napcat.media import MediaError

    with pytest.raises(MediaError, match="ws_max_bytes"):
        await adapter.send_agent_media(Target.parse("private:200"), kind, str(source), caption="界" * 400)
    adapter.transport.call.assert_not_awaited()


async def test_bundle_preflights_all_media_before_text(hermes_doubles, settings, tmp_path):
    adapter, root = make_adapter(hermes_doubles, settings, tmp_path)
    good = root / "good.png"
    good.write_bytes(PNG)
    result = await send_bundle(adapter, Target.parse("private:200"), "caption",
                               [(str(good), False), (str(root / "missing.png"), False)])
    assert not result["success"] and not result["partial"]
    adapter.transport.call.assert_not_called()


async def test_bundle_preserves_partial_uncertainty_without_retry(hermes_doubles, settings, tmp_path):
    adapter, root = make_adapter(hermes_doubles, settings, tmp_path)
    image = root / "photo.png"
    image.write_bytes(PNG)
    adapter.transport.call.side_effect = [{"message_id": 9}, DeliveryUncertain("lost ack")]
    result = await send_bundle(adapter, Target.parse("private:200"), "caption", [(str(image), False)])
    assert not result["success"] and result["partial"] and result["delivery_uncertain"]
    assert result["message_ids"] == ["9"] and result["delivered_items"] == 1
    assert adapter.transport.call.await_count == 2


async def test_force_document_reuses_file_upload(hermes_doubles, settings, tmp_path):
    adapter, root = make_adapter(hermes_doubles, settings, tmp_path)
    image = root / "photo.png"
    image.write_bytes(PNG)
    adapter.transport.call.return_value = {"file_id": "uploaded"}
    result = await send_bundle(adapter, Target.parse("private:200"), "", [(str(image), False)],
                               force_document=True)
    assert result["success"] and result["delivered_items"] == 1
    assert adapter.transport.call.await_args.args[0] == "upload_private_file"


async def test_standalone_sender_accepts_hermes_media_pairs(monkeypatch, hermes_doubles, settings, tmp_path):
    from hermes_napcat.plugin import standalone_send

    adapter, root = make_adapter(hermes_doubles, settings, tmp_path)
    image = root / "photo.png"
    image.write_bytes(PNG)
    adapter.connect = AsyncMock(return_value=True)
    adapter.disconnect = AsyncMock()
    monkeypatch.setattr(hermes_doubles.module, "NapCatAdapter", lambda *_a, **_k: adapter)
    result = await standalone_send(adapter.config, "private:200", "", media_files=[(str(image), False)])
    assert result["success"] and result["message_id"] == "77"
    adapter.disconnect.assert_awaited_once()


async def test_standalone_refuses_cross_root_before_side_effects(hermes_doubles, settings, tmp_path):
    adapter, _ = make_adapter(hermes_doubles, settings, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    result = await send_bundle(adapter, Target.parse("private:200"), "caption", [(str(outside), False)])
    assert not result["success"] and not result["partial"]
    adapter.transport.call.assert_not_called()
