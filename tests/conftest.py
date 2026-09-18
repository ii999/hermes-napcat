"""Test fixtures. Hermes doubles below are NOT a full upstream integration test."""
from __future__ import annotations

import importlib
import sys
import types
from dataclasses import dataclass, field
from enum import Enum
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_napcat.config import Settings

TOKEN = "test-onebot-secret-for-local-tests"


@pytest.fixture
def settings():
    def create(**kwargs):
        values = dict(self_id="100", token=TOKEN, allowed_users=["200", "201"],
                      allowed_groups=["300"], admins=["200"], send_interval=0,
                      request_timeout=0.25, connect_timeout=1,
                      reconnect_min=0.02, reconnect_max=0.05)
        values.update(kwargs)
        return Settings.model_validate(values)
    return create


@pytest.fixture
def raw_event():
    def create(text="hello", **kwargs):
        event = dict(post_type="message", message_type="private", self_id=100,
                     user_id=200, message_id=-10,
                     sender={"nickname": "User", "card": ""},
                     message=[{"type": "text", "data": {"text": text}}])
        event.update(kwargs)
        return event
    return create


@pytest.fixture
def hermes_doubles(monkeypatch, tmp_path):
    """Use the inspected v2026.9.14 signatures, without claiming to execute Hermes."""
    class Platform(str):
        @property
        def value(self):
            return str(self)

    @dataclass
    class PlatformConfig:
        extra: dict = field(default_factory=dict)

    @dataclass
    class SendResult:
        success: bool
        message_id: str | None = None
        error: str | None = None
        raw_response: dict | None = None
        retryable: bool = False
        retry_after: float | None = None
        continuation_message_ids: tuple = ()
        error_kind: str | None = None

    class MessageType(Enum):
        TEXT = "text"
        PHOTO = "photo"
        VOICE = "voice"
        VIDEO = "video"
        DOCUMENT = "document"

    class MessageEvent(SimpleNamespace):
        pass

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config = config
            self.platform = platform
            self.handle_message = AsyncMock()
            self.marked_connected = False

        def build_source(self, **kwargs):
            return SimpleNamespace(platform=self.platform, **kwargs)

        def _mark_connected(self):
            self.marked_connected = True

        def _mark_disconnected(self):
            self.marked_connected = False

    contents = {
        "gateway": {},
        "gateway.config": {"Platform": Platform, "PlatformConfig": PlatformConfig},
        "gateway.platforms": {},
        "gateway.platforms.base": {"BasePlatformAdapter": BasePlatformAdapter, "SendResult": SendResult},
        "gateway.platforms.event": {"MessageType": MessageType, "MessageEvent": MessageEvent},
        "gateway.platforms._shared": {"get_scoped_secret": lambda name, default=None: default},
        "hermes_constants": {"get_hermes_home": lambda: tmp_path / "hermes-home"},
    }
    for name, attributes in contents.items():
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delitem(sys.modules, "hermes_napcat.adapter", raising=False)
    module = importlib.import_module("hermes_napcat.adapter")
    yield SimpleNamespace(module=module, PlatformConfig=PlatformConfig, SendResult=SendResult,
                          MessageType=MessageType)
    # Discard classes referring to fake Hermes modules after this test's monkeypatch scope.
    sys.modules.pop("hermes_napcat.adapter", None)
