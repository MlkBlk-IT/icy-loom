"""Preuves des 3 corrections du 2026-11-22.

1. ``token_tool_schemas`` mesurait le sch\u00e9ma BRUT ; l'agent ne re\u00e7oit que
   le contrat compress\u00e9 (12,6 M de tokens sur 30 j au lieu du sch\u00e9ma r\u00e9el).
2. ``token_cache_read`` restait \u00e0 0 : le repli canonique, qui sert
   r\u00e9ellement les hits, n'\u00e9tait pas instrument\u00e9.
3. 547 ``tool_error`` / 30 j (dont 546 ``list_tools``) : les erreurs
   identiques se r\u00e9p\u00e9taient au lieu d'\u00eatre d\u00e9dupliqu\u00e9es.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import build_config  # noqa: E402

from icyloom.registry import Database  # noqa: E402
from icyloom.registry import ToolRegistry  # noqa: E402
from icyloom.toolcompress import compress_schema  # noqa: E402
from icyloom.toolproxy import ToolProxy, canon_key  # noqa: E402
from icyloom.tokenizer import count_tokens  # noqa: E402


def _big_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            f"p{i}": {"type": "string", "description": "x" * 400} for i in range(30)
        },
    }


def test_schema_tokens_mesures_le_schema_expose(config) -> None:
    """Le compteur doit refl\u00e9ter le sch\u00e9ma inject\u00e9, pas le sch\u00e9ma brut."""
    db = Database(config)
    db.ensure_schema()
    reg = ToolRegistry(db)
    big = _big_schema()
    reg.register("srv", [{"name": "t", "description": "d" * 500}], {"t": big})

    raw = count_tokens(json.dumps(big, ensure_ascii=False))
    shown = count_tokens(json.dumps(compress_schema(big), ensure_ascii=False))
    assert shown < raw, "le sch\u00e9ma compress\u00e9 doit \u00eatre plus petit que le brut"
    assert reg.schema_tokens("srv")["srv"] == shown


def test_cache_get_canonical_compte_une_lecture(config) -> None:
    """Le repli canonique sert les hits : il doit \u00eatre compt\u00e9 en cache_read."""
    proxy = ToolProxy(config)
    proxy.cache_set(canon_key("srv", "tool", {"a": 1, "b": 2}), "srv", "tool",
                    {"a": 1, "b": 2}, 1200,
                    canon=canon_key("srv", "tool", {"a": 1, "b": 2}))

    hit = proxy.cache_get_canonical("srv", "tool", {"b": 2, "a": 1}, 600)
    assert hit is not None, "le repli canonique doit servir le hit"

    with proxy.db.transaction() as cur:
        row = cur.execute(
            "SELECT tokens_in FROM ledger_entries WHERE kind='token_cache_read'"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "cache_read n'a pas \u00e9t\u00e9 enregistr\u00e9 dans le ledger"
    assert row[0] > 0


def test_erreur_identique_n_est_loggee_qu_une_fois(config) -> None:
    """547 tool_error / 30 j : une erreur identique ne doit passer qu'une fois."""
    proxy = ToolProxy(config)
    for _ in range(5):
        proxy._log_event("tool_error", "srv", "list_tools", {}, 7000, 0,
                         "Connection closed")
    proxy._log_event("tool_error", "srv", "list_tools", {}, 7000, 0, "autre cause")

    with proxy.db.transaction() as cur:
        n = cur.execute(
            "SELECT COUNT(*) FROM tool_events WHERE kind='tool_error' AND server='srv'"
        ).fetchone()[0]
    assert n == 2, f"2 causes distinctes attendues, {n} lignes"
