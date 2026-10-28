"""Simplicité d'usage : configuration client + auto-test « comme un client »."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from icyloom.clientcheck import (  # noqa: E402
    client_config,
    install_client_config,
    run_checks,
)


def test_client_config_points_at_this_interpreter() -> None:
    block = client_config()["mcpServers"]["icyloom"]
    assert block["command"] == sys.executable
    assert block["args"][:3] == ["-m", "icyloom", "mcp"]
    assert "--transport" in block["args"]


def test_client_config_accepts_a_config_path() -> None:
    block = client_config("D:/tmp/icyloom.yaml")["mcpServers"]["icyloom"]
    assert "--config" in block["args"]
    assert "D:/tmp/icyloom.yaml" in block["args"]


def test_install_writes_a_valid_json(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "mcp.json"
    monkeypatch.setattr("icyloom.clientcheck._client_file", lambda _t: target)
    path = install_client_config("cursor")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "icyloom" in data["mcpServers"]
    assert data["mcpServers"]["icyloom"]["command"] == sys.executable


def test_install_keeps_other_servers(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "mcp.json"
    target.write_text(json.dumps({"mcpServers": {"autre": {"command": "x"}}}),
                      encoding="utf-8")
    monkeypatch.setattr("icyloom.clientcheck._client_file", lambda _t: target)
    install_client_config("cursor")
    data = json.loads(target.read_text(encoding="utf-8"))
    assert "autre" in data["mcpServers"]
    assert "icyloom" in data["mcpServers"]


def test_stdio_transport_works_like_a_real_client() -> None:
    """Le transport stdio doit exposer les 13 outils et répondre."""
    ok, checks = run_checks(transports=("stdio",))
    assert ok, [label for flag, label in checks if not flag]
    assert len(checks) >= 7
