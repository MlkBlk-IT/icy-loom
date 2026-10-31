"""Campagne de tests fonctionnels IcyLoom.

Objectif : vérifier que le serveur fonctionne **pour de vrai**, avec des
entrées toutes différentes — pas le même mot, pas le même contexte.

La campagne est hermétique : elle démarre sa propre pile IcyLoom sur des
ports dédiés (8160-8164) avec une base temporaire, joue tous les scénarios,
puis referme tout. Elle n'écrit rien hors de ``var/``.

Lancement :
    .\\.venv\\Scripts\\python.exe tools\\campaign.py

Rapport : ``var/campaign-report.json`` (JSON, un entrée par vérification).
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import yaml

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tests"))

from fake_upstream import FakeUpstream  # noqa: E402

sys.path.insert(0, str(PROJECT / "tools"))
from echo_upstream import EchoUpstream, echo_digest  # noqa: E402

PYTHON = sys.executable
GATEWAY_PORT = 8160
MCP_HTTP_PORT = 8161
MCP_SSE_PORT = 8162
ADMIN_PORT = 8163
UPSTREAM_PORT = 8164

GATEWAY = f"http://127.0.0.1:{GATEWAY_PORT}"
ADMIN = f"http://127.0.0.1:{ADMIN_PORT}"
MCP_HTTP = f"http://127.0.0.1:{MCP_HTTP_PORT}/mcp"
MCP_SSE = f"http://127.0.0.1:{MCP_SSE_PORT}/sse"

DB_PATH = PROJECT / "var" / "campaign.db"
CONFIG_PATH = PROJECT / "var" / "campaign.yaml"
REPORT_PATH = PROJECT / "var" / "campaign-report.json"
LOG_PATH = PROJECT / "var" / "campaign.serve.log"

CACHE_HEADER = "X-IcyLoom-Cache"
EXPECTED_TOOLS = {
    "cache", "tool_proxy", "proxy_info", "tool_cache_clear",
    "ctx_store", "ctx_get", "ctx_search", "ctx_verify", "ctx_check",
    "ctx_profile", "ctx_session", "ctx_admin", "ledger",
}

#: 24 prompts **très** différents : français, anglais, code, SQL, YAML, JSON,
#: maths, unicode, emoji, très long, nombres en toutes lettres.
PROMPTS: list[str] = [
    "Explique la différence entre un cache et un tiroir de contexte",
    "What is the difference between a mutex and a semaphore?",
    "Écris une fonction Python qui inverse une chaîne",
    "SELECT nom, COUNT(*) FROM ventes GROUP BY nom ORDER BY 2 DESC LIMIT 10",
    "```python\ndef fib(n):\n    return n if n < 2 else fib(n-1) + fib(n-2)\n```",
    '{"service": "icyloom", "port": 8150, "actif": true}',
    "port: 8151\ncompression:\n  enabled: false\n  profile: strict",
    "Erreur 502 sur /v1/chat/completions après 1200 ms",
    "Résumé du journal d'audit : 3 tentatives, 1 refus, 0 fuite de secret",
    "Café, naïve, résumé, Ångström — accents et diacritiques",
    "漢字とひらがなのテスト",
    "emoji 🚀 symbols ✓ ✗ ± and arrows → ←",
    "two plus two equals four",
    "12+2 = 14 and 12*12 = 144",
    "a very long repeated prompt " * 60,
    "How to configure WAL mode in SQLite for concurrent readers?",
    "Dockerfile FROM python:3.12-slim RUN pip install icyloom",
    "Erreur : impossible d'écrire dans la base (lecture seule)",
    "Compare p50 and p95 latency over the last 30 days",
    "git log --oneline -n 20 --since='2 weeks ago'",
    "https://example.com/docs/api?page=2&sort=asc",
    "```sql\nBEGIN IMMEDIATE;\nUPDATE cache SET hits=hits+1;\nCOMMIT;\n```",
    "DeepSeek R1 vs Qwen3 vs Llama 4 : quel modèle pour le code ?",
    "zéro une deux trois quatre cinq six sept huit neuf dix",
]

#: 6 documents **très** différents pour le tiroir de contexte.
DOCUMENTS: list[tuple[str, str]] = [
    ("config.yaml",
     "server:\n  name: icyloom\n  host: 127.0.0.1\nports:\n  gateway_http: 8150\n"
     "  admin: 8153\nversion: 3.12.10\nle 2026-10-26\n"),
    ("module.py",
     "def normalize(text: str) -> str:\n    return text.strip().lower()\n\n"
     "# version 2.1.0 — chemin /srv/icyloom/src\n"),
    ("nginx.log",
     "2026/10/26 10:00:01 [error] 502 upstream timed out : /v1/chat/completions\n"
     "2026/10/26 10:00:09 [warn] latency p95 = 1200 ms\n"),
    ("data.json",
     '{"items": [{"id": 1, "name": "café", "prix": 3.5}], "total": 1}\n'),
    ("notes.md",
     "# Notes de conception\n\nLe service écoute sur le port 8151.\n"
     "La base vit dans /srv/icyloom/var.\nVersion 3.12.10\n"),
    ("prose.txt",
     "Le tiroir de contexte range le contexte, les invariants sont conservés, "
     "la compression est stricte. 🚀 Le port 8153 sert la console.\n"),
]

#: 3 documents porteurs d'un marqueur unique **dans le contenu** (seul le
#: corps est indexé par FTS5 ; le label reste une métadonnée).
MARKERS: list[tuple[str, str, str]] = [
    ("s-koala", "MARQUEUR_KOALA_9F3",
     "Le koala dort dans le port 8150. MARQUEUR_KOALA_9F3\n"),
    ("s-lynx", "MARQUEUR_LYNX_7B2",
     "Le lynx chasse dans le port 8151. MARQUEUR_LYNX_7B2\n"),
    ("s-bouquin", "MARQUEUR_BOUQUIN_4D8",
     "Le bouquin lit dans le port 8152. MARQUEUR_BOUQUIN_4D8\n"),
]


#: 8 demandes d'agent **réellement différentes** (plan, risques, test, exposé,
#: menace, rollback, mesure, stratégie). Aucune n'est une variante de l'autre.
AGENT_TASKS: list[tuple[str, str]] = [
    ("plan",
     "Rédige le plan de migration du loader vers le hot-reload, en cinq étapes."),
    ("risques",
     "Quels sont les risques de régression du cache LRU quand le TTL passe à sept jours ?"),
    ("test",
     "Écris un test pytest qui vérifie que le compteur access_count est persisté."),
    ("fts",
     "Explique la différence entre FTS5 et LIKE pour une recherche plein texte."),
    ("menace",
     "What is the risk of writing upstream tokens into the ledger without redaction?"),
    ("rollback",
     "Génère le plan de rollback de la migration additive du schéma SQLite."),
    ("mesure",
     "Compare p50 and p95 latency of the gateway over the last 30 days."),
    ("gros",
     "Propose une stratégie de cache pour des prompts de 200 k tokens."),
]

#: 6 appels d'outils **différents** du serveur MCP descendant.
AGENT_TOOLS: list[dict[str, Any]] = [
    {"server": "demo", "tool": "list_metrics", "args": {}},
    {"server": "demo", "tool": "read_notes", "args": {"port": "8150"}},
    {"server": "demo", "tool": "read_notes", "args": {"port": "8153"}},
    {"server": "demo", "tool": "read_file", "args": {"path": "src/icyloom/gateway.py"}},
    {"server": "demo", "tool": "list_dir", "args": {"path": "src/icyloom"}},
    {"server": "demo", "tool": "search_code", "args": {"pattern": "canonicalizer"}},
    {"server": "demo", "tool": "git_log", "args": {"ref": "main"}},
]


# --------------------------------------------------------------------------
# rapport
# --------------------------------------------------------------------------
class Report:
    """Collecte les vérifications et les affiche au fil de l'eau."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.started = time.time()

    def group(self, title: str) -> None:
        print(f"\n\033[1m{title}\033[0m")

    def check(self, group: str, label: str, ok: bool, detail: str = "") -> bool:
        ok = bool(ok)
        self.rows.append({"group": group, "label": label, "ok": ok, "detail": str(detail)})
        mark = "\033[32m✔\033[0m" if ok else "\033[31m✘\033[0m"
        suffix = f" — {detail}" if detail else ""
        print(f"  {mark} {label}{suffix}")
        return ok

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if not r["ok"]]

    def summary(self) -> str:
        done = len(self.rows)
        ko = len(self.failed)
        elapsed = time.time() - self.started
        return (f"\n{'-' * 68}\n{'RÉSULTAT' :^{68}}\n"
                f"{done - ko}/{done} vérifications réussies "
                f"({ko} échec(s)) en {elapsed:.1f}s")


# --------------------------------------------------------------------------
# outils de bas niveau
# --------------------------------------------------------------------------
def _payload(result: Any) -> Any:
    """Extrait le résultat structuré d'un appel d'outil MCP."""
    structured = getattr(result, "structuredContent", None) or {}
    data = structured.get("result") if isinstance(structured, dict) else None
    if data is None:
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                try:
                    data = json.loads(text)
                except Exception:
                    data = text
                break
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        data = data[0]
    return data


async def _mcp_http(calls: list[tuple[str, dict]]) -> dict:
    """Appels MCP en HTTP : renvoie la liste des résultats, dans l'ordre."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    out: dict[str, Any] = {"tools": [], "results": []}
    async with streamablehttp_client(MCP_HTTP) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            out["tools"] = [t.name for t in listed.tools]
            for name, args in calls:
                out["results"].append(_payload(await session.call_tool(name, args)))
    return out


async def _mcp_sse(calls: list[tuple[str, dict]]) -> dict:
    """Appels MCP en SSE : renvoie la liste des résultats, dans l'ordre."""
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    out: dict[str, Any] = {"tools": [], "results": []}
    async with sse_client(MCP_SSE) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            out["tools"] = [t.name for t in listed.tools]
            for name, args in calls:
                out["results"].append(_payload(await session.call_tool(name, args)))
    return out


def _db(query: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(str(DB_PATH))
    try:
        return con.execute(query, params).fetchall()
    finally:
        con.close()


def _write_config() -> None:
    payload = {
        "server": {"name": "icyloom-campaign", "host": "127.0.0.1",
                   "remote_access_token": None},
        "ports": {"gateway_http": GATEWAY_PORT, "mcp_http": MCP_HTTP_PORT,
                  "mcp_sse": MCP_SSE_PORT, "admin": ADMIN_PORT},
        "database": {"path": str(DB_PATH), "journal_mode": "WAL",
                     "busy_timeout_ms": 5000, "synchronous": "NORMAL"},
        "cache": {"ttl_seconds": 604800, "max_entries": 50000,
                  "semantic_enabled": False},
        "compression": {"enabled": False, "profile": "strict"},
        "context": {"session_ttl_days": 30, "sandbox_enabled": True,
                    "allowed_dirs": [], "redact_enabled": True},
        "tool_proxy": {"default_ttl_seconds": 3600, "loop_window_seconds": 300,
                       "loop_warn_after": 3, "loop_block_after": 5,
                       "loop_max_cycle": 3, "max_result_bytes": 1048576,
                       "connect_timeout_seconds": 20.0, "call_timeout_seconds": 60.0,
                       "servers": [{"name": "demo", "command": PYTHON,
                                    "args": [str(PROJECT / "tools" / "fake_mcp_server.py")],
                                    "cache_enabled": True, "ttl_seconds": 3600,
                                    "cache_tools": {"list_metrics": True,
                                                    "read_notes": True,
                                                    "read_file": True,
                                                    "list_dir": True,
                                                    "search_code": True,
                                                    "git_log": True}}]},
        "ledger": {"enabled": True, "retention_days": 90},
        "breaker": {"failure_threshold": 3, "reset_seconds": 30.0,
                    "half_open_probes": 1},
        "upstreams": [{"name": "fake", "base_url": f"http://127.0.0.1:{UPSTREAM_PORT}/v1",
                       "priority": 10, "kind": "openai", "api_key_env": "",
                       "timeout_seconds": 30.0, "sticky": True}],
    }
    CONFIG_PATH.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
                           encoding="utf-8")


def _wait_http(url: str, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def _wait_port(port: int, timeout: float = 60.0) -> bool:
    import socket

    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


# --------------------------------------------------------------------------
# scénarios
# --------------------------------------------------------------------------
def scenario_cache_variety(report: Report, client: httpx.Client) -> None:
    """24 prompts différents : miss puis hit, sans collision de clé."""
    report.group("A. Cache LLM — 24 prompts différents")
    misses, hits, mismatches = 0, 0, []
    for prompt in PROMPTS:
        body = {"model": "fake-model", "messages": [{"role": "user", "content": prompt}]}
        first = client.post(f"{GATEWAY}/v1/chat/completions", json=body)
        state = first.headers.get(CACHE_HEADER)
        if state == "miss":
            misses += 1
        else:
            mismatches.append((prompt[:28], state))
        second = client.post(f"{GATEWAY}/v1/chat/completions", json=body)
        if second.headers.get(CACHE_HEADER) == "hit":
            hits += 1
        if second.json()["choices"] != first.json()["choices"]:
            mismatches.append((prompt[:28], "réponse différente entre miss et hit"))

    report.check("A", "24 prompts distincts = 24 premiers appels en MISS",
                 misses == len(PROMPTS), f"{misses}/{len(PROMPTS)}")
    report.check("A", "24 rejoués = 24 HITS", hits == len(PROMPTS), f"{hits}/{len(PROMPTS)}")
    report.check("A", "aucune réponse divergente entre miss et hit",
                 not mismatches, str(mismatches[:3]))
    entries, keys = _db("SELECT COUNT(*), COUNT(DISTINCT key) FROM cache_entries")[0]
    report.check("A", "24 entrées en base, 24 clés distinctes",
                 entries == len(PROMPTS) and keys == len(PROMPTS),
                 f"entrées={entries} clés={keys}")
    events = _db("SELECT kind, COUNT(*) FROM llm_events GROUP BY kind")
    report.check("A", "journal : 24 miss + 24 hit",
                 dict(events) == {"miss": len(PROMPTS), "hit": len(PROMPTS)},
                 str(dict(events)))


def scenario_equivalences(report: Report, client: httpx.Client) -> None:
    """Des formulations différentes mais équivalentes doivent tomber en HIT."""
    report.group("B. Cache — formulations différentes, même sens (HIT attendu)")

    def states(prompts: list[str]) -> list[str]:
        out = []
        for prompt in prompts:
            body = {"model": "fake-model",
                    "messages": [{"role": "user", "content": prompt}]}
            out.append(client.post(f"{GATEWAY}/v1/chat/completions", json=body)
                       .headers.get(CACHE_HEADER))
        return out

    cases = [
        ("ponctuation et casse : « Bonjour ! » / « bonjour » / «  bonjour  » / « Bonjour!!! »",
         ["Bonjour !", "bonjour", "  bonjour  ", "Bonjour!!!"], ["miss", "hit", "hit", "hit"]),
        ("accents : « Café » / « cafe »", ["Café", "cafe"], ["miss", "hit"]),
        ("nombres en mots : « two plus two » / « 2+2 »",
         ["two plus two", "2+2"], ["miss", "hit"]),
        ("politesse : « Please summarize the log » / « summarize the log »",
         ["Please summarize the log", "summarize the log"], ["miss", "hit"]),
        ("espaces multiples : « a   b » / « a b »", ["a   b", "a b"], ["miss", "hit"]),
    ]
    for label, prompts, expected in cases:
        got = states(prompts)
        report.check("B", label, got == expected, f"{got} (attendu {expected})")


def scenario_no_false_hit(report: Report, client: httpx.Client) -> None:
    """Des prompts proches mais différents ne doivent jamais se confondre."""
    report.group("C. Cache — anti-collision (prompts proches mais distincts)")
    # « alpha », « alpha! » et « AlphA » sont la MÊME forme canonique
    # (ponctuation et casse repliées) : ils doivent donc se répondre en hit.
    # « alpha1 », « alphb », « beta » et « alph » sont réellement différents :
    # ils doivent chacun être un miss.
    equivalents = ["alpha", "alpha!", "AlphA"]
    distincts = ["alpha1", "alphb", "beta", "alph"]

    def state(prompt: str) -> str:
        body = {"model": "fake-model",
                "messages": [{"role": "user", "content": prompt}]}
        return client.post(f"{GATEWAY}/v1/chat/completions", json=body) \
            .headers.get(CACHE_HEADER)

    got_equiv = [state(p) for p in equivalents]
    got_distinct = [state(p) for p in distincts]

    report.check("C", "3 écritures de « alpha » (casse/ponctuation) = 1 miss + 2 hits",
                 got_equiv == ["miss", "hit", "hit"], str(got_equiv))
    report.check("C", "4 prompts réellement différents = 4 MISS (aucun faux hit)",
                 got_distinct == ["miss"] * 4, str(got_distinct))

    before = _db("SELECT COUNT(*) FROM cache_entries")[0][0]
    state("alpha2")   # voisin d'alpha : nouvelle clé
    state("alpha")    # déjà connu : hit, aucune nouvelle entrée
    after = _db("SELECT COUNT(*) FROM cache_entries")[0][0]
    report.check("C", "« alpha2 » crée une entrée, « alpha » non",
                 after - before == 1, f"+{after - before} entrée(s)")


def scenario_key_scope(report: Report, client: httpx.Client) -> None:
    """Ce qui entre (ou non) dans la clé de cache."""
    report.group("D. Cache — portée de la clé")
    base = {"model": "fake-model",
            "messages": [{"role": "user", "content": "portée de la clé"}]}
    other_model = {**base, "model": "fake-model-2"}

    first = client.post(f"{GATEWAY}/v1/chat/completions", json=base)
    second = client.post(f"{GATEWAY}/v1/chat/completions", json=base,
                         headers={"x-client": "un-autre-client"})
    third = client.post(f"{GATEWAY}/v1/chat/completions", json=other_model)

    report.check("D", "même corps + client différent = HIT (le client n'est pas dans la clé)",
                 first.headers.get(CACHE_HEADER) == "miss"
                 and second.headers.get(CACHE_HEADER) == "hit",
                 f"{first.headers.get(CACHE_HEADER)} puis {second.headers.get(CACHE_HEADER)}")
    report.check("D", "même prompt + modèle différent = MISS (le modèle est dans la clé)",
                 third.headers.get(CACHE_HEADER) == "miss",
                 third.headers.get(CACHE_HEADER))


def scenario_ledger(report: Report, client: httpx.Client) -> None:
    """Le ledger compte les vrais tokens et le gain réel."""
    report.group("E. Ledger — économie réelle")
    metrics = client.get(f"{GATEWAY}/api/metrics").json()
    counters = dict(_db("SELECT kind, COUNT(*) FROM llm_events GROUP BY kind"))
    n_miss = counters.get("miss", 0)
    n_hit = counters.get("hit", 0)
    report.check("E", "tokens réels = 18 par appel amont",
                 metrics["tokens_real"] == 18 * n_miss,
                 f"tokens_real={metrics['tokens_real']} pour {n_miss} appels")
    ledger = client.get(f"{GATEWAY}/api/ledger")
    saved = sum(int(r[0]) for r in
                _db("SELECT tokens_saved FROM ledger_entries"))
    report.check("E", "tokens économisés = 18 par hit",
                 saved == 18 * n_hit, f"ledger={saved} pour {n_hit} hits")
    report.check("E", "endpoint /api/ledger répond avec un résumé",
                 ledger.status_code == 200 and isinstance(ledger.json(), dict),
                 f"status={ledger.status_code}")
    dashboard = client.get(f"{ADMIN}/api/dashboard").json()
    kpi = dashboard["kpi"]
    report.check("E", "gain mesuré ≥ seuil de 35 %",
                 kpi["gain_pct"] >= kpi["threshold_pct"],
                 f"gain={kpi['gain_pct']} % / seuil={kpi['threshold_pct']} %")
    report.check("E", "seuil effectivement franchi", kpi["threshold_met"] is True,
                 str(kpi["threshold_met"]))
    report.check("E", "hit rate cohérent avec le journal",
                 abs(kpi["hit_rate_pct"] - (100 * n_hit / max(1, n_hit + n_miss))) < 0.2,
                 f"hit_rate={kpi['hit_rate_pct']} %")


def scenario_context_docs(report: Report) -> None:
    """6 documents différents rangés puis relus octet pour octet."""
    report.group("F. Tiroir de contexte — 6 documents différents")
    calls = [("ctx_store", {"source": "text", "content": body,
                             "session_id": "campagne", "label": label})
             for label, body in DOCUMENTS]
    out = asyncio.run(_mcp_http(calls))
    stores = out["results"]
    ok = all(isinstance(s, dict) and s.get("ok") for s in stores)
    report.check("F", "6 ctx_store acceptés", ok,
                 str([s.get("error") for s in stores if isinstance(s, dict)
                      and not s.get("ok")]))
    chunk_ids = [cid for s in stores if isinstance(s, dict) for cid in s.get("chunk_ids", [])]
    report.check("F", "chaque document a rendu ses propres fragments",
                 len(chunk_ids) >= len(DOCUMENTS)
                 and len(set(chunk_ids)) >= len(DOCUMENTS),
                 f"{len(chunk_ids)} fragments, {len(set(chunk_ids))} distincts")

    get_calls = [("ctx_get", {"chunk_id": str(cid)}) for cid in chunk_ids]
    got = asyncio.run(_mcp_http(get_calls))["results"]
    rebuilt: dict[str, str] = {}
    for (label, _), chunk in zip(DOCUMENTS, got):
        rebuilt[label] = chunk.get("content", "") if isinstance(chunk, dict) else ""
    exact = []
    for label, body in DOCUMENTS:
        exact.append(rebuilt.get(label, "") == body)
    report.check("F", "relecture byte-exact des 6 documents", all(exact),
                 f"{sum(exact)}/6 identiques")

    verify = asyncio.run(_mcp_http([("ctx_verify", {"session_id": "campagne"})]))["results"][0]
    report.check("F", "ctx_verify : couverture 100 % des invariants",
                 bool(verify.get("ok")) and verify.get("coverage") == 1.0,
                 json.dumps(verify)[:160])
    check = asyncio.run(_mcp_http([("ctx_check", {"session_id": "campagne"})]))["results"][0]
    report.check("F", "ctx_check : aucun invariant perdu",
                 bool(check.get("ok")) and not check.get("invariants_missing"),
                 json.dumps(check)[:160])

    kinds = set()
    for label, body in DOCUMENTS:
        from icyloom.context.anchors import extract

        kinds |= {inv.kind for inv in extract(body)}
    report.check("F", "invariants extraits des 6 documents (port/path/version/date)",
                 {"port", "path", "version", "date"} <= kinds, str(sorted(kinds)))


def scenario_context_search(report: Report) -> None:
    """La recherche doit distinguer trois contenus sans marqueur commun."""
    report.group("G. Tiroir — recherche discriminante")
    calls = [("ctx_store", {"source": "text", "content": body,
                             "session_id": session, "label": marker})
             for session, marker, body in MARKERS]
    asyncio.run(_mcp_http(calls))
    time.sleep(0.2)
    for session, marker, _ in MARKERS:
        found = asyncio.run(_mcp_http([("ctx_search", {"query": marker, "limit": 10,
                                                        "session_id": ""})]))["results"][0]
        count = int(found.get("count", 0))
        bodies = json.dumps(found)
        report.check("G", f"recherche « {marker} » trouve son document",
                     count >= 1 and marker in bodies, f"count={count}")
        others = [m for s, m, _ in MARKERS if s != session]
        report.check("G", f"« {marker} » ne renvoie pas les autres documents",
                     not any(o in bodies for o in others), str(others))
    scoped = asyncio.run(_mcp_http([("ctx_search", {"query": "MARQUEUR", "limit": 10,
                                                     "session_id": "s-lynx"})]))["results"][0]
    report.check("G", "recherche restreinte à une session",
                 int(scoped.get("count", 0)) >= 1
                 and "MARQUEUR_LYNX_7B2" in json.dumps(scoped)
                 and "MARQUEUR_KOALA_9F3" not in json.dumps(scoped),
                 f"count={scoped.get('count')}")
    empty = asyncio.run(_mcp_http([("ctx_search", {"query": "zzz_inexistant_9x7",
                                                    "limit": 5, "session_id": ""})]))["results"][0]
    report.check("G", "recherche d'un marqueur absent = 0 résultat",
                 int(empty.get("count", 0)) == 0, str(empty.get("count")))


def scenario_sessions(report: Report) -> None:
    """Les sessions sont cloisonnées."""
    report.group("H. Tiroir — sessions cloisonnées")
    payload = asyncio.run(_mcp_http([("ctx_session", {"action": "list"})]))["results"][0]
    sessions = payload.get("sessions") if isinstance(payload, dict) else None
    names = {s.get("session_id") for s in (sessions or [])} if sessions else set()
    report.check("H", "3 sessions de marqueurs + 1 session campagne visibles",
                 {"s-koala", "s-lynx", "s-bouquin", "campagne"} <= names, str(sorted(names)))
    rows = _db("SELECT session_id, COUNT(*) FROM context_chunks GROUP BY session_id")
    per_session = {r[0]: r[1] for r in rows}
    report.check("H", "chaque session a ses propres fragments",
                 all(per_session.get(s, 0) > 0 for s in
                     ("s-koala", "s-lynx", "s-bouquin")), str(per_session))
    mixed = _db("SELECT COUNT(*) FROM context_chunks "
                "WHERE body LIKE '%MARQUEUR_KOALA%' AND session_id <> 's-koala'")
    report.check("H", "aucun fragment de koala dans une autre session",
                 mixed[0][0] == 0, f"{mixed[0][0]} fragment(s) mal rangé(s)")


def scenario_tools(report: Report) -> None:
    """Relais d'outils : 3 appels différents, puis rejeu (cache d'outils)."""
    report.group("I. Relais d'outils — appels différents")
    batch = [{"server": "demo", "tool": "list_metrics", "args": {}},
             {"server": "demo", "tool": "read_notes", "args": {"port": "8150"}},
             {"server": "demo", "tool": "read_notes", "args": {"port": "8153"}}]
    out = asyncio.run(_mcp_http([("tool_proxy", {"calls": batch})]))["results"][0]
    results = out.get("results", []) if isinstance(out, dict) else []
    report.check("I", "3 appels d'outils distincts acceptés",
                 len(results) == 3 and all(r.get("ok") for r in results),
                 f"{len(results)} résultat(s)")
    ports = [json.dumps(r) for r in results]
    report.check("I", "chaque appel renvoie sa propre réponse",
                 len(set(ports)) == 3, f"{len(set(ports))} réponses distinctes")

    asyncio.run(_mcp_http([("tool_proxy", {"calls": batch})]))
    hits = _db("SELECT COUNT(*) FROM tool_events WHERE kind='tool_hit'")[0][0]
    cache_hits = _db("SELECT COALESCE(SUM(hits),0) FROM tool_cache")[0][0]
    report.check("I", "le rejeu est servi par le cache d'outils",
                 hits >= 3, f"tool_hit={hits}")
    report.check("I", "compteur tool_cache.hits persisté et cohérent",
                 cache_hits >= 3, f"tool_cache.hits={cache_hits}")
    info = asyncio.run(_mcp_http([("proxy_info", {"what": "stats"})]))["results"][0]
    stats = info.get("stats", {}) if isinstance(info, dict) else {}
    cache_stats = stats.get("cache", {}) if isinstance(stats, dict) else {}
    report.check("I", "proxy_info stats cohérentes avec la base",
                 int(cache_stats.get("hits", 0)) == cache_hits
                 and int(cache_stats.get("entries", 0)) == 3,
                 json.dumps(cache_stats))


def scenario_formats(report: Report, client: httpx.Client) -> None:
    """Les formats d'API et le streaming."""
    report.group("J. Passerelle — formats et streaming")
    chat = client.post(f"{GATEWAY}/v1/chat/completions",
                       json={"model": "echo-model",
                             "messages": [{"role": "user", "content": "format openai"}]})
    payload = chat.json()
    expected = echo_digest({"model": "echo-model",
                            "messages": [{"role": "user", "content": "format openai"}]})
    report.check("J", "OpenAI /v1/chat/completions : choix + usage réels",
                 chat.status_code == 200
                 and payload["choices"][0]["message"]["content"] == f"echo:{expected}"
                 and payload["usage"]["total_tokens"] == 18,
                 f"status={chat.status_code} usage={payload.get('usage')}")

    anthropic = client.post(f"{GATEWAY}/v1/messages",
                             json={"model": "echo-model", "max_tokens": 64,
                                   "messages": [{"role": "user", "content": "format anthropic"}]})
    report.check("J", "Anthropic /v1/messages : 200 + contenu",
                 anthropic.status_code == 200 and "echo:" in anthropic.text,
                 f"status={anthropic.status_code}")

    models = client.get(f"{GATEWAY}/v1/models")
    report.check("J", "/v1/models expose le modèle de l'amont",
                 models.status_code == 200
                 and models.json()["data"][0]["id"] == "echo-model",
                 f"status={models.status_code}")

    health = client.get(f"{GATEWAY}/health")
    report.check("J", "/health répond healthy",
                 health.status_code == 200 and health.json()["status"] == "healthy",
                 health.text[:80])

    # Streaming SSE : le flux doit arriver entier ET être archivé (cache +
    # ledger). C'est ce chemin qui était cassé (NameError en fin de flux).
    before_rows = _db("SELECT COUNT(*) FROM llm_events WHERE stream=1")[0][0]
    ctype, chunks, error = "", [], ""
    try:
        with client.stream("POST", f"{GATEWAY}/v1/chat/completions",
                           json={"model": "fake-model", "stream": True,
                                 "messages": [{"role": "user",
                                               "content": "flux streaming"}]}) as stream:
            ctype = stream.headers.get("content-type", "")
            chunks = [line for line in stream.iter_lines() if line.strip()]
    except Exception as exc:                      # pragma: no cover - diagnostic
        error = f"{type(exc).__name__}: {exc}"
    report.check("J", "streaming SSE : réponse complète (aucune coupure)",
                 not error, error or f"ctype={ctype} lignes={len(chunks)}")
    report.check("J", "streaming SSE : content-type event-stream",
                 "text/event-stream" in ctype, ctype)
    after_rows = _db("SELECT COUNT(*) FROM llm_events WHERE stream=1")[0][0]
    report.check("J", "le flux archivé (llm_events stream=1)",
                 after_rows == before_rows + 1, f"{before_rows} -> {after_rows}")
    streamed = _db("SELECT COUNT(*) FROM cache_entries WHERE raw_hash IS NULL")[0][0]
    report.check("J", "le texte du flux a été mis en cache",
                 streamed >= 1, f"{streamed} entrée(s) sans empreinte d'amont")

    missing = client.get(f"{GATEWAY}/v1/inexistant")
    report.check("J", "route inconnue = 404", missing.status_code == 404,
                 str(missing.status_code))


def scenario_mcp_parity(report: Report) -> None:
    """Les deux transports MCP exposent la même chose et répondent pareil."""
    report.group("K. MCP — parité HTTP / SSE")
    http_out = asyncio.run(_mcp_http([]))
    sse_out = asyncio.run(_mcp_sse([]))
    http_tools = sorted(http_out.get("tools", []))
    sse_tools = sorted(sse_out.get("tools", []))
    report.check("K", "HTTP expose exactement les 13 outils",
                 set(http_tools) == EXPECTED_TOOLS and len(http_tools) == 13,
                 str(http_tools))
    report.check("K", "SSE expose exactement les 13 outils",
                 set(sse_tools) == EXPECTED_TOOLS and len(sse_tools) == 13,
                 str(sse_tools))
    report.check("K", "les deux transports exposent la même liste",
                 http_tools == sse_tools, "identique" if http_tools == sse_tools else "divergent")

    http_store = asyncio.run(_mcp_http([("ctx_store",
                                             {"source": "text",
                                              "content": "contenu PARITE via http",
                                              "session_id": "parite", "label": "http"})]))["results"][0]
    sse_search = asyncio.run(_mcp_sse([("ctx_search", {"query": "parite",
                                                        "session_id": "parite"})]))["results"][0]
    report.check("K", "ctx_store via HTTP puis ctx_search via SSE",
                 bool(http_store.get("ok"))
                 and int(sse_search.get("count", 0)) >= 1,
                 f"store={http_store.get('ok')} count={sse_search.get('count')}")


def scenario_robustness(report: Report, client: httpx.Client) -> None:
    """Garde-fous : confirmation obligatoire, concurrence, amont en échec."""
    report.group("L. Robustesse — confirmation, concurrence, panne amont")
    refused = client.post(f"{ADMIN}/api/cache-clear", json={})
    report.check("L", "purge du cache refusée sans confirmation",
                 refused.status_code == 400, str(refused.status_code))
    confirmed = client.post(f"{ADMIN}/api/cache-clear", json={},
                            headers={"X-IcyLoom-Confirm": "1"})
    report.check("L", "purge du cache acceptée avec confirmation",
                 confirmed.status_code == 200 and confirmed.json()["removed"] >= 1,
                 confirmed.text[:80])

    body = {"model": "fake-model",
            "messages": [{"role": "user", "content": "concurrence huit threads"}]}
    client.post(f"{GATEWAY}/v1/chat/completions", json=body)  # amorçage
    before = _db("SELECT COUNT(*) FROM llm_events WHERE kind='hit'")[0][0]
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(
            lambda _: client.post(f"{GATEWAY}/v1/chat/completions", json=body), range(8)))
    hits = sum(1 for r in responses if r.headers.get(CACHE_HEADER) == "hit")
    after = _db("SELECT COUNT(*) FROM llm_events WHERE kind='hit'")[0][0]
    report.check("L", "8 requêtes concurrentes servies par le cache",
                 hits == 8 and after - before == 8,
                 f"hits={hits} événements +{after - before}")


def scenario_dashboard(report: Report, client: httpx.Client) -> None:
    """Le dashboard reflète l'état réel de la base et n'est pas figé."""
    report.group("M. Console — chiffres réels et page vivante")
    html = client.get(f"{ADMIN}/dashboard").text
    report.check("M", "la console est servie par l'API d'administration",
                 "/api/dashboard" in html and "IcyLoom" in html,
                 f"{len(html)} caractères")
    report.check("M", "la page ne contient aucune métrique figée",
                 "1284 appels" not in html and "gain 91" not in html.lower(),
                 "aucune valeur en dur détectée")

    before = client.get(f"{ADMIN}/api/dashboard").json()["kpi"]["calls"]
    body = {"model": "fake-model",
            "messages": [{"role": "user", "content": "le dashboard doit bouger"}]}
    client.post(f"{GATEWAY}/v1/chat/completions", json=body)
    client.post(f"{GATEWAY}/v1/chat/completions", json=body)
    after = client.get(f"{ADMIN}/api/dashboard").json()["kpi"]["calls"]
    report.check("M", "le compteur du dashboard suit l'activité réelle",
                 after == before + 2, f"{before} -> {after}")

    snapshot = client.get(f"{ADMIN}/api/dashboard").json()
    events = _db("SELECT COUNT(*) FROM llm_events")[0][0]
    sessions = _db("SELECT COUNT(*) FROM context_sessions")[0][0]
    report.check("M", "kpi.calls == événements réels en base",
                 snapshot["kpi"]["calls"] == events, f"{snapshot['kpi']['calls']} vs {events}")
    report.check("M", "sessions contexte == sessions réelles",
                 snapshot["kpi"]["sessions"] == sessions,
                 f"{snapshot['kpi']['sessions']} vs {sessions}")
    report.check("M", "tokens amoindris réels, jamais estimés",
                 snapshot["kpi"]["token_counter"].startswith("tiktoken"),
                 snapshot["kpi"]["token_counter"])


def scenario_agentic(report: Report, client: httpx.Client) -> None:
    """Un agent qui fait un vrai travail : chercher, ranger, demander, outiller.

    Chaque demande est différente ; la réponse de l'amont porte l'empreinte
    de la requête. On vérifie donc que chaque réponse correspond **exactement**
    à sa demande (aucun mélange possible entre prompts).
    """
    report.group("N. Travail agentique — 8 demandes réellement différentes")

    # Étape 1 — l'agent interroge d'abord son tiroir de contexte.
    found = asyncio.run(_mcp_http([
        ("ctx_store", {"source": "text",
                       "content": "SPEC_LOADER: le loader lit config.yaml au démarrage. "
                                  "SPEC_MARKER\n",
                       "session_id": "agent", "label": "spec-loader"}),
        ("ctx_search", {"query": "SPEC_LOADER", "limit": 5, "session_id": "agent"}),
    ]))["results"][1]
    report.check("N", "étape 1 — l'agent retrouve sa spec dans le tiroir",
                 int(found.get("count", 0)) >= 1, f"count={found.get('count')}")

    # Étape 2 — l'agent range un gros journal (fichier réel, pas du texte).
    log_path = PROJECT / "var" / "campaign_build.log"
    log_path.write_text(
        "build 2026-10-26T01:11:00Z version=3.12.10 port=8151 host=127.0.0.1\n"
        "unicode=café naïve — 漢字 — emoji 🚀\n"
        "étape 3 : normalisation des clés de cache\n"
        "étape 4 : mesure du gain\n", encoding="utf-8")
    stored = asyncio.run(_mcp_http([
        ("ctx_store", {"source": "file", "path": str(log_path),
                       "session_id": "agent", "label": "build.log"}),
    ]))["results"][0]
    report.check("N", "étape 2 — l'agent range un fichier réel",
                 bool(stored.get("ok")) and bool(stored.get("chunk_ids")),
                 f"ok={stored.get('ok')} fragments={len(stored.get('chunk_ids', []))}")
    back = asyncio.run(_mcp_http([
        ("ctx_get", {"chunk_id": str(stored.get("chunk_ids", [""])[0])})]))["results"][0]
    report.check("N", "étape 2 — relecture byte-exact du fichier",
                 back.get("content") == log_path.read_text(encoding="utf-8"),
                 "identique" if back.get("content") == log_path.read_text(encoding="utf-8")
                 else "diffère")

    # Étape 3 — huit demandes différentes, puis rejeu : la réponse doit être
    # celle de SA demande (empreinte de requête).
    digests: dict[str, str] = {}
    states: list[str] = []
    for name, prompt in AGENT_TASKS:
        body = {"model": "echo-model",
                "messages": [{"role": "user", "content": prompt}]}
        first = client.post(f"{GATEWAY}/v1/chat/completions", json=body)
        states.append(first.headers.get(CACHE_HEADER))
        digests[name] = echo_digest(body)
    replays: dict[str, str] = {}
    for name, prompt in AGENT_TASKS:
        body = {"model": "echo-model",
                "messages": [{"role": "user", "content": prompt}]}
        second = client.post(f"{GATEWAY}/v1/chat/completions", json=body)
        content = second.json()["choices"][0]["message"]["content"]
        replays[name] = content

    report.check("N", "étape 3 — 8 demandes = 8 MISS", states == ["miss"] * len(AGENT_TASKS),
                 str(states))
    mixed = [name for name, prompt in AGENT_TASKS
             if replays[name] != f"echo:{digests[name]}"]
    report.check("N", "étape 3 — chaque réponse est celle de SA demande",
                 not mixed, f"mélangées : {mixed}" if mixed else "aucun mélange")
    others = {name: set(digests.values()) - {digests[name]} for name in digests}
    leaked = [name for name in digests
              if any(other in replays[name] for other in others[name])]
    report.check("N", "étape 3 — aucune réponse ne contient l'empreinte d'une autre",
                 not leaked, f"fuites : {leaked}" if leaked else "aucune fuite")

    # Étape 4 — une itération d'agent qui affine : les deux versions sont
    # différentes, donc les deux doivent manquer le cache.
    v1 = {"model": "echo-model",
          "messages": [{"role": "user", "content": "Rédige le plan de migration, v1"}]}
    v2 = {"model": "echo-model",
          "messages": [{"role": "user", "content": "Rédige le plan de migration, v2"}]}
    s1 = client.post(f"{GATEWAY}/v1/chat/completions", json=v1).headers.get(CACHE_HEADER)
    s2 = client.post(f"{GATEWAY}/v1/chat/completions", json=v2).headers.get(CACHE_HEADER)
    report.check("N", "étape 4 — itération « v1 » puis « v2 » = 2 MISS (pas de sur-service)",
                 s1 == "miss" and s2 == "miss", f"{s1} / {s2}")
    r1 = client.post(f"{GATEWAY}/v1/chat/completions", json=v1).json()["choices"][0]["message"]["content"]
    r2 = client.post(f"{GATEWAY}/v1/chat/completions", json=v2).json()["choices"][0]["message"]["content"]
    report.check("N", "étape 4 — v1 et v2 ont deux réponses distinctes",
                 r1 == f"echo:{echo_digest(v1)}" and r2 == f"echo:{echo_digest(v2)}",
                 f"{r1} / {r2}")

    # Étape 5 — l'agent outille : 7 appels, 7 outils/args différents.
    out = asyncio.run(_mcp_http([("tool_proxy", {"calls": AGENT_TOOLS})]))["results"][0]
    results = out.get("results", [])
    report.check("N", "étape 5 — 7 appels d'outils différents acceptés",
                 len(results) == len(AGENT_TOOLS)
                 and all(r.get("ok") for r in results), f"{len(results)} résultat(s)")
    payloads = [json.dumps(r.get("result", r), sort_keys=True) for r in results]
    report.check("N", "étape 5 — chaque outil rend son propre résultat",
                 len(set(payloads)) == len(AGENT_TOOLS),
                 f"{len(set(payloads))} résultats distincts sur {len(AGENT_TOOLS)}")
    tool_names = sorted({r.get("tool") for r in results})
    report.check("N", "étape 5 — 6 outils descendants distincts appelés",
                 len(tool_names) == 6, str(tool_names))
    notes = [json.dumps(r.get("result", {}), sort_keys=True)
             for r in results if r.get("tool") == "read_notes"]
    report.check("N", "étape 5 — read_notes avec 2 ports rend 2 réponses distinctes",
                 len(notes) == 2 and notes[0] != notes[1], f"{len(notes)} réponses")
    entries = _db("SELECT COUNT(*), COUNT(DISTINCT key) FROM tool_cache")[0]
    report.check("N", "étape 5 — une entrée de cache par appel d'outil",
                 entries[1] >= len(AGENT_TOOLS), f"{entries[0]} entrées / {entries[1]} clés")

    # Étape 6 — l'agent referme : invariants intacts, aucune perte.
    verify = asyncio.run(_mcp_http([("ctx_verify", {"session_id": "agent"})]))["results"][0]
    check = asyncio.run(_mcp_http([("ctx_check", {"session_id": "agent"})]))["results"][0]
    report.check("N", "étape 6 — invariants du travail agentique intacts",
                 bool(verify.get("ok")) and verify.get("coverage") == 1.0,
                 f"coverage={verify.get('coverage')}")
    report.check("N", "étape 6 — aucune perte d'information",
                 bool(check.get("ok")) and check.get("roundtrip_ok"),
                 f"missing={check.get('invariants_missing')}")

    # Étape 7 — la comptabilité du workflow.
    counters = dict(_db("SELECT kind, COUNT(*) FROM llm_events GROUP BY kind"))
    saved = sum(int(r[0]) for r in _db("SELECT tokens_saved FROM ledger_entries"))
    report.check("N", "étape 7 — 8 misses amont + 11 hits comptés",
                 counters.get("miss", 0) >= 8 and counters.get("hit", 0) >= 11,
                 str(counters))
    report.check("N", "étape 7 — tokens économisés = 18 par hit",
                 saved == 18 * counters.get("hit", 0), f"ledger={saved}")


def scenario_no_crosstalk(report: Report, client: httpx.Client) -> None:
    """Aucun mélange : requêtes entrelacées, outils entrelacés, purge réelle."""
    report.group("O. Anti-mélange — prompts et outils entrelacés")
    prompts = [p for _, p in AGENT_TASKS[:4]]
    digests = [echo_digest({"model": "echo-model",
                             "messages": [{"role": "user", "content": p}]})
               for p in prompts]
    for p in prompts:
        client.post(f"{GATEWAY}/v1/chat/completions",
                    json={"model": "echo-model",
                          "messages": [{"role": "user", "content": p}]})

    order = [0, 1, 2, 3, 0, 2, 1, 3]
    answers: dict[int, str] = {}
    wrong = []
    for index in order:
        body = {"model": "echo-model",
                "messages": [{"role": "user", "content": prompts[index]}]}
        response = client.post(f"{GATEWAY}/v1/chat/completions", json=body)
        content = response.json()["choices"][0]["message"]["content"]
        answers.setdefault(index, content)
        if content != answers[index]:
            wrong.append(index)
    report.check("O", "8 requêtes entrelacées rendent toujours la bonne réponse",
                 not wrong, f"indices faux : {wrong}" if wrong else "0 mélange")
    report.check("O", "les 4 réponses sont distinctes",
                 len(set(answers.values())) == 4, f"{len(set(answers.values()))} distinctes")
    stray = [i for i, c in answers.items()
              if any(d != digests[i] and d in c for d in digests)]
    report.check("O", "aucune empreinte étrangère dans les réponses",
                 not stray, f"fuites : {stray}" if stray else "aucune fuite")

    # 4 outils différents, dans un ordre entrelacé : chacun doit rendre
    # SA réponse. (On ne répète pas le même appel : le garde anti-boucle
    # bloquerait legitimately une 3e répétition identique.)
    mixed_tools = [AGENT_TOOLS[0], AGENT_TOOLS[3], AGENT_TOOLS[5], AGENT_TOOLS[6]]
    out = asyncio.run(_mcp_http([("tool_proxy", {"calls": mixed_tools})]))["results"][0]
    payloads = [json.dumps(r.get("result", r), sort_keys=True) for r in out.get("results", [])]
    report.check("O", "4 appels d'outils entrelacés rendent la bonne réponse",
                 len(payloads) == 4 and len(set(payloads)) == 4
                 and all(r.get("ok") for r in out.get("results", [])),
                 f"{len(set(payloads))} résultats distincts | "
                 f"tools={[r.get('tool') for r in out.get('results', [])]} | "
                 f"cached={[r.get('cached') for r in out.get('results', [])]}")

    before = _db("SELECT COUNT(*) FROM cache_entries")[0][0]
    client.post(f"{ADMIN}/api/cache-clear", json={},
                headers={"X-IcyLoom-Confirm": "1"})
    after = _db("SELECT COUNT(*) FROM cache_entries")[0][0]
    replay = client.post(f"{GATEWAY}/v1/chat/completions",
                         json={"model": "echo-model",
                               "messages": [{"role": "user", "content": prompts[0]}]})
    report.check("O", "purge confirmée : le cache est réellement vidé",
                 after == 0 and before > 0, f"{before} -> {after} entrées")
    report.check("O", "après purge, le même prompt redevient un MISS",
                 replay.headers.get(CACHE_HEADER) == "miss",
                 replay.headers.get(CACHE_HEADER))


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------
def main() -> int:
    # La console Windows est souvent en cp1252 : on force l'UTF-8 pour que
    # les libellés accentués et les symboles de rapport s'affichent.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print("\033[1mCampagne de tests fonctionnels IcyLoom\033[0m")
    print(f"pile isolée : passerelle {GATEWAY_PORT} · mcp {MCP_HTTP_PORT}/{MCP_SSE_PORT}"
          f" · admin {ADMIN_PORT} · amont {UPSTREAM_PORT}")

    for suffix in ("", "-wal", "-shm"):
        stale = DB_PATH.with_name(DB_PATH.name + suffix)
        if stale.exists():
            stale.unlink()

    _write_config()
    upstream = EchoUpstream(port=UPSTREAM_PORT).start()
    log = LOG_PATH.open("w", encoding="utf-8")
    stack = subprocess.Popen(
        [PYTHON, "-m", "icyloom", "--config", str(CONFIG_PATH), "serve"],
        cwd=str(PROJECT), stdout=log, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    report = Report()
    try:
        if not _wait_http(f"{GATEWAY}/health") or not _wait_http(f"{ADMIN}/health"):
            print("\n\033[31mla pile n'a pas démarré\033[0m")
            print(LOG_PATH.read_text(encoding="utf-8", errors="replace")[-2000:])
            return 2
        if not _wait_port(MCP_HTTP_PORT) or not _wait_port(MCP_SSE_PORT):
            print("\n\033[31mles transports MCP n'ont pas démarré\033[0m")
            return 2

        with httpx.Client(timeout=30.0) as client:
            scenario_cache_variety(report, client)
            scenario_equivalences(report, client)
            scenario_no_false_hit(report, client)
            scenario_key_scope(report, client)
            scenario_ledger(report, client)
            scenario_context_docs(report)
            scenario_context_search(report)
            scenario_sessions(report)
            scenario_tools(report)
            scenario_formats(report, client)
            scenario_mcp_parity(report)
            scenario_robustness(report, client)
            scenario_dashboard(report, client)
            scenario_agentic(report, client)
            scenario_no_crosstalk(report, client)
    finally:
        stack.terminate()
        try:
            stack.wait(timeout=15)
        except Exception:
            stack.kill()
        upstream.stop()
        log.close()
        build_log = PROJECT / "var" / "campaign_build.log"
        if build_log.exists():
            build_log.unlink()

    print(report.summary())
    if report.failed:
        print("\nÉchecs :")
        for row in report.failed:
            print(f"  - [{row['group']}] {row['label']} :: {row['detail']}")

    REPORT_PATH.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total": len(report.rows),
        "failed": len(report.failed),
        "duration_s": round(time.time() - report.started, 1),
        "checks": report.rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nrapport JSON : {REPORT_PATH}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
