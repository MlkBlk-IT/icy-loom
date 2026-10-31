"""Preuves automatiques exigées par un testeur externe.

Trois preuves, sans API payante, sans fournisseur distant et sans accès à un
modèle :

1. **cache LLM** — aller-retour ``miss`` puis ``hit`` sur un faux amont local :
   la 1re requête appelle l'amont, la 2e requête identique ne l'appelle plus,
   la réponse servie est la même et le journal économique est mis à jour ;
2. **relais d'outils** — un vrai serveur MCP en stdio est atteint, répond, et
   son résultat est servi depuis le cache au 2e appel identique ;
3. **installation** — ``icyloom serve`` puis ``icyloom check --transport all``
   répondent 21/21.

Ces tests sont la preuve reproductible : ils tournent dans la CI, sur
Windows et sur Linux, sans GPU et sans clé d'API.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from conftest import build_config

from icyloom.config import ToolServerConfig
from icyloom.gateway import CACHE_HEADER, create_app
from icyloom.toolproxy import ToolProxy

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Faux serveur MCP descendant, lance en stdio par le relais d'outils.
FAKE_MCP_SERVER = REPO_ROOT / "tools" / "fake_mcp_server.py"

#: Ports d'essai dedies : le perimetre du projet reserve 18150-18153.
PORT_GATEWAY, PORT_HTTP, PORT_SSE, PORT_ADMIN = 18150, 18151, 18152, 18153


# --------------------------------------------------------------------------
# 1. cache LLM : miss puis hit
# --------------------------------------------------------------------------
def test_llm_cache_round_trip_miss_then_hit(config, upstream) -> None:
    """1re requete = miss (amont appele) ; 2e requete identique = hit."""
    body = {"model": "fake-model",
            "messages": [{"role": "user", "content": "preuve du cache LLM"}]}
    app = create_app(config)
    with TestClient(app) as client:
        first = client.post("/v1/chat/completions", json=body)
        second = client.post("/v1/chat/completions", json=body)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.headers.get(CACHE_HEADER) == "miss"
    assert second.headers.get(CACHE_HEADER) == "hit"
    assert upstream.calls == 1, "l'amont ne doit etre appele qu'une seule fois"
    assert second.json()["choices"] == first.json()["choices"], \
        "la reponse servie doit etre identique"

    from icyloom.db import Database

    with Database(config) as db:
        with db.cursor(commit=False) as cur:
            events = [row["kind"] for row in cur.execute(
                "SELECT kind FROM llm_events ORDER BY id").fetchall()]
            ledger = [row["kind"] for row in cur.execute(
                "SELECT kind FROM ledger_entries ORDER BY id").fetchall()]
    assert events == ["miss", "hit"], events
    assert "llm_call" in ledger and "llm_cache_hit" in ledger, ledger


# --------------------------------------------------------------------------
# 2. relais d'outils : vrai serveur MCP stdio
# --------------------------------------------------------------------------
def test_tool_relay_reaches_a_real_stdio_server(config) -> None:
    """Le relais doit atteindre un vrai serveur MCP, puis le mettre en cache."""
    config.tool_proxy.servers = [ToolServerConfig(
        name="demo", command=sys.executable, args=[str(FAKE_MCP_SERVER)],
        cache_enabled=True, ttl_seconds=3600, cache_tools={"read_notes": True})]
    proxy = ToolProxy(config)

    async def scenario() -> tuple[dict, dict, dict]:
        listed = await proxy.list_tools("demo")
        first = await proxy.call("demo", "read_notes", {"port": "8151"})
        second = await proxy.call("demo", "read_notes", {"port": "8151"})
        return listed, first, second

    try:
        listed, first, second = asyncio.run(scenario())
    finally:
        asyncio.run(proxy.close())

    assert listed["ok"] is True, listed
    assert "read_notes" in {tool["name"] for tool in listed["tools"]}
    assert first["ok"] is True, first
    assert first["result"]["is_error"] is False
    assert "MCP streamable-HTTP" in first["result"]["content"][0], first["result"]
    assert second["ok"] is True, second
    assert second.get("cached") is True, "le 2e appel identique doit venir du cache"


# --------------------------------------------------------------------------
# 3. installation : serve puis check --transport all
# --------------------------------------------------------------------------
def _write_service_config(target: Path, tmp_path: Path) -> Path:
    """Configuration d'essai : ports dedies, base temporaire, aucun amont."""
    payload = {
        "server": {"name": "icyloom-proof", "host": "127.0.0.1"},
        "ports": {"gateway_http": PORT_GATEWAY, "mcp_http": PORT_HTTP,
                  "mcp_sse": PORT_SSE, "admin": PORT_ADMIN},
        "database": {"path": str(tmp_path / "icyloom-proof.db")},
        "upstreams": [],
    }
    target.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
                      encoding="utf-8")
    return target


def _wait_for_port(host: str, port: int, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def test_serve_then_check_reports_21_of_21(tmp_path) -> None:
    """`icyloom serve` puis `icyloom check --transport all` : 21/21."""
    config = _write_service_config(tmp_path / "icyloom.yaml", tmp_path)
    env = {**os.environ,
           "ICYLOOM_CONFIG": str(config),
           "ICYLOOM_STATE_DIR": str(tmp_path),
           "ICYLOOM_HOME": "",
           "PYTHONUNBUFFERED": "1"}

    server_log = tmp_path / "serve.log"
    # La sortie du serveur part dans un FICHIER : un tube se remplit et
    # bloquerait le serveur en plein check (c'est arrivé).
    with server_log.open("w", encoding="utf-8") as stream:
        server = subprocess.Popen(
            [sys.executable, "-m", "icyloom", "--config", str(config), "serve"],
            cwd=str(tmp_path), env=env, stdout=stream, stderr=subprocess.STDOUT)
    try:
        assert _wait_for_port("127.0.0.1", PORT_HTTP), \
            "le serveur MCP streamable-HTTP n'a pas demarre"
        assert _wait_for_port("127.0.0.1", PORT_SSE), \
            "le serveur MCP SSE n'a pas demarre"
        result = subprocess.run(
            [sys.executable, "-m", "icyloom", "--config", str(config),
             "check", "--transport", "all"],
            cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120)
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover
            server.kill()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "21/21" in result.stdout, result.stdout + result.stderr
