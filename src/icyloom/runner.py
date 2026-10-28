"""Lancement combiné : passerelle + API d'administration + serveur MCP.

Les services partagent **la même configuration** et **la même base**.

Le serveur MCP est construit par une fonction unique (`build_server`), ce qui
garantit que les transports HTTP et SSE exposent **exactement la même liste
d'outils** : un client qui passe d'un transport à l'autre ne voit rien
changer. Le transport stdio, lui, est réservé au processus dédié
(`icyloom mcp --transport stdio`) : il doit posséder les flux standard.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .config import Config

log = logging.getLogger("icyloom.runner")


def _serve(app: Any, host: str, port: int, name: str) -> threading.Thread:
    """Démarre un serveur uvicorn dans un thread de fond."""
    import uvicorn

    def _target() -> None:
        log.info("%s -> http://%s:%s", name, host, port)
        uvicorn.run(app, host=host, port=port, log_level="warning")

    thread = threading.Thread(target=_target, name=f"icyloom-{name}", daemon=True)
    thread.start()
    return thread


def _serve_mcp(config: Config, transport: str, host: str, port: int) -> threading.Thread:
    """Démarre le serveur MCP sur un transport HTTP dans un thread de fond."""
    from .mcpserver import build_server

    def _target() -> None:
        mcp = build_server(config)
        mcp.settings.host = host
        mcp.settings.port = port
        log.info("mcp-%s -> http://%s:%s", transport, host, port)
        if transport == "sse":
            mcp.run(transport="sse")
        else:
            mcp.run(transport="streamable-http")

    thread = threading.Thread(target=_target, name=f"icyloom-mcp-{transport}", daemon=True)
    thread.start()
    return thread


def run_all(config: Config, wait: bool = True) -> None:
    """Lance les quatre services réseau."""
    from .admin import create_app as create_admin_app
    from .gateway import create_app as create_gateway_app

    host = config.server.host
    _serve(create_gateway_app(config), host, config.ports.gateway_http, "gateway")
    _serve(create_admin_app(config), host, config.ports.admin, "admin")
    _serve_mcp(config, "streamable-http", host, config.ports.mcp_http)
    _serve_mcp(config, "sse", host, config.ports.mcp_sse)

    if not wait:
        return
    log.info("IcyLoom en service : passerelle %s, MCP http %s, MCP sse %s, admin %s",
             config.ports.gateway_http, config.ports.mcp_http,
             config.ports.mcp_sse, config.ports.admin)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:  # pragma: no cover - arrêt manuel
        log.info("arrêt")
