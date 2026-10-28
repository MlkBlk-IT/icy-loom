"""Jeu de démonstration du dashboard IcyLoom.

Démontre la console avec des données **réelles** : le trafic passe par la
passerelle (8150) vers un faux amont, le contexte passe par les outils MCP
(8151) et le relais d'outils appelle un vrai serveur MCP descendant.

Étapes :
  1. écrit ``var/demo.yaml`` (base isolée ``var/demo.db``) ;
  2. démarre le faux amont (8154) et la pile IcyLoom (8150/8151/8152/8153) ;
  3. joue du trafic LLM (miss + hit), range du contexte, appelle des outils ;
  4. répartit les événements mesurés sur les dernières heures/jours pour que
     les courbes du dashboard aient une profondeur temporelle ;
  5. laisse la console ouverte.

Lancement : ``.venv\\Scripts\\python.exe tools\\seed_demo.py``
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

PROJECT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
sys.path.insert(0, str(PROJECT / "tests"))
sys.path.insert(0, str(PROJECT / "tools"))

from fake_upstream import FakeUpstream  # noqa: E402

GATEWAY = "http://127.0.0.1:8150"
ADMIN = "http://127.0.0.1:8153"
MCP_HTTP = "http://127.0.0.1:8151/mcp"
FAKE = "http://127.0.0.1:8154"

PROMPTS = [
    "Quelle est la latence du cache sur le port 8150 ?",
    "Explique le rôle de la passerelle HTTP IcyLoom.",
    "Résume la politique de compression lossless du tiroir.",
    "Comment le ledger calcule-t-il le gain en pourcentage ?",
    "Quelle est la différence entre miss et hit dans le journal ?",
    "Décris le rôle du circuit breaker et son réarmement.",
    "Que fait l'outil ctx_verify dans IcyLoom ?",
    "Comment le tiroir garde-t-il les invariants (ports, dates) ?",
    "Pourquoi les tokens amont ne sont-ils jamais estimés ?",
    "Que mesure la clé canonique du cache ?",
    "Comment fonctionne la bascule d'amont en cas d'échec ?",
    "Quel est le rôle du garde anti-boucle du relais d'outils ?",
]
CLIENTS = ["ide-vscode", "cli-openai", "agent-mavis"]
MODELS = ["fake-model", "fake-model-mini"]

CONFIG_TEMPLATE = """# Généré par tools/seed_demo.py — configuration de démonstration isolée.
server:
  name: icyloom-demo
  host: 127.0.0.1
  remote_access_token: null

ports:
  gateway_http: 8150
  mcp_http: 8151
  mcp_sse: 8152
  admin: 8153

database:
  path: demo.db
  journal_mode: WAL
  busy_timeout_ms: 5000
  synchronous: NORMAL

cache:
  ttl_seconds: 604800
  max_entries: 50000
  semantic_enabled: false

compression:
  enabled: false
  profile: strict
  auto_store_threshold: 500
  max_anchor_chars: 6000
  max_invariants: 40

context:
  session_ttl_days: 30
  redact_enabled: true
  rate_limit_per_minute: 1200
  allowed_dirs: []
  audit_enabled: true
  audit_retention_days: 30
  sandbox_enabled: true
  sandbox_timeout_seconds: 30.0

breaker:
  failure_threshold: 3
  reset_seconds: 30.0
  half_open_probes: 1

ledger:
  enabled: true
  retention_days: 90

tool_proxy:
  default_ttl_seconds: 3600
  loop_window_seconds: 300
  loop_warn_after: 3
  loop_block_after: 5
  loop_max_cycle: 3
  max_result_bytes: 1048576
  connect_timeout_seconds: 20.0
  call_timeout_seconds: 60.0
  servers:
    - name: demo
      command: "{python}"
      args: ["{server}"]
      cache_enabled: true
      ttl_seconds: 3600
      # allowlist stricte : seuls ces outils (lecture seule) sont cachables.
      # Les outils d'écriture ne le sont jamais, meme s'ils s'appellent
      # "read" ou "get".
      cache_tools:
        list_metrics: true
        read_notes: true
        read_file: true
        list_dir: true
        search_code: true
        git_log: true

upstreams:
  - name: fake
    base_url: http://127.0.0.1:8154/v1
    priority: 10
    kind: openai
    api_key_env: ""
    timeout_seconds: 30.0
    sticky: true
"""


def write_config() -> Path:
    """Écrit la configuration de démonstration dans var/."""
    target = PROJECT / "var" / "demo.yaml"
    target.write_text(
        CONFIG_TEMPLATE.format(
            python=PYTHON.replace("\\", "\\\\"),
            server=str(PROJECT / "tools" / "fake_mcp_server.py").replace("\\", "\\\\"),
        ),
        encoding="utf-8",
    )
    return target


def clear_database() -> None:
    """Part d'une base vide (sinon les hits attendus deviennent des hits)."""
    for suffix in ("", "-wal", "-shm"):
        stale = PROJECT / "var" / f"demo.db{suffix}"
        if stale.exists():
            stale.unlink()


async def mcp_calls() -> None:
    """Range du contexte et appelle des outils via le serveur MCP."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(MCP_HTTP) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            long_text = (
                "Notes de conception IcyLoom. "
                "La passerelle écoute sur le port 8150 et relaie les appels OpenAI et Anthropic. "
                "Le serveur MCP expose 13 outils sur les ports 8151 (streamable-HTTP) et 8152 (SSE). "
                "L'API d'administration occupe le port 8153 et sert la console web. "
                "La base SQLite unique vit dans var/icyloom.db en mode WAL. "
                "La compression est désactivée par défaut et le profil strict est lossless. "
                "Les tokens amont sont toujours réels, jamais estimés. "
                "Le seuil d'exploitation retenu est un gain de 35 pour cent. "
            ) * 3
            for index, session_id in enumerate(("demo-alpha", "demo-beta", "demo-gamma")):
                result = await session.call_tool("ctx_store", {
                    "source": "text",
                    "content": long_text,
                    "session_id": session_id,
                    "label": f"note-{index}",
                })
                print(f"  ctx_store[{session_id}] : "
                      f"{'ok' if not result.isError else 'ERREUR'}")

            batch = [
                {"server": "demo", "tool": "list_metrics", "args": {}},
                {"server": "demo", "tool": "read_notes", "args": {"port": "8150"}},
                {"server": "demo", "tool": "list_metrics", "args": {}},
                {"server": "demo", "tool": "read_notes", "args": {"port": "8153"}},
            ]
            result = await session.call_tool("tool_proxy", {"calls": batch})
            payload = None
            for block in result.content:
                if getattr(block, "text", None):
                    payload = block.text
            print(f"  tool_proxy : {(payload or '')[:120]}")


def llm_traffic(count: int = 72) -> tuple[int, int]:
    """Trafic LLM réel : un premier passage en miss, puis des hits."""
    client = httpx.Client(timeout=30.0)
    hits = misses = 0
    for index in range(count):
        prompt = PROMPTS[index % len(PROMPTS)]
        # corps **identique** pour un même prompt : le second passage est un hit
        body = {
            "model": MODELS[index % len(MODELS)],
            "messages": [{"role": "user", "content": prompt}],
        }
        response = client.post(f"{GATEWAY}/v1/chat/completions", json=body,
                               headers={"x-client": CLIENTS[index % len(CLIENTS)]})
        state = response.headers.get("X-IcyLoom-Cache")
        if state == "hit":
            hits += 1
        elif state == "miss":
            misses += 1
    client.close()
    return hits, misses


def traffic_loop(stop: "threading.Event") -> None:
    """Trafic **continu** : la console doit vivre, pas rester figée.

    Un fil dbackground poste en boucle des requêtes (une nouvelle toutes les
    ~250 ms, plus une requête « fraîche » toutes les 8 qui est un miss), ce
    qui fait grimper les compteurs, anime le débit req/s et fait pousser les
    courbes du dashboard en direct.
    """
    client = httpx.Client(timeout=20.0)
    index = 0
    while not stop.is_set():
        fresh = index % 8 == 0
        prompt = (f"question neuve #{index} sur le port 8153" if fresh
                  else PROMPTS[index % len(PROMPTS)])
        body = {
            "model": MODELS[index % len(MODELS)],
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            client.post(f"{GATEWAY}/v1/chat/completions", json=body,
                        headers={"x-client": CLIENTS[index % len(CLIENTS)]})
        except Exception:
            pass
        index += 1
        stop.wait(0.25)
    client.close()


def tool_loop(stop: "threading.Event") -> None:
    """Appels d'outils périodiques : la section Outils reste vivante."""
    while not stop.is_set():
        stop.wait(9.0)
        if stop.is_set():
            break
        try:
            asyncio.run(mcp_calls())
        except Exception:
            pass


def spread_in_time() -> None:
    """Répartit les événements mesurés sur les dernières heures et les
    derniers jours : les valeurs (tokens, latences) restent réelles, seule
    l'horodatage est décalé pour donner de la profondeur aux courbes.

    La répartition elle-même vit dans ``tools/spread_demo.py`` : elle est
    partageable avec la démo déjà en service (relance sans rejouer le trafic).
    """
    from spread_demo import spread

    counts = spread()
    print("horodatage        : " + ", ".join(f"{k} {v}" for k, v in counts.items()))


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Démonstration IcyLoom")
    parser.add_argument("--only-traffic", action="store_true",
                        help="ne pas démarrer la pile : rejouer seulement le trafic "
                             "continu sur une instance déjà en service")
    parser.add_argument("--keep-db", action="store_true",
                        help="ne pas vider la base de démonstration")
    args = parser.parse_args()

    if not args.keep_db:
        clear_database()
    config = write_config()
    print(f"configuration      : {config}")

    upstream = None
    stack = None
    if args.only_traffic:
        print("mode              : trafic seul (pile déjà en service)")
    else:
        upstream = FakeUpstream(port=8154)
        upstream.start()
        print(f"faux amont        : {FAKE}")

        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        stack = subprocess.Popen(
            [PYTHON, "-m", "icyloom", "--config", str(config), "serve"],
            cwd=str(PROJECT), env=env,
            stdout=open(PROJECT / "var" / "demo.serve.out.log", "w", encoding="utf-8"),
            stderr=open(PROJECT / "var" / "demo.serve.err.log", "w", encoding="utf-8"),
        )
    try:
        deadline = time.time() + 60
        up = False
        while time.time() < deadline:
            try:
                if httpx.get(f"{GATEWAY}/health", timeout=2).status_code == 200:
                    up = True
                    break
            except Exception:
                time.sleep(0.5)
        if not up:
            print("ECHEC : la pile IcyLoom n'a pas démarré")
            return 1
        print("pile IcyLoom      : 8150 / 8151 / 8152 / 8153")

        if not args.only_traffic:
            hits, misses = llm_traffic()
            print(f"trafic LLM        : {misses} miss, {hits} hit")

            asyncio.run(mcp_calls())

        # trafic continu : la console reste vivante (compteurs, débit, courbes)
        stop = threading.Event()
        workers = [threading.Thread(target=traffic_loop, args=(stop,), daemon=True),
                   threading.Thread(target=tool_loop, args=(stop,), daemon=True)]
        for worker in workers:
            worker.start()
        print("trafic continu    : actif (~4 req/s, outils toutes les 9 s)")

        if not args.keep_db and not args.only_traffic:
            spread_in_time()
            print("horodatage        : événements répartis sur 14 jours")

        data = httpx.get(f"{ADMIN}/api/dashboard", timeout=10).json()
        k = data["kpi"]
        print()
        print(f"console           : {ADMIN}/dashboard")
        print(f"  appels {k['calls']} · cache {k['hit_rate_pct']} %")
        print(f"  payé {k['tokens_paid']} · économisé {k['tokens_saved']} · gain {k['gain_pct']} %")
        print(f"  latence p95 {k['latency_p95']} ms · entrées cache {k['cache_entries']}")
        print(f"  sessions {k['sessions']} · chunks {k['chunks']}")
        print()
        print("Ctrl+C pour arrêter la démonstration.")
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        print("\narrêt")
    finally:
        stop.set()
        if stack is not None:
            stack.terminate()
            try:
                stack.wait(timeout=15)
            except Exception:
                stack.kill()
        if upstream is not None:
            upstream.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
