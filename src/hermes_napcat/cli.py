"""Operator-only configuration and transport diagnostics; no Hermes import needed."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict
from pathlib import Path

import yaml
from pydantic import ValidationError

from .plugin import settings_from_extra
from .policy import Policy
from .protocol import Target, text_segments
from .transport import OneBotTransport


def load_settings(path: Path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("configuration must be a YAML mapping")
    return settings_from_extra(data, os.environ.get)


async def _operate(args, config):
    async def ignore(_):
        return
    transport = OneBotTransport(config, ignore)
    try:
        await transport.start()
        if config.mode == "reverse":
            async with asyncio.timeout(args.wait):
                await transport.ready.wait()
        if args.command == "probe":
            status = await transport.call("get_status")
            print(json.dumps({"connected": transport.connected, "self_id": config.self_id,
                              "status": status, "transport": asdict(transport.stats)},
                             ensure_ascii=False, indent=2))
        elif args.command == "send":
            target = Target.parse(args.target)
            if not Policy(config).can_send(target):
                raise PermissionError("target is not allowlisted")
            if not args.message.strip() or len(args.message) > config.message_chars:
                raise ValueError("diagnostic message is empty or exceeds message_chars")
            result = await transport.call(target.action,
                                          {**target.params, "message": text_segments(args.message)})
            print(json.dumps({"success": True, "result": result}, ensure_ascii=False))
    finally:
        await transport.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate or probe the NapCat OneBot connection")
    parser.add_argument("--config", type=Path, required=True,
                        help="Flat plugin-extra YAML, e.g. examples/napcat.yaml")
    parser.add_argument("--verbose", action="store_true")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("check", help="Validate configuration without opening a connection")
    for command in ("probe", "send"):
        item = subs.add_parser(command)
        item.add_argument("--wait", type=float, default=30,
                          help="Seconds to wait for a reverse client")
        if command == "send":
            item.add_argument("--target", required=True)
            item.add_argument("--message", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    try:
        config = load_settings(args.config)
        if args.command == "check":
            print(json.dumps({"valid": True, "mode": config.mode, "self_id": config.self_id,
                              "allowed_users": len(config.allowed_users),
                              "allowed_groups": len(config.allowed_groups),
                              "allow_all_users": config.allow_all_users}, indent=2))
        else:
            asyncio.run(_operate(args, config))
        return 0
    except ValidationError as exc:
        for error in exc.errors(include_input=False, include_url=False):
            print(f"{'.'.join(str(v) for v in error['loc'])}: {error['msg']}", file=sys.stderr)
        return 2
    except (ValueError, OSError, PermissionError) as exc:
        print(f"Configuration/operation failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"OneBot operation failed: {type(exc).__name__}. Check the QQ conversation before retrying a send.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
