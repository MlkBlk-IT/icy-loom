"""Migration additive vers la base unifiée.

Principe : les bases d'origine sont **lues** (jamais modifiées) et leurs
lignes sont **copiées** dans la base unifiée, avec correspondance de
colonnes. Aucune suppression, aucune modification côté source.

L'opération est réversible : la base unifiée peut être jetée et reconstruite
depuis les sources. Un mode ``--dry-run`` affiche le plan sans écrire.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database

log = logging.getLogger("icyloom.migrate")

#: Correspondance (table source → table cible, colonnes).
#: Seules les tables dont le schéma est compatible sont migrées.
MAPPING: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("cache", "cache_entries",
     ("prompt_hash", "normalized_prompt", "original_prompt", "response", "is_stream",
      "provider", "model")),
    ("tool_cache", "tool_cache",
     ("key", "server", "tool", "result_json", "bytes", "latency_ms", "ttl")),
)


def _source_tables(path: Path) -> list[str]:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def run_migration(config: Config, source: Path | None = None,
                  dry_run: bool = False) -> bool:
    """Copie les données d'une base source vers la base unifiée."""
    if source is None:
        log.error("aucune source indiquée : utilisez --source <fichier .db>")
        return False
    if not source.is_file():
        log.error("source introuvable : %s", source)
        return False

    tables = _source_tables(source)
    if not tables:
        log.error("source illisible ou vide : %s", source)
        return False

    db = Database(config)
    db.ensure_schema()
    plan: list[dict[str, Any]] = []
    for src_table, dst_table, columns in MAPPING:
        if src_table not in tables:
            continue
        count = _count(source, src_table)
        plan.append({"source_table": src_table, "target_table": dst_table,
                     "columns": list(columns), "rows": count})
    if not plan:
        log.warning("aucune table migrable dans %s (trouvées : %s)", source, tables)
        return False

    for entry in plan:
        log.info("migration %s -> %s : %s lignes",
                 entry["source_table"], entry["target_table"], entry["rows"])
    if dry_run:
        log.info("dry-run : rien n'a été écrit")
        return True

    for entry in plan:
        _copy(db, source, entry["source_table"], entry["target_table"], entry["columns"])
    log.info("migration terminée")
    return True


def _count(path: Path, table: str) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _copy(db: Database, source: Path, src_table: str, dst_table: str,
          columns: tuple[str, ...]) -> int:
    """Copie les lignes compatibles (les colonnes absentes sont ignorées)."""
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        available = {r[1] for r in conn.execute(f"PRAGMA table_info({src_table})")}
        usable = [c for c in columns if c in available]
        if not usable:
            return 0
        rows = conn.execute(
            f"SELECT {', '.join(usable)} FROM {src_table}").fetchall()
    finally:
        conn.close()

    copied = 0
    with db.transaction() as cur:
        target_columns = {r[1] for r in cur.execute(f"PRAGMA table_info({dst_table})")}
        for column in usable:
            if column not in target_columns:
                continue
        placeholders = ", ".join("?" * len(usable))
        for row in rows:
            try:
                cur.execute(
                    f"INSERT OR IGNORE INTO {dst_table} ({', '.join(usable)})"
                    f" VALUES ({placeholders})",
                    tuple(row[c] for c in usable),
                )
                copied += 1
            except sqlite3.Error as exc:
                log.warning("ligne ignorée (%s) : %s", src_table, exc)
    return copied
