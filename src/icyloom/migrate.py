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

#: Correspondance (table source → table cible, {colonne source: colonne cible}).
#: Les noms diffèrent entre l'ancien schéma et le schéma unifié (prompt_hash→key,
#: normalized_prompt→normalized, original_prompt→original) : réutiliser le nom
#: source côté cible faisait échouer chaque INSERT.
MAPPING: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("cache", "cache_entries", {
        "prompt_hash": "key", "normalized_prompt": "normalized",
        "original_prompt": "original", "response": "response",
        "is_stream": "is_stream", "provider": "provider", "model": "model",
        "created_at": "created_at", "last_accessed": "last_accessed",
        "access_count": "access_count"}),
    ("tool_cache", "tool_cache", {c: c for c in (
        "key", "server", "tool", "result_json", "bytes", "latency_ms", "ttl",
        "created_at", "last_accessed", "hits")}),
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
                     "columns": dict(columns), "rows": count})
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
        copied = _copy(db, source, entry["source_table"], entry["target_table"],
                       entry["columns"])
        log.info("copié %s -> %s : %s/%s lignes", entry["source_table"],
                 entry["target_table"], copied, entry["rows"])
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
          columns: dict[str, str]) -> int:
    """Copie les lignes (colonne source → colonne cible). Renvoie le nombre
    de lignes réellement insérées (un doublon ignoré ne compte pas)."""
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        available = {r[1] for r in conn.execute(f"PRAGMA table_info({src_table})")}
        pairs = [(s, d) for s, d in columns.items() if s in available]
        if not pairs:
            return 0
        rows = conn.execute(
            f"SELECT {', '.join(s for s, _ in pairs)} FROM {src_table}").fetchall()
    finally:
        conn.close()

    copied = 0
    with db.transaction() as cur:
        target_columns = {r[1] for r in cur.execute(f"PRAGMA table_info({dst_table})")}
        pairs = [(s, d) for s, d in pairs if d in target_columns]
        if not pairs:
            return 0
        sql = (f"INSERT OR IGNORE INTO {dst_table} ({', '.join(d for _, d in pairs)})"
               f" VALUES ({', '.join('?' * len(pairs))})")
        for row in rows:
            try:
                cur.execute(sql, tuple(row[s] for s, _ in pairs))
                copied += int(cur.rowcount or 0)
            except sqlite3.Error as exc:
                log.warning("ligne ignorée (%s) : %s", src_table, exc)
    return copied
