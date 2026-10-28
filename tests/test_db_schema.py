"""Regression : le schema doit se creer sur une base NEUVE (bug canon_key)."""

from __future__ import annotations

from icyloom.db import Database


def test_schema_on_fresh_db_is_created_and_idempotent(config) -> None:
    db = Database(config)
    db.ensure_schema()
    db.ensure_schema()
    assert db.has_table("tool_cache")
    with db.cursor(commit=False) as cur:
        cols = {r[1] for r in cur.execute("PRAGMA table_info(tool_cache)")}
        idx = {r[1] for r in cur.execute("PRAGMA index_list(tool_cache)")}
    assert "canon_key" in cols
    assert "idx_tool_cache_canon" in idx
