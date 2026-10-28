"""Test réel du relais d'outils avec le vrai serveur MCP ``fetch``.

Lancer (détaché, avec sortie dans un log) :

    cmd /d /s /c ".venv\\Scripts\\python.exe tests\\real_mcp_fetch.py < NUL > D:\\_audit\\real_fetch.log 2>&1"

Prérequis : ``uvx`` dans le PATH et accès Internet (https://example.com).
Le premier lancement télécharge ``mcp-server-fetch`` : il peut prendre une
minute. La base est temporaire : ``var/icyloom.db`` n'est jamais touchée.
Le script se termine toujours (délai global + sortie forcée).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import build_config  # noqa: E402

from icyloom.toolproxy import ToolProxy  # noqa: E402

URL = "https://example.com"
GLOBAL_TIMEOUT = 240.0
verdicts: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    verdicts.append(bool(ok))
    mark = "OK    " if ok else "ECHEC "
    print(f"[{mark}] {label}" + (f"  -> {detail}" if detail else ""), flush=True)


def content_of(answer: dict[str, Any]) -> list[str]:
    result = answer.get("result")
    return list(result.get("content") or []) if isinstance(result, dict) else []


def is_error(answer: dict[str, Any]) -> bool:
    result = answer.get("result")
    return (not answer.get("ok")) or (isinstance(result, dict) and bool(result.get("is_error")))


async def scenario(tmp: str) -> None:
    server = {"name": "fetch", "command": "uvx", "args": ["mcp-server-fetch@2026.10.10"],
              "cache_enabled": True, "ttl_seconds": 3600, "cache_tools": {"fetch": True}}
    cfg = build_config(Path(tmp), tool_proxy={
        "servers": [server], "connect_timeout_seconds": 120.0,
        "call_timeout_seconds": 60.0})
    proxy = ToolProxy(cfg)
    try:
        # 1. catalogue + inputSchema
        listed = await proxy.list_tools("fetch")
        names = [t["name"] for t in listed.get("tools", [])]
        check("list_tools répond", listed.get("ok") is True, f"outils: {names}")
        if not listed.get("ok"):
            print(listed, flush=True)
            return
        props = (proxy._schema_of("fetch", "fetch") or {}).get("properties", {})
        check("inputSchema de `fetch` mémorisé", bool(props),
              ", ".join(f"{k}:{v.get('type')}" for k, v in props.items()))

        # 2. MISS puis HIT sur le vrai serveur
        t0 = time.perf_counter()
        first = await proxy.call("fetch", "fetch", {"url": URL})
        d1 = (time.perf_counter() - t0) * 1000
        check("1er appel : vrai contenu, pas du cache",
              first.get("ok") and not first.get("cached") and not is_error(first)
              and any("Example Domain" in c for c in content_of(first)), f"{d1:.0f} ms")
        t0 = time.perf_counter()
        second = await proxy.call("fetch", "fetch", {"url": URL})
        d2 = (time.perf_counter() - t0) * 1000
        check("2e appel identique : servi du cache, même contenu",
              second.get("cached") is True and content_of(second) == content_of(first),
              f"{d2:.0f} ms (MISS {d1:.0f} ms)")

        # 3. canonisation guidée par l'inputSchema (entier déclaré)
        if (props.get("max_length") or {}).get("type") == "integer":
            await proxy.call("fetch", "fetch", {"url": URL, "max_length": 5000})
            merged = await proxy.call("fetch", "fetch", {"url": URL, "max_length": "5000"})
            check("max_length \"5000\" = 5000 (type integer déclaré)",
                  merged.get("cached") is True and merged.get("matched") == "canonical")
        else:
            print("[SAUTE ] canonisation : max_length n'est pas déclaré integer", flush=True)

        # 4. une URL différente n'est jamais fusionnée
        other = await proxy.call("fetch", "fetch", {"url": URL + "/?x=1"})
        check("URL différente : jamais servie depuis l'autre entrée",
              not (other.get("cached") and content_of(other) == content_of(first)
                   and other.get("matched") == "canonical"))

        # 5. une erreur réelle n'est pas mémorisée
        bad = {"url": "https://invalid.invalid/"}
        e1 = await proxy.call("fetch", "fetch", bad)
        e2 = await proxy.call("fetch", "fetch", bad)
        check("erreur réelle (hôte inexistant) : 1er appel en erreur", is_error(e1))
        check("erreur réelle : jamais rejouée du cache", not e2.get("cached"))
    finally:
        try:
            await asyncio.wait_for(proxy.close(), 15)
        except Exception as exc:  # fermeture best effort
            print(f"(fermeture des sessions : {type(exc).__name__})", flush=True)


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        try:
            await asyncio.wait_for(scenario(tmp), GLOBAL_TIMEOUT)
        except asyncio.TimeoutError:
            check(f"terminé avant {GLOBAL_TIMEOUT:.0f} s", False,
                  "relance : le 1er lancement télécharge le serveur")
        except Exception as exc:
            check("exécution sans exception", False, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
    good = sum(verdicts)
    print(f"\n{good}/{len(verdicts)} vérifications OK", flush=True)
    os._exit(0 if verdicts and all(verdicts) else 1)
