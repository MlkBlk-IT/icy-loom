"""Relais d'outils : persistance du compteur de réutilisations.

Régression : ``cache_get`` incrémentait ``tool_cache.hits`` dans une
transaction qui n'était jamais commitée -> le compteur restait à 0 alors que
le journal ``tool_events`` comptabilisait des ``tool_hit``.
"""

from __future__ import annotations

from icyloom.toolproxy import ToolProxy


def _hits(db) -> int:
    with db.cursor() as cur:
        row = cur.execute(
            "SELECT COALESCE(SUM(hits),0) AS h FROM tool_cache"
        ).fetchone()
    return int(row["h"])


def test_cache_hit_counter_is_persisted(config):
    proxy = ToolProxy(config)
    proxy.cache_set("key-1", "demo", "list_metrics", {"ok": True}, 12)

    assert proxy.cache_get("key-1", 3600) is not None
    assert proxy.cache_get("key-1", 3600) is not None

    # relu sur une connexion neuve : la valeur doit avoir été persistée
    assert _hits(proxy.db) == 2


def test_cache_get_on_expired_entry_does_not_count(config):
    proxy = ToolProxy(config)
    proxy.cache_set("key-2", "demo", "read_notes", {"ok": True}, 5)

    assert proxy.cache_get("key-2", 0) is None
    assert _hits(proxy.db) == 0


def test_cache_set_refreshes_existing_key(config):
    proxy = ToolProxy(config)
    proxy.cache_set("key-3", "demo", "list_metrics", {"v": 1}, 5)
    proxy.cache_set("key-3", "demo", "list_metrics", {"v": 2}, 7)

    with proxy.db.cursor() as cur:
        row = cur.execute(
            "SELECT hits, result_json FROM tool_cache WHERE key=?", ("key-3",)
        ).fetchone()
    assert int(row["hits"]) == 1
    assert '"v": 2' in row["result_json"]
