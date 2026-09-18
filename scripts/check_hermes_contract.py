#!/usr/bin/env python3
"""Run with the real Hermes Python interpreter. Does not start an agent or access QQ."""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    from gateway.config import PlatformConfig
    from gateway.platform_registry import PlatformEntry
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.platforms.event import MessageEvent
    from hermes_napcat.adapter import NapCatAdapter
    from hermes_napcat.plugin import register

    assert issubclass(NapCatAdapter, BasePlatformAdapter)
    assert not inspect.isabstract(NapCatAdapter), NapCatAdapter.__abstractmethods__
    assert "extra" in inspect.signature(PlatformConfig).parameters
    assert "allow_gateway_control" in inspect.signature(MessageEvent).parameters
    assert "scope_id" in inspect.signature(BasePlatformAdapter.build_source).parameters
    entries = []
    register(SimpleNamespace(register_platform=lambda **kw: entries.append(PlatformEntry(**kw))))
    assert len(entries) == 1 and entries[0].name == "napcat"
    assert entries[0].parse_target_ref_fn("group:12345") == ("group:12345", None)
    assert entries[0].validate_target_ref_fn("12345") is not True
    assert entries[0].check_fn()
    print("PASS: real Hermes imports, adapter contract and PlatformEntry registration")
    print("This check does not exercise the gateway authorization/session runner or a real QQ account.")


if __name__ == "__main__":
    main()
