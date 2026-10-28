"""Serveur MCP descendant factice (stdio), utilisé pour la démonstration.

Sert de cible au relais d'outils d'IcyLoom (`tool_proxy`) : deux outils en
lecture seule, donc cachables, ce qui permet de peupler réellement les tables
`tool_cache`, `tool_events` et le ledger.

Lancement : `python tools/fake_mcp_server.py`
"""

from __future__ import annotations

import time

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("icyloom-demo")

NOTES = {
    "8150": "passerelle HTTP OpenAI/Anthropic",
    "8151": "MCP streamable-HTTP",
    "8152": "MCP SSE",
    "8153": "API d'administration et console web",
}


@mcp.tool()
def list_metrics() -> dict:
    """Rend un petit tableau de métriques (outil en lecture, donc cachable)."""
    return {"ports": sorted(NOTES), "latency_ms": 3, "checked_at": int(time.time())}


@mcp.tool()
def read_notes(port: str) -> dict:
    """Rend la note associée à un port (outil en lecture, donc cachable)."""
    return {"port": port, "note": NOTES.get(port, "port inconnu")}


@mcp.tool()
def read_file(path: str) -> dict:
    """Rend le contenu d'un fichier fictif (outil en lecture, donc cachable)."""
    name = path.rsplit("/", 1)[-1]
    return {"path": path, "name": name, "lines": 42,
            "content": f"# {name}\ncontenu de {path}\nfin du fichier {name}\n"}


@mcp.tool()
def list_dir(path: str) -> dict:
    """Liste le contenu d'un répertoire fictif (outil en lecture, donc cachable)."""
    entries = ["config.yaml", "README.md", "loader.py", "tests/", "var/"]
    return {"path": path, "entries": entries, "count": len(entries)}


@mcp.tool()
def search_code(pattern: str, limit: int = 10) -> dict:
    """Cherche un motif dans le code (outil en lecture, donc cachable)."""
    return {"pattern": pattern, "limit": limit,
            "matches": [{"file": "src/icyloom/gateway.py", "line": 237},
                        {"file": "src/icyloom/cache.py", "line": 88}]}


@mcp.tool()
def git_log(ref: str) -> dict:
    """Rend un faux historique git (outil en lecture, donc cachable)."""
    return {"ref": ref,
            "commits": [{"sha": "a1b2c3d4", "message": "console live"},
                        {"sha": "e5f6a7b8", "message": "compteur de cache"}]}


if __name__ == "__main__":
    mcp.run(transport="stdio")
