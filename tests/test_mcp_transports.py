"""Tests de transport du serveur MCP unifié (couche 4).

Vérifie les trois transports : **stdio**, **streamable-HTTP**, **SSE**.
Sur stdio, on vérifie en plus que stdout ne contient **que** du JSON-RPC
(les logs doivent partir sur stderr) et que l'UTF-8 est forcé.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

PROJECT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

EXPECTED_TOOLS = {
    "cache", "tool_proxy", "proxy_info", "tool_cache_clear",
    "ctx_store", "ctx_get", "ctx_search", "ctx_verify", "ctx_check",
    "ctx_profile", "ctx_session", "ctx_admin", "ledger",
}

HTTP_PORT = 8155
SSE_PORT = 8156


def _write_config(tmp_path: Path, **overrides) -> Path:
    """Écrit un YAML de configuration de test (base temporaire)."""
    payload = {
        "server": {"name": "icyloom-test", "host": "127.0.0.1"},
        "ports": {"gateway_http": 8154, "mcp_http": HTTP_PORT, "mcp_sse": SSE_PORT,
                  "admin": 8157},
        "database": {"path": str(tmp_path / "icyloom-mcp.db"), "journal_mode": "WAL",
                     "busy_timeout_ms": 5000, "synchronous": "NORMAL"},
        "compression": {"enabled": False, "profile": "strict"},
        "upstreams": [{"name": "fake", "base_url": "http://127.0.0.1:8154/v1",
                       "priority": 10}],
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(payload.get(key), dict):
            payload[key].update(value)
        else:
            payload[key] = value
    target = tmp_path / "icyloom-test.yaml"
    target.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
                      encoding="utf-8")
    return target


def _drain(stream, q: "queue.Queue[str]") -> None:
    for line in stream:
        q.put(line.rstrip("\n"))
    q.put("__EOF__")


# --------------------------------------------------------------------------
# stdio
# --------------------------------------------------------------------------
def test_stdio_handshake_lists_exactly_13_tools(tmp_path) -> None:
    config = _write_config(tmp_path)
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTHONIOENCODING", None)
    proc = subprocess.Popen(
        [PYTHON, "-m", "icyloom", "--config", str(config), "mcp", "--transport", "stdio"],
        cwd=str(PROJECT), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    out_q: "queue.Queue[str]" = queue.Queue()
    err_lines: list[str] = []
    threading.Thread(target=_drain, args=(proc.stdout, out_q), daemon=True).start()
    threading.Thread(target=lambda: [err_lines.append(l.rstrip("\n")) for l in proc.stderr],
                     daemon=True).start()

    def send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                     "clientInfo": {"name": "test", "version": "1.0"}}})

    tools: list[str] = []
    init: dict = {}
    raw_lines: list[str] = []
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            line = out_q.get(timeout=1.0)
        except queue.Empty:
            continue
        if line == "__EOF__":
            break
        if not line.strip():
            continue
        raw_lines.append(line)
        message = json.loads(line)          # doit toujours être du JSON
        assert message.get("jsonrpc") == "2.0", line
        if message.get("id") == 1 and "result" in message:
            init = message["result"]
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        elif message.get("id") == 2 and "result" in message:
            tools = [t["name"] for t in message["result"]["tools"]]
            break
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()

    assert init, "pas de réponse initialize"
    assert set(tools) == EXPECTED_TOOLS, sorted(tools)
    assert len(tools) == 13
    assert raw_lines, "stdout vide"
    # Chaque ligne de stdout est du JSON-RPC : aucune trace de log.
    for line in raw_lines:
        json.loads(line)
    # Les logs existent, mais sur stderr.
    assert err_lines, "aucun log sur stderr"


def test_stdio_tool_call_roundtrip(tmp_path) -> None:
    """Un appel d'outil passe bien par le serveur et rend un résultat structuré."""
    config = _write_config(tmp_path)
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.Popen(
        [PYTHON, "-m", "icyloom", "--config", str(config), "mcp", "--transport", "stdio"],
        cwd=str(PROJECT), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    out_q: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=_drain, args=(proc.stdout, out_q), daemon=True).start()
    threading.Thread(target=lambda: [l for l in proc.stderr], daemon=True).start()

    def send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                     "clientInfo": {"name": "test", "version": "1.0"}}})
    payload = "port 8151 chemin D:/x version 1.2.3 — accent café"
    results: list[dict] = []
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            line = out_q.get(timeout=1.0)
        except queue.Empty:
            continue
        if line == "__EOF__":
            break
        message = json.loads(line)
        if message.get("id") == 1 and "result" in message:
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "ctx_store",
                             "arguments": {"source": "text", "content": payload,
                                           "session_id": "default", "label": "t"}}})
        elif message.get("id") == 2 and "result" in message:
            result = message["result"]
            structured = result.get("structuredContent") or {}
            payload_result = structured.get("result")
            if payload_result is None:
                for block in result.get("content") or []:
                    if block.get("type") == "text":
                        payload_result = json.loads(block["text"])
                        break
            results = payload_result
            if isinstance(results, dict):
                results = [results]
            break
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()

    assert results, "ctx_store n'a rien renvoyé"
    assert results[0]["ok"] is True
    assert results[0]["chunk_ids"]
    # L'UTF-8 a survécu au transport.
    assert "café" in payload


# --------------------------------------------------------------------------
# streamable-HTTP et SSE
# --------------------------------------------------------------------------
def _start_server(config: Path, transport: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.Popen(
        [PYTHON, "-m", "icyloom", "--config", str(config), "mcp", "--transport", transport],
        cwd=str(PROJECT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _wait_port(host: str, port: int, timeout: float = 45.0) -> bool:
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            if sock.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


@pytest.mark.timeout(120) if hasattr(pytest.mark, "timeout") else (lambda f: f)
def test_streamable_http_lists_the_same_tools(tmp_path) -> None:
    import asyncio

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    config = _write_config(tmp_path)
    proc = _start_server(config, "http")
    try:
        assert _wait_port("127.0.0.1", HTTP_PORT), "le serveur HTTP n'a pas démarré"

        async def _go() -> list[str]:
            async with streamablehttp_client(f"http://127.0.0.1:{HTTP_PORT}/mcp") as (
                read, write, _get_session_id
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    return [t.name for t in listed.tools]

        names = asyncio.run(_go())
        assert set(names) == EXPECTED_TOOLS, sorted(names)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


@pytest.mark.timeout(120) if hasattr(pytest.mark, "timeout") else (lambda f: f)
def test_sse_lists_the_same_tools(tmp_path) -> None:
    import asyncio

    from mcp import ClientSession
    from mcp.client.sse import sse_client

    config = _write_config(tmp_path)
    proc = _start_server(config, "sse")
    try:
        assert _wait_port("127.0.0.1", SSE_PORT), "le serveur SSE n'a pas démarré"

        async def _go() -> list[str]:
            async with sse_client(f"http://127.0.0.1:{SSE_PORT}/sse") as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    return [t.name for t in listed.tools]

        names = asyncio.run(_go())
        assert set(names) == EXPECTED_TOOLS, sorted(names)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


@pytest.mark.timeout(120) if hasattr(pytest.mark, "timeout") else (lambda f: f)
def test_ctx_search_can_span_every_session(tmp_path) -> None:
    """Régression : ``session_id=""`` doit chercher dans toutes les sessions.

    L'outil ``ctx_search`` documentait « vide = toutes », mais la valeur vide
    était écrasée par « default » : la recherche ne voyait qu'une session.
    """
    import asyncio

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    config = _write_config(tmp_path)
    proc = _start_server(config, "http")
    try:
        assert _wait_port("127.0.0.1", HTTP_PORT), "le serveur HTTP n'a pas démarré"

        async def _go() -> tuple[int, int]:
            async with streamablehttp_client(
                    f"http://127.0.0.1:{HTTP_PORT}/mcp") as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    for sid, marker in (("alpha", "MARQUEUR_ALPHA_7Q2"),
                                        ("beta", "MARQUEUR_BETA_8R3")):
                        await session.call_tool("ctx_store", {
                            "source": "text",
                            "content": f"document {sid} avec {marker}\n",
                            "session_id": sid,
                            "label": sid,
                        })
                    everywhere = await session.call_tool("ctx_search", {
                        "query": "MARQUEUR", "limit": 20, "session_id": ""})
                    scoped = await session.call_tool("ctx_search", {
                        "query": "MARQUEUR", "limit": 20, "session_id": "alpha"})

                    def _count(result) -> int:
                        payload = result.structuredContent or {}
                        data = payload.get("result")
                        if data is None:
                            for block in result.content or []:
                                data = json.loads(getattr(block, "text", "") or "{}")
                                break
                        if isinstance(data, list):
                            data = data[0]
                        return int((data or {}).get("count", 0))

                    return _count(everywhere), _count(scoped)

        all_count, alpha_count = asyncio.run(_go())
        assert all_count >= 2, f"la recherche globale a trouvé {all_count} document(s)"
        assert alpha_count == 1, f"la recherche par session a trouvé {alpha_count} document(s)"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
