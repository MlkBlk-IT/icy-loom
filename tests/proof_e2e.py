"""Preuve de bout en bout sur les ports réels d'IcyLoom.

Démarre :
  * un faux amont OpenAI-compatible sur **8154** ;
  * la pile IcyLoom (passerelle 8150, MCP streamable-HTTP 8151, SSE 8152,
    admin 8153) avec la configuration du projet.

Puis contrôle : santé des 4 services, liste des outils sur les 3 transports
MCP, et mesure de latence (direct / MISS / HIT) sur 200 requêtes.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

PROJECT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
sys.path.insert(0, str(PROJECT / "tests"))

from fake_upstream import FakeUpstream  # noqa: E402

N = 200
GATEWAY = "http://127.0.0.1:8150"
FAKE = "http://127.0.0.1:8154"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(pct / 100 * len(ordered))) - 1))
    return ordered[index]


def _report(name: str, values: list[float]) -> dict[str, float]:
    return {
        "serie": name,
        "n": len(values),
        "p50_ms": round(_percentile(values, 50), 3),
        "p95_ms": round(_percentile(values, 95), 3),
        "max_ms": round(max(values), 3) if values else 0.0,
        "moyenne_ms": round(statistics.fmean(values), 3) if values else 0.0,
    }


def _body(index: int) -> dict:
    return {"model": "fake-model",
            "messages": [{"role": "user", "content": f"question {index} sur le port 8151"}]}


def main() -> int:
    failures: list[str] = []

    # La preuve doit partir d'une base vide : sinon les corps déjà connus
    # sont servis par le cache et le test « MISS » échoue à tort.
    for suffix in ("", "-wal", "-shm"):
        stale = PROJECT / "var" / f"icyloom-e2e.db{suffix}"
        if stale.exists():
            stale.unlink()

    upstream = FakeUpstream(port=8154, delay_seconds=0.0)
    upstream.start()
    print(f"faux amont        : {FAKE} (pid dans le thread de test)")

    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    (PROJECT / "var").mkdir(parents=True, exist_ok=True)
    stack = subprocess.Popen(
        [PYTHON, "-m", "icyloom", "--config", str(PROJECT / "tests" / "icyloom.e2e.yaml"),
         "serve"],
        cwd=str(PROJECT), env=env,
        stdout=open(PROJECT / "var" / "serve.out.log", "w", encoding="utf-8"),
        stderr=open(PROJECT / "var" / "serve.err.log", "w", encoding="utf-8"),
    )
    try:
        # --- démarrage -------------------------------------------------
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
            failures.append("la passerelle 8150 n'a pas démarré")
            raise SystemExit(1)
        print("pile IcyLoom      : passerelle 8150, MCP 8151, SSE 8152, admin 8153")

        # --- santé des 4 services -------------------------------------
        checks = {
            "gateway 8150 /health": (f"{GATEWAY}/health", 200),
            "admin 8153 /health": ("http://127.0.0.1:8153/health", 200),
            "gateway 8150 /v1/models": (f"{GATEWAY}/v1/models", 200),
        }
        for label, (url, expected) in checks.items():
            try:
                code = httpx.get(url, timeout=5).status_code
            except Exception as exc:
                code = f"erreur {type(exc).__name__}"
            print(f"  {label:<34} -> {code}")
            if code != expected:
                failures.append(f"{label} -> {code}")

        # --- outils sur les 3 transports MCP ---------------------------
        expected_tools = {
            "cache", "tool_proxy", "proxy_info", "tool_cache_clear",
            "ctx_store", "ctx_get", "ctx_search", "ctx_verify", "ctx_check",
            "ctx_profile", "ctx_session", "ctx_admin", "ledger",
        }
        from mcp import ClientSession
        from mcp.client.sse import sse_client
        from mcp.client.streamable_http import streamablehttp_client

        async def http_tools() -> list[str]:
            async with streamablehttp_client("http://127.0.0.1:8151/mcp") as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    return [t.name for t in (await session.list_tools()).tools]

        async def sse_tools() -> list[str]:
            async with sse_client("http://127.0.0.1:8152/sse") as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    return [t.name for t in (await session.list_tools()).tools]

        for label, coro in (("MCP streamable-HTTP 8151", http_tools),
                            ("MCP SSE 8152", sse_tools)):
            try:
                names = asyncio.run(coro())
                ok = set(names) == expected_tools
                print(f"  {label:<34} -> {len(names)} outils "
                      f"({'identiques' if ok else 'DIFFERENTS'})")
                if not ok:
                    failures.append(f"{label}: {sorted(set(names) ^ expected_tools)}")
            except Exception as exc:
                print(f"  {label:<34} -> ERREUR {type(exc).__name__}: {exc}")
                failures.append(f"{label}: {exc}")

        # --- latence : direct / MISS / HIT -----------------------------
        client = httpx.Client(timeout=30.0)
        direct: list[float] = []
        miss: list[float] = []
        hit: list[float] = []
        hit_body = _body(0)
        for index in range(N):
            started = time.perf_counter()
            client.post(f"{FAKE}/v1/chat/completions", json=_body(index))
            direct.append((time.perf_counter() - started) * 1000)

            started = time.perf_counter()
            response = client.post(f"{GATEWAY}/v1/chat/completions", json=_body(index))
            miss.append((time.perf_counter() - started) * 1000)
            if response.headers.get("X-IcyLoom-Cache") != "miss":
                failures.append(f"requête #{index} : pas un MISS")

            started = time.perf_counter()
            response = client.post(f"{GATEWAY}/v1/chat/completions", json=hit_body)
            hit.append((time.perf_counter() - started) * 1000)
            if response.headers.get("X-IcyLoom-Cache") != "hit":
                failures.append(f"requête #{index} : pas un HIT")

        report = [_report("direct (faux amont)", direct),
                  _report("gateway MISS", miss),
                  _report("gateway HIT", hit)]
        print()
        print(f"{'série':<26}{'n':>5}{'p50 ms':>10}{'p95 ms':>10}{'max ms':>10}")
        for row in report:
            print(f"{row['serie']:<26}{row['n']:>5}{row['p50_ms']:>10}"
                  f"{row['p95_ms']:>10}{row['max_ms']:>10}")
        overhead_miss = report[1]["p50_ms"] - report[0]["p50_ms"]
        overhead_hit = report[2]["p50_ms"] - report[0]["p50_ms"]
        print(f"\noverhead gateway p50 : MISS {overhead_miss:+.3f} ms | "
              f"HIT {overhead_hit:+.3f} ms")

        # --- état économique -------------------------------------------
        metrics = httpx.get(f"{GATEWAY}/api/metrics", timeout=5).json()
        ledger = httpx.get("http://127.0.0.1:8153/api/ledger", timeout=5).json()
        print(f"\ncache  : {metrics['calls']} appels, {metrics['hits']} hits, "
              f"hit rate {metrics['hit_rate_pct']} %")
        print(f"tokens : réels {metrics['tokens_real']}, économisés "
              f"{metrics['tokens_saved']}, {metrics['saved_pct']} %")
        print(f"ledger : payé {ledger['tokens_paid']}, économisé {ledger['tokens_saved']}, "
              f"gain {ledger['gain_pct']} % (seuil {ledger['threshold_pct']} %, "
              f"atteint={ledger['threshold_met']})")
        client.close()
    finally:
        stack.terminate()
        try:
            stack.wait(timeout=15)
        except Exception:
            stack.kill()
        upstream.stop()

    print()
    if failures:
        print("ECHECS :")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("TOUTES LES PREUVES DE BOUT EN BOUT SONT VERTES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
