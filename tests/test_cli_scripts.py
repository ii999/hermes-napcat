from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_napcat import cli

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("valid", [True, False])
def test_cli_check_uses_env_and_redacts_secrets(monkeypatch, tmp_path, capsys, valid):
    config = tmp_path / "config.yaml"
    config.write_text("self_id: '100'\n" + ("" if valid else "ws_url: ws://remote.example\n"))
    secret = "random-test-token-never-print-this"
    monkeypatch.setenv("NAPCAT_TOKEN", secret)
    monkeypatch.setenv("NAPCAT_ALLOWED_USERS", "200")
    monkeypatch.setenv("NAPCAT_ALLOW_ALL_USERS", "false")
    monkeypatch.setattr(sys, "argv", ["hermes-napcat", "--config", str(config), "check"])
    result = cli.main()
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert result == (0 if valid else 2)
    if valid:
        assert json.loads(output.out)["allowed_users"] == 1


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("login,remote,should_create", [("ii999", "", True), ("someone-else", "", False), ("ii999", "origin", False)])
def test_publisher_honors_identity_private_visibility_and_existing_remote(monkeypatch, tmp_path, login, remote, should_create):
    module = load_script("publish_github")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(module, "ROOT", project)
    monkeypatch.setattr(module.shutil, "which", lambda name: "/tools/" + name)
    monkeypatch.setattr(sys, "argv", ["publish_github.py", "--repo", "ii999/hermes-napcat"])
    commands = []
    def run(argv, *, capture=False):
        commands.append(argv)
        if argv[1:] == ["api", "user"]:
            return json.dumps({"login": login, "id": 10848912})
        if argv[1:] == ["rev-parse", "--show-toplevel"]:
            return str(project)
        if argv[1:] == ["remote"]:
            return remote
        if argv[1:] == ["diff", "--cached", "--name-only"]:
            return "README.md"
        return ""
    monkeypatch.setattr(module, "run", run)
    if should_create:
        module.main()
        create = next(c for c in commands if c[1:3] == ["repo", "create"])
        assert "--private" in create and "--push" in create and "--public" not in create
        staged = next(c for c in commands if c[1:3] == ["add", "--"])
        assert "requirements-tested.txt" in staged and "MANIFEST.in" in staged
        assert "." not in staged
    else:
        with pytest.raises(SystemExit):
            module.main()
        assert not any(c[1:3] == ["repo", "create"] for c in commands)


def test_publisher_never_uses_nested_shells(monkeypatch):
    module = load_script("publish_github")
    captured = {}
    def run(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return SimpleNamespace(stdout="ok\n")
    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.run(["gh", "api", "user"], capture=True) == "ok"
    assert isinstance(captured["argv"], list)
    assert captured.get("shell", False) is False
    assert captured["cwd"] == ROOT
    assert captured["env"]["GH_HOST"] == "github.com"
