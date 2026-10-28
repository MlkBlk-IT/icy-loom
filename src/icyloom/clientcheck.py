"""Rendre l'usage d'IcyLoom simple : config client + auto-test.

Deux services :

``client_config()``
    Le bloc de configuration à coller dans un client MCP (Claude Code,
    Claude Desktop, Cursor, Codex, Windsurf) — chemin de l'interpréteur
    déjà résolu, donc copié-collé tel quel.

``run_checks()``
    Un auto-test qui **joue le rôle du client** : il démarre le serveur
    comme le ferait un client, puis appelle réellement les outils. C'est la
    réponse à « est-ce que je peux vraiment les utiliser ? ».
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

#: Transports testés par l'auto-test, dans l'ordre d'intérêt.
TRANSPORTS = ("stdio", "http", "sse")


# --------------------------------------------------------------------------
# configuration client
# --------------------------------------------------------------------------
def client_config(config_path: str | None = None,
                  transport: str = "stdio") -> dict[str, Any]:
    """Bloc ``mcpServers`` prêt à coller."""
    root = Path(__file__).resolve().parents[2]
    python = Path(sys.executable)
    args = ["-m", "icyloom"]
    if config_path:
        args += ["--config", config_path]
    args += ["mcp", "--transport", transport]
    return {"mcpServers": {"icyloom": {"command": str(python), "args": args}}}


def _client_file(target: str) -> Path:
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    if target == "claude":                 # Claude Desktop
        return appdata / "Claude" / "claude_desktop_config.json"
    if target == "claude-code":            # Claude Code
        return home / ".claude.json"
    if target == "cursor":
        return home / ".cursor" / "mcp.json"
    if target == "windsurf":
        return home / ".codeium" / "windsurf" / "mcp_config.json"
    if target == "codex":
        return home / ".codex" / "config.toml"
    raise ValueError(f"cible inconnue : {target}")


def install_client_config(target: str = "cursor", config_path: str | None = None,
                          transport: str = "stdio") -> Path:
    """Écrit (ou met à jour) la configuration du client choisi."""
    path = _client_file(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    block = client_config(config_path, transport)["mcpServers"]["icyloom"]

    if path.suffix == ".toml":             # Codex : format TOML
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = [line for line in existing.splitlines()
                 if not line.startswith("command") and not line.startswith("args")
                 and "[mcp_servers.icyloom" not in line]
        quoted = ", ".join(f'"{a}"' for a in block["args"])
        lines += ["[mcp_servers.icyloom]",
                  f'command = "{block["command"]}"',
                  f"args = [{quoted}]"]
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return path

    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    servers = data.setdefault("mcpServers", {})
    servers["icyloom"] = block
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# auto-test « comme un client »
# --------------------------------------------------------------------------
async def _exercise(session: Any, checks: list[tuple[bool, str]]) -> dict[str, Any]:
    listed = await session.list_tools()
    names = sorted(t.name for t in listed.tools)
    checks.append((len(names) == 13, f"13 outils exposés ({len(names)})"))

    marker = f"CLIENT_CHECK_{names[0].upper()}"
    store = _payload(await session.call_tool(
        "ctx_store", {"source": "text", "session_id": "client-check",
                       "label": "check", "content": f"{marker} contenu de test\n"}))
    checks.append((bool(store.get("ok")), "ctx_store accepte un texte"))

    search = _payload(await session.call_tool(
        "ctx_search", {"query": marker, "limit": 5, "session_id": "client-check"}))
    checks.append((int(search.get("count", 0)) >= 1,
                   f"ctx_search retrouve le fragment (count={search.get('count')})"))

    verify = _payload(await session.call_tool("ctx_verify",
                                              {"session_id": "client-check"}))
    checks.append((bool(verify.get("ok")), "ctx_verify valide la session"))

    for name, args, label in (
            ("cache", {"action": "stats"}, "cache(stats) répond"),
            ("ledger", {"view": "summary"}, "ledger(summary) répond"),
            ("proxy_info", {"what": "servers"}, "proxy_info(servers) répond")):
        payload = _payload(await session.call_tool(name, args))
        checks.append((bool(payload.get("ok")), label))

    return {"tools": names}


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
    return data if isinstance(data, dict) else {"ok": bool(data)}


async def _check_stdio(config_path: str | None,
                        checks: list[tuple[bool, str]]) -> dict[str, Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    args = ["-m", "icyloom"]
    if config_path:
        args += ["--config", config_path]
    args += ["mcp", "--transport", "stdio"]
    params = StdioServerParameters(command=sys.executable, args=args,
                                   env={**os.environ, "PYTHONUNBUFFERED": "1"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await _exercise(session, checks)


async def _check_http(checks: list[tuple[bool, str]]) -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client("http://127.0.0.1:8151/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await _exercise(session, checks)


async def _check_sse(checks: list[tuple[bool, str]]) -> dict[str, Any]:
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async with sse_client("http://127.0.0.1:8152/sse") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await _exercise(session, checks)


def run_checks(config_path: str | None = None,
               transports: tuple[str, ...] = TRANSPORTS,
               quiet: bool = False) -> tuple[bool, list[tuple[bool, str]]]:
    """Auto-test : un vrai client se connecte et appelle les outils.

    Retourne ``(ok, checks)``.
    """
    checks: list[tuple[bool, str]] = []
    runners = {
        "stdio": lambda: _check_stdio(config_path, checks),
        "http": lambda: _check_http(checks),
        "sse": lambda: _check_sse(checks),
    }
    for transport in transports:
        if not quiet:
            print(f"\n  Transport {transport}")
        try:
            asyncio.run(runners[transport]())
        except Exception as exc:
            checks.append((False, f"{transport} : {type(exc).__name__}: {exc}"))
    ok = all(flag for flag, _ in checks)
    return ok, checks
