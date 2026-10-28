"""Reconnaissance d'un appel d'outil « presque identique » + session réutilisée.

La fusion est **guidée par le schéma** de l'outil : on ne fusionne que ce que
le type déclaré rend équivalent. Deux valeurs réellement différentes ne sont
jamais confondues.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import build_config  # noqa: E402

from icyloom.toolproxy import (  # noqa: E402
    ToolProxy,
    canon_key,
    canonical_args,
)

INTEGER = {"type": "integer"}
NUMBER = {"type": "number"}
BOOLEAN = {"type": "boolean"}
TEXT = {"type": "string"}
PATH = {"type": "string"}


def test_integer_argument_merges_string_and_int() -> None:
    assert canonical_args("10", INTEGER) == 10
    assert canonical_args(10, INTEGER) == 10
    assert canonical_args(10.0, INTEGER) == 10


def test_number_and_boolean_arguments() -> None:
    assert canonical_args("10.5", NUMBER) == 10.5
    assert canonical_args("true", BOOLEAN) is True
    assert canonical_args("false", BOOLEAN) is False


def test_string_argument_is_never_touched() -> None:
    # une chaîne est une valeur : " 8150 " n'est PAS "8150"
    assert canonical_args(" 8150 ", TEXT) == " 8150 "
    assert canonical_args("Cache", TEXT) == "Cache"


def test_path_argument_normalizes_separators() -> None:
    # la règle « chemin » s'applique aux paramètres dont le NOM le dit
    # (path, file, dir…) : c'est le schéma de l'outil qui fait foi.
    assert canonical_args("src\\icyloom\\gateway.py", PATH, "path") == \
        "src/icyloom/gateway.py"
    assert canonical_args("src//icyloom/", PATH, "path") == "src/icyloom"
    # un paramètre texte qui n'est pas un chemin n'est pas touché
    assert canonical_args("src\\icyloom\\gateway.py", TEXT, "label") == \
        "src\\icyloom\\gateway.py"


def test_canonical_key_ignores_key_order_and_padding() -> None:
    left = canon_key("srv", "tool", {"b": 2, "a": 1})
    right = canon_key("srv", "tool", {"a": 1, "b": 2})
    assert left == right


def test_canonical_key_separates_real_differences() -> None:
    assert canon_key("srv", "tool", {"port": 8150}) != canon_key("srv", "tool", {"port": 8153})
    assert canon_key("srv", "tool", {"q": "cache"}) != canon_key("srv", "tool", {"q": "ledger"})


def test_cache_lookup_by_canonical_key(tmp_path: Path) -> None:
    """Un appel quasi identique est retrouvé par la clé canonique.

    La fusion exige le **schéma** de l'outil (chargé avec le catalogue) :
    sans lui, seuls les appels strictement identiques sont reconnus.
    """
    proxy = ToolProxy(build_config(tmp_path))
    schema = {"type": "object", "properties": {"limit": {"type": "integer"}}}
    proxy._schemas["srv"] = {"tool": schema}
    proxy.cache_set("key-exact", "srv", "tool", {"content": ["v"]}, 12,
                    canon=canon_key("srv", "tool", {"limit": 10}, schema=schema))

    found = proxy.cache_get_canonical("srv", "tool", {"limit": "10"}, 3600)
    assert found is not None, "l'appel quasi identique doit être retrouvé"
    assert found["result"] == {"content": ["v"]}
    assert found["matched"] == "canonical"

    # une valeur réellement différente n'est pas retrouvée
    assert proxy.cache_get_canonical("srv", "tool", {"limit": 99}, 3600) is None
    # ni un autre serveur / un autre outil
    assert proxy.cache_get_canonical("autre", "tool", {"limit": 10}, 3600) is None
    assert proxy.cache_get_canonical("srv", "autre", {"limit": 10}, 3600) is None


def test_without_schema_only_identical_calls_are_recognized(tmp_path: Path) -> None:
    """Sans schéma, « 10 » et 10 restent deux appels différents."""
    proxy = ToolProxy(build_config(tmp_path))
    proxy.cache_set("key-exact", "srv", "tool", {"content": ["v"]}, 12,
                    canon=canon_key("srv", "tool", {"limit": 10}))
    assert proxy.cache_get_canonical("srv", "tool", {"limit": "10"}, 3600) is None
    assert proxy.cache_get_canonical("srv", "tool", {"limit": 10}, 3600) is not None


def test_canonical_lookup_ignores_expired_entries(tmp_path: Path) -> None:
    proxy = ToolProxy(build_config(tmp_path))
    proxy.cache_set("key-exact", "srv", "tool", {"content": ["v"]}, 12,
                    canon=canon_key("srv", "tool", {"limit": 10}))
    assert proxy.cache_get_canonical("srv", "tool", {"limit": "10"}, 0) is None


def test_exact_cache_still_wins_over_canonical(tmp_path: Path) -> None:
    """La clé exacte reste prioritaire : elle porte l'entrée la plus récente."""
    proxy = ToolProxy(build_config(tmp_path))
    proxy.cache_set("key-exact", "srv", "tool", {"content": ["exact"]}, 12,
                    canon=canon_key("srv", "tool", {"limit": 10}))
    exact = proxy.cache_get("key-exact", 3600)
    assert exact is not None
    assert exact["result"] == {"content": ["exact"]}
    assert "matched" not in exact
