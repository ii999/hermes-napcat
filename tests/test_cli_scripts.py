from __future__ import annotations

import json
import sys

import pytest

from hermes_napcat import cli

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
