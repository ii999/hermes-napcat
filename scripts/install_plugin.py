#!/usr/bin/env python3
"""Install into an existing Hermes environment. No core patches or shell-string execution."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(argv: list[str]) -> None:
    subprocess.run(argv, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-python", required=True, type=Path,
                        help="Existing Hermes virtualenv interpreter (use Scripts/python.exe on Windows)")
    parser.add_argument("--hermes-home", type=Path,
                        default=Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))))
    parser.add_argument("--upgrade", action="store_true", help="Back up and replace this plugin's shim")
    parser.add_argument("--wheelhouse", type=Path, help="Offline wheel directory including build dependencies")
    args = parser.parse_args()
    python = args.hermes_python.expanduser().absolute()
    if not python.is_file():
        parser.error("--hermes-python must refer to an existing interpreter")
    home = args.hermes_home.expanduser().resolve()
    if home == Path(home.anchor) or home == Path.home().resolve():
        parser.error("--hermes-home must be a dedicated Hermes directory")
    plugins = home / "plugins"
    target = plugins / "napcat"
    if target.exists() or target.is_symlink():
        if not args.upgrade or target.is_symlink():
            parser.error("plugin directory already exists; inspect it before using --upgrade")
        allowed = {"__init__.py", "plugin.yaml", "__pycache__"}
        if any(p.name not in allowed for p in target.iterdir()):
            parser.error("existing directory has other files; refusing to replace it")
        manifest = target / "plugin.yaml"
        if not manifest.is_file() or "name: napcat" not in manifest.read_text(encoding="utf-8"):
            parser.error("existing directory is not this plugin")
    uv = shutil.which("uv")
    if not uv:
        parser.error("uv is required")
    install = [uv, "pip", "install", "--python", str(python)]
    if args.wheelhouse:
        install += ["--no-index", "--find-links", str(args.wheelhouse.resolve())]
    run(install + [str(ROOT)])
    run([str(python), str(ROOT / "scripts" / "check_hermes_contract.py")])
    plugins.mkdir(parents=True, exist_ok=True)
    staging = plugins / f".napcat-install-{uuid.uuid4().hex}"
    shutil.copytree(ROOT / "plugin", staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if target.exists():
        backups = home / "plugin-backups"
        backups.mkdir(parents=True, exist_ok=True)
        backup = backups / f"napcat-{uuid.uuid4().hex}"
        target.rename(backup)
        print(f"Previous shim preserved: {backup}")
    staging.rename(target)
    print(f"Installed plugin shim: {target}")
    print("Configure NAPCAT_TOKEN/NAPCAT_ALLOWED_USERS and gateway.platforms.napcat, then restart Hermes.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.returncode)
