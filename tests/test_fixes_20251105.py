"""Tests rouge→vert des 3 fixes de la session 2026-11-05.

1. ledger.daily() : GROUP BY date(ts) (pas day(), fonction inexistante).
2. flux stream coupé : breaker + error event, pas de cache.
3. migrate.py : mapping dict colonne source → colonne cible.
"""

from __future__ import annotations

import socket
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import build_config
from icyloom.db import Database
from icyloom.ledger import Ledger, LedgerEntry


# -- FIX 1 : ledger.daily() groupe par date(ts) ------------------------------

def test_ledger_daily_groups_by_date(tmp_path):
    """daily() retourne une série groupée par date(ts), pas par day(ts)."""
    db = Database(build_config(tmp_path))
    db.ensure_schema()
    ledger = Ledger(db, enabled=True)

    with db.transaction() as cur:
        # Today
        cur.execute(
            "INSERT INTO ledger_entries (ts, kind, tokens_in, tokens_out, tokens_saved)"
            " VALUES (datetime('now'), 'llm_call', 100, 50, 0)"
        )
        # Yesterday
        cur.execute(
            "INSERT INTO ledger_entries (ts, kind, tokens_in, tokens_out, tokens_saved)"
            " VALUES (datetime('now', '-1 day'), 'llm_call', 200, 80, 30)"
        )

    series = ledger.daily(days=7)
    assert len(series) >= 2, f"expected >= 2 days, got {len(series)}: {series}"
    days = [row["day"] for row in series]
    # Each day appears exactly once (grouped, not duplicated)
    assert len(days) == len(set(days)), f"duplicate days: {days}"
    # Ordered by date
    assert days == sorted(days), f"not ordered: {days}"
    # Totals match
    total_paid = sum(row["paid"] for row in series)
    total_saved = sum(row["saved"] for row in series)
    assert total_paid == 430  # (100+50) + (200+80)
    assert total_saved == 30


# -- FIX 2 : flux stream coupé -> breaker, error event, pas de cache --------

class _CuttingServer:
    """Serveur TCP brut : envoie un chunk SSE puis ferme (chunked tronqué)."""

    def __init__(self, port: int = 8158) -> None:
        self.port = port
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(1)
        self._sock.settimeout(0.5)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.recv(8192)  # consume HTTP request
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/event-stream\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"\r\n"
                )
                chunk = b'data: {"choices":[{"delta":{"content":"partial "}}]}\n\n'
                conn.sendall(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                # Close without chunked terminator (0\r\n\r\n) -> RemoteProtocolError
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            self._sock.close()
        if self._thread:
            self._thread.join(timeout=2)


def test_stream_breaker_on_disconnect(tmp_path):
    """Un flux coupé en plein vol : error event, pas de cache, breaker à 1."""
    from icyloom.gateway import create_app

    server = _CuttingServer(port=8158)
    server.start()
    time.sleep(0.3)

    cfg = build_config(tmp_path, upstream_url="http://127.0.0.1:8158/v1")
    app = create_app(cfg)

    try:
        with TestClient(app) as client:
            body = {"model": "fake-model", "stream": True,
                    "messages": [{"role": "user", "content": "test"}]}
            resp = client.post("/v1/chat/completions", json=body)
            assert resp.status_code == 200  # headers received before the cut

        # Verify DB state after the stream completes (with error)
        db = Database(cfg)
        with db.cursor(commit=False) as cur:
            error_count = int(cur.execute(
                "SELECT COUNT(*) FROM llm_events WHERE kind='error' AND stream=1"
            ).fetchone()[0])
            cache_count = int(cur.execute(
                "SELECT COUNT(*) FROM cache_entries"
            ).fetchone()[0])

        gateway = app.state.gateway
        breaker = gateway.breakers.for_name("fake")

        assert error_count >= 1, f"expected >= 1 error event, got {error_count}"
        assert cache_count == 0, f"truncated stream should not be cached, got {cache_count}"
        assert breaker.status().failures >= 1, \
            f"breaker should have >= 1 failure, got {breaker.status().failures}"
    finally:
        server.stop()


# -- FIX 3 : migrate.py mapping dict source → cible --------------------------

def test_migrate_dict_mapping(tmp_path):
    """Migration avec mapping dict : colonnes source renommées vers colonnes cible."""
    from icyloom.config import Config, DatabaseConfig, PortsConfig, LedgerConfig
    from icyloom.migrate import run_migration

    # Source DB with old schema (column names differ from unified schema)
    source_path = tmp_path / "source.db"
    conn = sqlite3.connect(str(source_path))
    conn.executescript("""
        CREATE TABLE cache (
            prompt_hash      TEXT PRIMARY KEY,
            normalized_prompt TEXT,
            original_prompt  TEXT,
            response         TEXT,
            is_stream        INTEGER DEFAULT 0,
            provider         TEXT,
            model            TEXT,
            created_at       TEXT DEFAULT (datetime('now')),
            last_accessed    TEXT DEFAULT (datetime('now')),
            access_count     INTEGER DEFAULT 0
        );
        CREATE TABLE tool_cache (
            key         TEXT PRIMARY KEY,
            server      TEXT,
            tool        TEXT,
            result_json TEXT,
            bytes       INTEGER,
            latency_ms  INTEGER,
            ttl         INTEGER,
            created_at  TEXT DEFAULT (datetime('now')),
            last_accessed TEXT DEFAULT (datetime('now')),
            hits        INTEGER DEFAULT 0
        );
    """)
    for i in range(5):
        conn.execute(
            "INSERT INTO cache (prompt_hash, normalized_prompt, original_prompt,"
            " response, is_stream, provider, model) VALUES (?,?,?,?,?,?,?)",
            (f"hash_{i}", f"norm_{i}", f"orig_{i}", f"resp_{i}", 0, "fake", "m1")
        )
    conn.execute(
        "INSERT INTO tool_cache (key, server, tool, result_json, bytes, latency_ms, ttl)"
        " VALUES (?,?,?,?,?,?,?)",
        ("tkey_1", "srv", "read_file", '{"x":1}', 10, 5, 60)
    )
    conn.commit()
    conn.close()

    # Config for the unified destination DB
    cfg = Config.model_validate({
        "server": {"name": "test"},
        "ports": PortsConfig().model_dump(),
        "database": DatabaseConfig(path=str(tmp_path / "unified.db")).model_dump(),
        "ledger": LedgerConfig(enabled=True).model_dump(),
        "upstreams": [],
    })

    # Run migration
    result = run_migration(cfg, source=source_path, dry_run=False)
    assert result is True

    # Verify: 5 rows in cache_entries with renamed columns
    db = Database(cfg)
    with db.cursor(commit=False) as cur:
        rows = cur.execute(
            "SELECT key, normalized, original, response FROM cache_entries ORDER BY key"
        ).fetchall()
        assert len(rows) == 5, f"expected 5 cache entries, got {len(rows)}"
        assert rows[0]["key"] == "hash_0"          # prompt_hash -> key
        assert rows[0]["normalized"] == "norm_0"    # normalized_prompt -> normalized
        assert rows[0]["original"] == "orig_0"      # original_prompt -> original

        tool_rows = cur.execute(
            "SELECT key, server, tool FROM tool_cache"
        ).fetchall()
        assert len(tool_rows) == 1, f"expected 1 tool_cache row, got {len(tool_rows)}"
        assert tool_rows[0]["key"] == "tkey_1"

    # Idempotency: re-run, still 5 (INSERT OR IGNORE)
    run_migration(cfg, source=source_path, dry_run=False)
    with db.cursor(commit=False) as cur:
        count = int(cur.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0])
        assert count == 5, f"idempotent re-run should still be 5, got {count}"
