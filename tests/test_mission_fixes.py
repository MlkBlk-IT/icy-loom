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


