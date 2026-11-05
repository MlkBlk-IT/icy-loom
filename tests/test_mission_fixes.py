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
from icyloom.config import PolicyConfig, ToolServerConfig
from icyloom.dashboard import snapshot
from icyloom.db import Database
from icyloom.mcpserver import refresh_catalog
from icyloom.policy import PolicyEngine
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
        # Les drapeaux (--from, --with, --python) ne sont pas des paquets :
        # on exige qu'au moins un SPÉC de paquet soit épinglé.
        specs = [a for a in (srv.get("args") or [])
                 if not a.startswith("-") and a not in ("3.12", "3.11", "3.13")]
        pinned = [s for s in specs if "==" in s or "@" in s]
        assert pinned, f"{srv['name']} n'est pas épinglé : {specs}"


def test_A1_broken_servers_are_neutralised() -> None:
    cfg = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    servers = {s["name"]: s for s in cfg["tool_proxy"]["servers"]}
    # `mcp-server-everything` n'existe pas sur PyPI : le serveur est désactivé.
    assert servers["everything"]["enabled"] is False
    # `mcp-server-sqlite` a besoin d'une base explicite, d'une version fixe et
    # d'un SDK compatible : sa release 2025.4.25 exige mcp<1.10 / pydantic<2.9.
    assert "--db-path" in servers["sqlite"]["args"]
    assert any("==" in a for a in servers["sqlite"]["args"])
    assert servers["sqlite"]["enabled"] is True
    assert "mcp==1.9.4" in servers["sqlite"]["args"]
    assert "pydantic==2.8.2" in servers["sqlite"]["args"]


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


# ---------------------------------------------------------------- A3 · registre
def test_A3_registry_indexes_every_active_server() -> None:
    """Chaque serveur actif doit etre indexe sans qu'un client le demande."""
    cfg = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    servers = {s["name"]: ToolServerConfig(name=s["name"], command=s["command"],
                                            args=s.get("args") or [],
                                            enabled=s.get("enabled", True))
               for s in cfg["tool_proxy"]["servers"]}
    listed: list[str] = []

    class FakeProxy:
        async def list_tools(self, name: str) -> dict:
            listed.append(name)
            return {"ok": True}

    # La vraie configuration est une LISTE de ToolServerConfig : la boucle
    # d'indexation doit suivre cette forme (un dict casserait le serveur MCP).
    config_list = list(servers.values())
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        asyncio.run(asyncio.wait_for(
            refresh_catalog(FakeProxy(), config_list, interval=0.01), timeout=0.5))
    assert "sqlite" in listed, "le serveur sqlite actif n'est pas indexe"
    assert "filesystem" in listed
    assert "everything" not in listed, "un serveur desactive ne doit pas etre indexe"


def test_A3_downstream_servers_get_a_usable_environment(config) -> None:
    """Un env vide tue uvx/npx au demarrage (Connection closed)."""
    proxy = ToolProxy(config)
    bare = ToolServerConfig(name="fs", command="uvx", args=["mcp-server-sqlite"],
                             cache_enabled=True)
    assert proxy._session(bare)._params.env is None, \
        "les serveurs descendants doivent heriter de l'environnement (PATH, APPDATA...)"
    custom = ToolServerConfig(name="fs", command="uvx", args=["x"], cache_enabled=True,
                              env={"FOO": "bar"})
    env = proxy._session(custom)._params.env
    assert env and env.get("FOO") == "bar" and env.get("PATH"), \
        "les variables declarees doivent s'ajouter a l'heritage, pas le remplacer"


# ---------------------------------------------------------------- I · politique
def test_I_agents_are_read_only_and_keep_their_own_roots() -> None:
    """Les clients agents : racines declarees seulement, role lecture seule."""
    cfg = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    pol = cfg["policy"]
    assert pol["default_role"] == "reader", \
        "les clients agents ne doivent pas heriter du role administrateur"
    assert not pol["project_roots"], \
        "IcyLoom ne doit pas ajouter de racines aux clients (les leurs suffisent)"
    engine = PolicyEngine(PolicyConfig(**pol))
    read = engine.decide(role="", server="filesystem", tool="read_text_file",
                         side_effects="none")
    write = engine.decide(role="", server="filesystem", tool="write_file",
                          side_effects="write")
    assert read.allowed, "un lecteur doit pouvoir lire"
    assert write.effect == "deny", "un lecteur ne doit pas pouvoir ecrire"
    assert write.reason == "role_forbids_side_effect"
    # l'escalade reste possible explicitement (administrateur).
    escalated = engine.decide(role="operator", server="filesystem", tool="write_file",
                               side_effects="write")
    assert escalated.allowed, "l'admin doit pouvoir escalader explicitement"


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


# ---------------------------------------------------------------- F · version
def test_F_version_has_a_single_source() -> None:
    assert __version__ == "2.2.1"
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in text
    assert 'version = { attr = "icyloom.__version__" }' in text
    assert "\nversion = \"2.0.0\"\n" not in text


# ---------------------------------------------------------------- G · chiffres
def test_G_saved_tokens_are_real_tokens() -> None:
    text = "The quick brown fox jumps over the lazy dog. " * 300
    out, meta = compress_result({"content": [text]}, 4000)
    assert meta["truncated"] is True
    assert meta["saved_tokens"] == meta["original_tokens"] - meta["compressed_tokens"]
    # Un token vaut ~4 caractères : si le compteur mesurait des caractères,
    # tokens et caractères seraient quasi égaux.
    assert meta["original_tokens"] * 3 < meta["original_chars"]


def test_G_unified_view_includes_tool_savings(config) -> None:
    db = Database(config)
    db.ensure_schema()
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO tool_audit (ts,actor,role,intent,server,tool,args_hash,"
            "decision,reason,compacted,tokens_in,tokens_out,tokens_saved,saved_chars,"
            "latency_ms,detail) VALUES (datetime('now'),'a','operator','','fs',"
            "'read_text_file','h','allow','policy_allow','structured',1000,0,400,1200,5,'')")
        # Ligne LEGACY : `saved_chars` NULL => mesurée en caractères, elle ne
        # doit pas entrer dans le domaine « tools » (qui compte des tokens).
        cur.execute(
            "INSERT INTO tool_audit (ts,actor,role,intent,server,tool,args_hash,"
            "decision,reason,compacted,tokens_in,tokens_out,tokens_saved,"
            "latency_ms,detail) VALUES (datetime('now'),'a','operator','','fs',"
            "'read_file','h','allow','policy_allow','structured',1000,0,9999,5,'')")
    with db.cursor(commit=False) as cur:
        rows = cur.execute(
            "SELECT domain, tokens_paid, tokens_saved FROM v_token_ledger").fetchall()
    tools = [r for r in rows if r["domain"] == "tools"]
    assert tools, "la vue unifiée ignore les gains d'outils"
    assert tools[0]["tokens_saved"] == 400
    assert tools[0]["tokens_paid"] == 1000
    assert len(tools) == 1, "une ligne legacy (caractères) a fuité dans la vue"
    # Le dashboard sépare les deux unités : tokens réels vs caractères legacy.
    snap = snapshot(config, hours=24, days=14)
    assert snap["gateway"]["audit"]["tokens_saved"] == 400
    assert snap["gateway"]["audit"]["tokens_saved_legacy_chars"] == 9999


def test_G_hit_rate_ignores_errors(config) -> None:
    db = Database(config)
    db.ensure_schema()
    with db.transaction() as cur:
        for kind in ("hit", "hit", "miss", "error"):
            cur.execute(
                "INSERT INTO llm_events (ts,kind,provider,model,stream,prompt_tokens,"
                "completion_tokens,saved_prompt,saved_completion,latency_ms)"
                " VALUES (datetime('now'),?,'p','m',0,0,0,10,0,5)", (kind,))
    snap = snapshot(config, hours=24, days=14)
    assert snap["kpi"]["errors"] == 1
    # 2 hits / (2 hits + 1 miss) : l'erreur ne compte pas comme un miss.
    assert snap["kpi"]["hit_rate_pct"] == pytest.approx(66.67, abs=0.01)
    assert snap["kpi"]["hit_rate_pct_all"] == pytest.approx(50.0, abs=0.01)


def test_G_audit_saved_uses_real_tokens(config) -> None:
    db = Database(config)
    db.ensure_schema()
    with db.transaction() as cur:
        # ligne nouvelle : tokens réels
        cur.execute(
            "INSERT INTO tool_audit (ts,actor,role,intent,server,tool,args_hash,"
            "decision,reason,compacted,tokens_in,tokens_out,tokens_saved,saved_chars,"
            "latency_ms,detail) VALUES (datetime('now'),'a','operator','','fs',"
            "'read_text_file','h','allow','policy_allow','structured',1000,0,400,1200,5,'')")
        # ligne legacy : le compteur mesurait des caractères. Une ligne
        # antérieure à la migration n'a pas de `saved_chars` (NULL).
        cur.execute(
            "INSERT INTO tool_audit (ts,actor,role,intent,server,tool,args_hash,"
            "decision,reason,compacted,tokens_in,tokens_out,tokens_saved,"
            "latency_ms,detail) VALUES (datetime('now'),'a','operator','','fs',"
            "'read_text_file','h','allow','policy_allow','structured',10,0,9000,5,'')")
    snap = snapshot(config, hours=24, days=14)
    assert snap["gateway"]["audit"]["tokens_saved"] == 400
    assert snap["gateway"]["audit"]["tokens_saved_legacy_chars"] == 9000
