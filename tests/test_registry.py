"""Le registre unifié (`v_token_ledger`) et la console.

Ces tests verrouillent le « 1+1>2 » du merge : les trois domaines (cache,
compression, tax d'outils) sont mesurés dans la **même unité** (tokens
payés / tokens évités), et la console doit les exposer sans jamais estimer.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from icyloom.config import Config
from icyloom.dashboard import SnapshotCache, snapshot
from icyloom.db import Database


#: La fixture `config` (base isolée, ports de test) vient de tests/conftest.py.


def _insert_event(db: Database, kind: str, prompt: int, completion: int,
                  saved_prompt: int, saved_completion: int, ts: str | None = None) -> None:
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO llm_events (kind, prompt_tokens, completion_tokens,"
            " saved_prompt, saved_completion, latency_ms, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (kind, prompt, completion, saved_prompt, saved_completion, 1,
             ts or time.strftime("%Y-%m-%d %H:%M:%S")),
        )


def test_view_exists_and_is_readable(config: Config) -> None:
    db = Database(config)
    db.ensure_schema()
    with db.cursor(commit=False) as cur:
        names = {r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='view'").fetchall()}
    assert "v_token_ledger" in names


def test_view_columns_are_the_unified_ones(config: Config) -> None:
    db = Database(config)
    db.ensure_schema()
    with db.cursor(commit=False) as cur:
        cur.execute("SELECT * FROM v_token_ledger LIMIT 1")
        columns = [d[0] for d in cur.description]
    assert columns == ["domain", "ts", "tokens_paid", "tokens_saved",
                       "latency_ms", "saved_latency_ms"]


def test_cache_miss_is_paid_and_hit_is_avoided(config: Config) -> None:
    """Un miss paie ses tokens, un hit les évite : rien n'est compté deux fois."""
    db = Database(config)
    db.ensure_schema()
    _insert_event(db, "miss", 100, 20, 0, 0)
    _insert_event(db, "hit", 0, 0, 100, 20)
    with db.cursor(commit=False) as cur:
        row = cur.execute(
            "SELECT SUM(tokens_paid) AS paid, SUM(tokens_saved) AS saved"
            " FROM v_token_ledger WHERE domain='cache'").fetchone()
    assert row["paid"] == 120
    assert row["saved"] == 120


def test_compression_domain_counts_anchor_as_paid(config: Config) -> None:
    db = Database(config)
    db.ensure_schema()
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO context_sources (session_id, label, digest, byte_size,"
            " chunk_count, tokens_before, anchor_tokens, created_at)"
            " VALUES ('s', 'l', 'd', 10, 1, 1000, 250, ?)",
            (time.time(),),
        )
    with db.cursor(commit=False) as cur:
        row = cur.execute(
            "SELECT tokens_paid, tokens_saved FROM v_token_ledger"
            " WHERE domain='compression'").fetchone()
    assert row["tokens_paid"] == 250
    assert row["tokens_saved"] == 750


def test_tool_tax_counts_saved_latency(config: Config) -> None:
    db = Database(config)
    db.ensure_schema()
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO tool_events (kind, server, tool, latency_ms, saved_latency_ms)"
            " VALUES ('tool_hit', 'srv', 'read', 120, 118)")
    with db.cursor(commit=False) as cur:
        row = cur.execute(
            "SELECT latency_ms, saved_latency_ms FROM v_token_ledger"
            " WHERE domain='tools'").fetchone()
    assert row["saved_latency_ms"] == 118


def test_registry_totals_and_threshold(config: Config) -> None:
    db = Database(config)
    db.ensure_schema()
    _insert_event(db, "miss", 600, 0, 0, 0)
    _insert_event(db, "hit", 0, 0, 600, 0)
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO context_sources (session_id, label, digest, byte_size,"
            " chunk_count, tokens_before, anchor_tokens, created_at)"
            " VALUES ('s', 'l', 'd', 10, 1, 400, 100, ?)", (time.time(),))
    data = snapshot(config, hours=24, days=14)
    registry = data["registry"]
    # payés 600 (miss) + 100 (ancre) = 700 ; évités 600 (hit) + 300 (compression)
    assert registry["totals"]["tokens_paid"] == 700
    assert registry["totals"]["tokens_saved"] == 900
    assert registry["totals"]["tokens_baseline"] == 1600
    assert registry["gain_pct"] == pytest.approx(56.25, abs=0.01)
    assert registry["threshold_pct"] == 35.0
    assert registry["threshold_met"] is True
    domains = {row["domain"] for row in registry["by_domain"]}
    assert {"cache", "compression"} <= domains


def test_registry_daily_matches_the_view(config: Config) -> None:
    """La série journalière doit totaled exactement comme la vue."""
    db = Database(config)
    db.ensure_schema()
    _insert_event(db, "hit", 0, 0, 300, 0)
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO context_sources (session_id, label, digest, byte_size,"
            " chunk_count, tokens_before, anchor_tokens, created_at)"
            " VALUES ('s', 'l', 'd', 10, 1, 500, 200, ?)", (time.time(),))
    data = snapshot(config, hours=24, days=14)
    daily = data["registry"]["daily"]
    assert daily
    with db.cursor(commit=False) as cur:
        view_paid = cur.execute(
            "SELECT COALESCE(SUM(tokens_paid), 0) FROM v_token_ledger"
            " WHERE ts >= datetime('now', '-14 days')").fetchone()[0]
        view_saved = cur.execute(
            "SELECT COALESCE(SUM(tokens_saved), 0) FROM v_token_ledger"
            " WHERE ts >= datetime('now', '-14 days')").fetchone()[0]
    assert sum(row["paid"] for row in daily) == view_paid
    assert sum(row["saved"] for row in daily) == view_saved


def test_surface_is_a_filled_day_by_hour_grid(config: Config) -> None:
    db = Database(config)
    db.ensure_schema()
    _insert_event(db, "miss", 10, 0, 0, 0, ts="2026-10-20 13:30:00")
    _insert_event(db, "miss", 10, 0, 0, 0, ts="2026-10-20 13:45:00")
    data = snapshot(config, hours=24, days=14)
    surface = data["surface"]
    assert len(surface["days"]) == surface["window_days"]
    assert all(len(row) == 24 for row in surface["calls"])
    assert surface["total"] == 2
    assert surface["peak"] == 2


def test_snapshot_exposes_the_fused_blocks(config: Config) -> None:
    data = snapshot(config, hours=24, days=14)
    for key in ("kpi", "registry", "surface", "series", "breakdown", "tables",
                "context", "recent", "db", "config"):
        assert key in data
    assert data["kpi"]["threshold_pct"] == 35.0
    assert "gain_pct" in data["kpi"]
    assert "by_model" in data["breakdown"]
    assert "by_upstream" in data["breakdown"]
    assert "tool_by_server" in data["breakdown"]


def test_snapshot_cache_serves_the_last_payload(config: Config) -> None:
    cache = SnapshotCache(config, hours=24, days=14, interval=0.2)
    try:
        first = cache.get()
        assert first["kpi"]["calls"] == 0
        time.sleep(0.5)
        cache.refresh()
        second = cache.get()
        assert second["kpi"]["calls"] == 0
        assert cache.status()["refreshes"] >= 1
    finally:
        cache.stop()


def test_dashboard_html_is_served_and_in_english() -> None:
    from icyloom.dashboard import dashboard_html

    html = dashboard_html()
    assert "<!DOCTYPE html>" in html
    # la console est en anglais et fusionne les deux parents
    assert "ICYLOOM" in html
    assert "v_token_ledger" in html
    assert "Reading the metrics" in html
    assert "The 13 MCP tools" in html
    assert "Audit live" in html
    assert "Context drawer" in html
    # aucun accent français résiduel dans les libellés d'interface
    for forbidden in ("Les jetons", "Le cratère du trafic", "Jetons payés"):
        assert forbidden not in html


def test_admin_serves_the_console(config: Config) -> None:
    from fastapi.testclient import TestClient

    from icyloom.admin import create_app

    app = create_app(config)
    with TestClient(app) as client:
        page = client.get("/dashboard")
        assert page.status_code == 200
        assert "ICYLOOM" in page.text
        data = client.get("/api/dashboard").json()
        assert "registry" in data
        assert "surface" in data


def test_the_view_survives_a_restart(config: Config) -> None:
    """La vue est created au démarrage : pas de table cachée, pas de migration."""
    db = Database(config)
    db.ensure_schema()
    _insert_event(db, "hit", 0, 0, 42, 0)
    with sqlite3.connect(str(config.db_path())) as raw:
        rows = raw.execute(
            "SELECT tokens_saved FROM v_token_ledger WHERE domain='cache'").fetchall()
    assert rows and rows[0][0] == 42
