"""Preuve fonctionnelle des optimisations du relais d'outils.

1. un appel **presque identique** (mêmes valeurs, écriture différente) doit
   être servi par le cache (clé canonique guidée par le schéma de l'outil) ;
2. deux valeurs **réellement différentes** ne doivent jamais être confondues ;
3. la session descendante doit être **réutilisée** (un seul processus) ;
4. le catalogue d'outils doit être **mis en cache** et **interrogeable**.

Lancement : .\\.venv\\Scripts\\python.exe tools\\proof_tools.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml

PROJECT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
GATEWAY_PORT = 8180
MCP_HTTP_PORT = 8181
MCP_SSE_PORT = 8182
ADMIN_PORT = 8183
UPSTREAM_PORT = 8184
MCP_HTTP = f"http://127.0.0.1:{MCP_HTTP_PORT}/mcp"
DB_PATH = PROJECT / "var" / "proof_tools.db"
CONFIG_PATH = PROJECT / "var" / "proof_tools.yaml"

OK = "\033[32m✔\033[0m"
KO = "\033[31m✘\033[0m"
results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((bool(ok), label, detail))
    print(f"  {OK if ok else KO} {label}" + (f" — {detail}" if detail else ""))
    return bool(ok)


def write_config() -> None:
    payload = {
        "server": {"name": "icyloom-proof", "host": "127.0.0.1",
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
        "tool_proxy": {
            "default_ttl_seconds": 3600, "loop_window_seconds": 300,
            "loop_warn_after": 3, "loop_block_after": 5, "loop_max_cycle": 3,
            "max_result_bytes": 1048576, "connect_timeout_seconds": 20.0,
            "call_timeout_seconds": 60.0,
            "servers": [{"name": "demo", "command": PYTHON,
                         "args": [str(PROJECT / "tools" / "fake_mcp_server.py")],
                         "cache_enabled": True, "ttl_seconds": 3600,
                         "cache_tools": {"list_metrics": True, "read_notes": True,
                                         "read_file": True, "list_dir": True,
                                         "search_code": True, "git_log": True}}],
        },
        "ledger": {"enabled": True, "retention_days": 90},
        "breaker": {"failure_threshold": 3, "reset_seconds": 30.0,
                    "half_open_probes": 1},
        "upstreams": [{"name": "fake",
                       "base_url": f"http://127.0.0.1:{UPSTREAM_PORT}/v1",
                       "priority": 10, "kind": "openai", "api_key_env": "",
                       "timeout_seconds": 30.0, "sticky": True}],
    }
    CONFIG_PATH.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
                           encoding="utf-8")


async def _call(calls: list[tuple[str, dict]], port: int = MCP_HTTP_PORT) -> list:
    """Appels MCP dans UNE seule session, avec la durée de chaque appel."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    out: list = []
    async with streamablehttp_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for name, args in calls:
                started = time.perf_counter()
                result = await session.call_tool(name, args)
                elapsed = int((time.perf_counter() - started) * 1000)
                payload = result.structuredContent or {}
                data = payload.get("result")
                if data is None:
                    for block in result.content or []:
                        data = json.loads(getattr(block, "text", "") or "{}")
                        break
                if isinstance(data, list) and len(data) == 1:
                    data = data[0]
                if isinstance(data, dict):
                    data = dict(data)
                    data["_elapsed_ms"] = elapsed
                out.append(data)
    return out


def tool_call(server: str, tool: str, args: dict) -> dict:
    return {"server": server, "tool": tool, "args": args}


def main() -> int:
    for suffix in ("", "-wal", "-shm"):
        stale = DB_PATH.with_name(DB_PATH.name + suffix)
        if stale.exists():
            stale.unlink()
    write_config()

    log = (PROJECT / "var" / "proof_tools.serve.log").open("w", encoding="utf-8")
    stack = subprocess.Popen(
        [PYTHON, "-m", "icyloom", "--config", str(CONFIG_PATH), "serve"],
        cwd=str(PROJECT), stdout=log, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    try:
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{MCP_HTTP_PORT}/health",
                             timeout=2).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.4)

        print("\n\033[1m1. Reconnaissance d'un appel « presque identique »\033[0m")
        # a) paramètre typé : l'entier 10 et la chaîne "10" sont la même
        #    valeur pour le schéma de l'outil -> un seul appel amont.
        typed = tool_call("demo", "search_code", {"pattern": "cache", "limit": 10})
        typed_str = tool_call("demo", "search_code", {"pattern": "cache", "limit": "10"})
        typed_other = tool_call("demo", "search_code", {"pattern": "cache", "limit": 99})
        res = asyncio.run(_call([("tool_proxy", {"calls": [typed]}),
                                  ("tool_proxy", {"calls": [typed_str]}),
                                  ("tool_proxy", {"calls": [typed_other]})]))
        first, second, third = res[0]["results"][0], res[1]["results"][0], res[2]["results"][0]
        check(first.get("cached") is not True, "1er appel : réellement servi par l'amont",
              f"cached={first.get('cached')} latence={first.get('latency_ms')} ms")
        check(second.get("cached") is True and second.get("matched") == "canonical",
              "entier 10 et chaîne \"10\" : un seul appel amont",
              f"cached={second.get('cached')} matched={second.get('matched')}")
        check(third.get("cached") is not True, "valeur 99 : PAS de confusion",
              f"cached={third.get('cached')}")

        # b) paramètre texte : le repli est conservateur, une chaîne
        #    «  8150  » n'est PAS la même valeur que « 8150 ».
        base = tool_call("demo", "read_notes", {"port": "8150"})
        near = tool_call("demo", "read_notes", {"port": " 8150 "})
        other = tool_call("demo", "read_notes", {"port": "8153"})
        res2 = asyncio.run(_call([("tool_proxy", {"calls": [base]}),
                                   ("tool_proxy", {"calls": [near]}),
                                   ("tool_proxy", {"calls": [other]})]))
        r_base, r_near, r_other = (res2[0]["results"][0], res2[1]["results"][0],
                                   res2[2]["results"][0])
        check(r_base.get("cached") is not True, "chaîne : 1er appel servi par l'amont",
              f"cached={r_base.get('cached')}")
        check(r_near.get("cached") is not True,
              "chaîne : «  8150  » n'est PAS fusionné avec « 8150 » (conservateur)",
              f"cached={r_near.get('cached')}")
        check(r_other.get("cached") is not True, "port différent : PAS de confusion",
              f"cached={r_other.get('cached')}")

        print("\n\033[1m2. La session descendante est réutilisée\033[0m")
        metrics_call = [("tool_proxy", {"calls": [
            tool_call("demo", "list_metrics", {})]})]
        both = asyncio.run(_call(metrics_call + metrics_call))
        first_call, second_call = both[0], both[1]
        lat1 = first_call["_elapsed_ms"]
        lat2 = second_call["_elapsed_ms"]
        check(first_call["results"][0].get("cached") is not True,
              "1er appel outil : pas de cache", f"{lat1} ms")
        check(second_call["results"][0].get("cached") is True,
              "2e appel outil identique : servi par le cache", f"{lat2} ms")
        check(lat2 < lat1, "le 2e appel est plus rapide que le 1er",
              f"{lat1} ms puis {lat2} ms")

        print("\n\033[1m3. Catalogue d'outils : cache + recherche\033[0m")
        info = asyncio.run(_call([("proxy_info", {"what": "tools", "server": "demo"}),
                                  ("proxy_info", {"what": "tools", "server": "demo"})]))
        listed = info[0]
        listed2 = info[1]
        check(listed.get("ok") is True and listed.get("count", 0) >= 6,
              "catalogue listé", f"{listed.get('count')} outils")
        check(listed2.get("cached") is True, "2e listing servi par le cache du catalogue",
              f"cached={listed2.get('cached')}")
        found = asyncio.run(_call([("proxy_info", {"what": "search", "query": "git"})]))
        matches = found[0].get("matches", [])
        check(len(matches) >= 1 and any("git" in m["tool"] for m in matches),
              "recherche « git » trouve l'outil", str([m["tool"] for m in matches]))
        found2 = asyncio.run(_call([("proxy_info", {"what": "search", "query": "fichier"})]))
        check(len(found2[0].get("matches", [])) >= 1,
              "recherche par description (« fichier »)",
              str([m["tool"] for m in found2[0].get("matches", [])]))

        print("\n\033[1m4. L'état du cache d'outils\033[0m")
        stats = asyncio.run(_call([("proxy_info", {"what": "stats"})]))[0]["stats"]
        cache_stats = stats.get("cache", {})
        check(int(cache_stats.get("hits", 0)) >= 2,
              "compteur de réutilisations > 0", json.dumps(cache_stats))
        import sqlite3

        con = sqlite3.connect(str(DB_PATH))
        print("   tool_cache :", con.execute(
            "SELECT key, server, tool, hits, canon_key FROM tool_cache").fetchall())
        print("   tool_events :", con.execute(
            "SELECT kind, tool, detail FROM tool_events").fetchall())
        con.close()
    finally:
        stack.terminate()
        try:
            stack.wait(timeout=15)
        except Exception:
            stack.kill()
        log.close()
        for path in (DB_PATH, DB_PATH.with_name(DB_PATH.name + "-wal"),
                     DB_PATH.with_name(DB_PATH.name + "-shm"), CONFIG_PATH):
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        try:
            (PROJECT / "var" / "proof_tools.serve.log").unlink()
        except OSError:
            pass

    failed = [r for r in results if not r[0]]
    print(f"\n{'-' * 62}\n{len(results) - len(failed)}/{len(results)} vérifications "
          f"réussies ({len(failed)} échec(s))")
    for _ok, label, detail in failed:
        print(f"  - {label} :: {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
