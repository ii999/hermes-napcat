#!/usr/bin/env python3
"""Create a NEW private GitHub repository using the operator's authenticated gh CLI."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(argv: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(argv, cwd=ROOT, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            env={**os.environ, "GH_HOST": "github.com"})
    return result.stdout.strip() if capture else ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="ii999/hermes-napcat", help="owner/repository")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("expected owner/repository")
    gh, git = shutil.which("gh"), shutil.which("git")
    if not gh or not git:
        parser.error("install GitHub CLI (gh) and git, then run gh auth login --hostname github.com --scopes workflow")
    profile = json.loads(run([gh, "api", "user"], capture=True))
    owner = args.repo.split("/", 1)[0]
    if profile["login"].lower() != owner.lower():
        parser.error("repository owner must match the active gh account")
    if not (ROOT / ".git").exists():
        run([git, "init", "--initial-branch=main"])
    actual = Path(run([git, "rev-parse", "--show-toplevel"], capture=True)).resolve()
    if actual != ROOT:
        parser.error("refusing to modify a parent repository")
    if run([git, "remote"], capture=True):
        parser.error("this checkout already has a remote; refusing to change or overwrite it")
    # Stage only project-owned paths; no home directory or user credentials are scanned.
    names = [".gitignore", ".github", "AGENTS.md", "LICENSE", "README.md", "SECURITY.md",
             "pyproject.toml", "MANIFEST.in", "requirements-tested.txt",
             "src", "tests", "plugin", "scripts", "examples", "docs"]
    if (ROOT / "uv.lock").exists():
        names.append("uv.lock")
    run([git, "add", "--", *names])
    # Never alter global git identity configuration.
    email = f"{profile['id']}+{profile['login']}@users.noreply.github.com"
    if run([git, "diff", "--cached", "--name-only"], capture=True):
        run([git, "-c", f"user.name={profile['login']}", "-c", f"user.email={email}",
             "commit", "-m", "feat: native Hermes NapCat OneBot WebSocket plugin"])
    run([gh, "repo", "create", args.repo, "--private", "--source", str(ROOT),
         "--remote", "origin", "--push", "--description",
         "Native Hermes gateway plugin for NapCat QQ via full-duplex OneBot v11 WebSocket"])
    print(run([gh, "repo", "view", args.repo, "--json", "url,visibility", "--jq",
               '"Repository: " + .url + " (" + .visibility + ")"'], capture=True))


if __name__ == "__main__":
    main()
