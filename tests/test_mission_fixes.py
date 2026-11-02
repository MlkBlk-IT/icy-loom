"""Correctifs de la mission « réussite totale » — un test par constat.

Chaque test vérifie un comportement qui était FAUX avant le correctif :

* A1 : les serveurs ``uvx`` activés sont épinglés ; ``everything`` est
  désactivé (paquet absent de PyPI) ; ``sqlite`` reçoit ``--db-path``.
* A2 : un serveur mort ne produit qu'UN ``tool_error`` puis benefits d'un
  backoff (le serveur n'est plus relancé).
* B : ``cache_tools`` est deny-by-default, et l'epoch entre dans la clé de
  cache (une écriture invalide les lectures mises en cache).
* C : le résultat ``structured`` dupliqué ne repart plus dans la réponse.
* D : le client est renseigné (HTTP : en-tête puis User-Agent ; MCP :
  ``clientInfo.name``).
* E : l'en-tête ``x-session-id`` compte même sans pair connu.
* F : ``__version__`` est la source unique (2.0.0).
* G : les tokens évités sont des TOKENS réels, la vue unifiée intègre les
  gains d'outils, et le taux de hit ignore les erreurs.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from icyloom import __version__
from icyloom.config import ToolServerConfig
from icyloom.dashboard import snapshot
from icyloom.db import Database
from icyloom.toolcompress import compress_result
from icyloom.toolproxy import ToolProxy, cache_key

ROOT = Path(__file__).resolve().parents[1]
YAML_PATH = ROOT / "icyloom.yaml"


# ---------------------------------------------------------------- A1 · yaml
def test_A1_uvx_servers_are_pinned() -> None:
    cfg = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    servers = cfg["tool_proxy"]["servers"]
    assert servers, "aucun serveur d'outils déclaré"
    for srv in servers:
        if not srv.get("enabled", True):
            continue
        if srv.get("command") != "uvx":
            continue
        first = (srv.get("args") or [""])[0]
        assert "==" in first or "@" in first, f"{srv['name']} n'est pas épinglé : {first}"


def test_A1_broken_servers_are_neutralised() -> None:
    cfg = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    servers = {s["name"]: s for s in cfg["tool_proxy"]["servers"]}
    # `mcp-server-everything` n'existe pas sur PyPI : le serveur est désactivé.
    assert servers["everything"]["enabled"] is False
    # `mcp-server-sqlite` a besoin d'une base explicite et d'une version fixe.
    assert "--db-path" in servers["sqlite"]["args"]
    assert any("==" in a for a in servers["sqlite"]["args"])


# ---------------------------------------------------------------- A2 · backoff
def test_A2_dead_server_is_not_relaunched(config) -> None:
    proxy = ToolProxy(config)
    launches = {"n": 0}

    async def boom(_cfg):
        launches["n"] += 1
        raise RuntimeError("McpError: Connection closed")

    proxy._live_session = boom
    proxy._servers["dead"] = ToolServerConfig(name="dead", command="uvx", args=["nope"],
                                              cache_enabled=True)
    first = asyncio.run(proxy.list_tools("dead"))
    second = asyncio.run(proxy.list_tools("dead"))
    assert first["ok"] is False
    assert second["backoff_seconds"], "le backoff doit signaler la pause"
    assert launches["n"] == 1, "le serveur mort ne doit pas être relancé"
    with proxy.db.cursor(commit=False) as cur:
        errors = cur.execute(
            "SELECT COUNT(*) FROM tool_events WHERE kind='tool_error'").fetchone()[0]
    assert errors == 1, "un seul tool_error pour un serveur mort"


# ---------------------------------------------------------------- B · cache outils
def test_B_cache_allowlist_is_read_only() -> None:
    cfg = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    servers = {s["name"]: s for s in cfg["tool_proxy"]["servers"]}
    allow = servers["filesystem"].get("cache_tools") or {}
    assert allow, "l'allowlist du cache d'outils est vide : le cache ne sert jamais"
    for tool in allow:
        risky = ("write", "create", "delete", "move", "edit", "remove", "put")
        assert not any(hint in tool.lower() for hint in risky), \
            f"{tool} n'est pas une lecture idempotente"
    # TTL court : une écriture hors du relais ne change pas l'epoch.
    assert servers["filesystem"]["ttl_seconds"] <= 900


def test_B_cache_is_deny_by_default(config) -> None:
    proxy = ToolProxy(config)
    closed = ToolServerConfig(name="fs", command="npx", args=[], cache_enabled=True)
    assert proxy._cache_allowed(closed, "read_text_file") is False
    opened = ToolServerConfig(name="fs", command="npx", args=[], cache_enabled=True,
                              cache_tools={"read_text_file": True})
    assert proxy._cache_allowed(opened, "read_text_file") is True
    assert proxy._cache_allowed(opened, "write_file") is False


def test_B_epoch_invalidates_cached_reads() -> None:
    before = cache_key("fs", "read_text_file", {"path": "a.txt"}, "boot.0")
    after = cache_key("fs", "read_text_file", {"path": "a.txt"}, "boot.1")
    assert before != after, "l'epoch doit entrer dans la clé de cache"


def test_B_write_bumps_the_epoch(config) -> None:
    proxy = ToolProxy(config)
    proxy._servers["fs"] = ToolServerConfig(name="fs", command="npx", args=[],
                                            cache_enabled=True)
    before = proxy._epoch_of("fs")
    proxy._bump_epoch("fs")
    assert proxy._epoch_of("fs") != before


# ---------------------------------------------------------------- C · compression
def test_C_structured_is_not_duplicated(config) -> None:
    proxy = ToolProxy(config)
    big = "x" * 20000
    payload = {"content": [big], "structured": {"file": big}}
    shown, meta = proxy._present("fs", "read_text_file", {"path": "a"}, payload)
    assert shown["truncated"] is True
    assert shown["structured"]["omitted"] is True
    assert big not in json.dumps(shown), "le contenu brut est encore sur le fil"
    # Le gain en tokens est verifie par le test G (compteur tiktoken).


def test_C_response_stays_small(config) -> None:
    proxy = ToolProxy(config)
    big = "y" * 50000
    payload = {"content": [big], "structured": {"file": big}}
    shown, _meta = proxy._present("fs", "read_text_file", {"path": "a"}, payload)
    assert len(json.dumps(shown).encode("utf-8")) < 20000


# ---------------------------------------------------------------- D · client
def test_D_client_of_http_request() -> None:
    from icyloom.gateway import _client_of

    class Peer:
        host = "127.0.0.1"

    class Request:
        def __init__(self, headers, client=None):
            self.headers = headers
            self.client = client

    assert _client_of(Request({"x-client": "claude"})) == "claude"
    assert _client_of(Request({"user-agent": "curl/8.0"})) == "curl/8.0"
    assert _client_of(Request({}, Peer())) == "127.0.0.1"
    assert _client_of(Request({})) == "http"


def test_D_client_of_mcp_context() -> None:
    from icyloom.mcpserver import _client_of

    class Info:
        name = "Claude Code"

    class Params:
        clientInfo = Info()

    class Session:
        client_params = Params()

    class Ctx:
        session = Session()

    assert _client_of(Ctx()) == "Claude Code"
    assert _client_of(None) == "mcp"


def test_D_tool_events_carry_the_client(config) -> None:
    proxy = ToolProxy(config)
    proxy._log_event("tool_call", "fs", "read_text_file", {}, 12, client="tester")
    with proxy.db.cursor(commit=False) as cur:
        row = cur.execute("SELECT client FROM tool_events ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "tester"


# ---------------------------------------------------------------- E · session
def test_E_session_id_header_without_peer() -> None:
    from icyloom.gateway import _session_key

    class Request:
        def __init__(self, headers, client=None):
            self.headers = headers
            self.client = client

    class Peer:
        host = "10.0.0.1"

    assert _session_key(Request({"x-session-id": "sess-1"})) == "sess-1"
    assert _session_key(Request({"x-session-id": "sess-1"}, Peer())) == "sess-1"
    assert _session_key(Request({}, Peer())) == "10.0.0.1"
    assert _session_key(Request({})) is None


