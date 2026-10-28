"""Tiroir de contexte : fragments indexés (FTS5), sources, ancres, invariants.

Le contenu d'origine est **conservé octet pour octet** dans la table des
fragments : c'est la garantie ``ctx_get`` est byte-exact. L'ancre (forme
compacte) et les invariants sont stockés à côté, pour l'indexation et la
vérification.

Indexation FTS5 à contenu externe : l'index est mis à jour **explicitement**
à l'écriture (une ligne par fragment), et un ``rebuild_index`` explicite
reconstruit l'index à la demande — jamais à chaque écriture (le recalcul
intégral à chaque écriture rend l'ensemble quadratique).
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Sequence

from ..db import Database
from .anchors import Invariant

FTS_TABLE = "context_chunks_fts"


@dataclass(frozen=True)
class Chunk:
    """Un fragment stocké."""

    id: int
    session_id: str
    source_digest: str
    chunk_index: int
    label: str
    body: str
    tokens: int
    created_at: float
    hits: int


@dataclass(frozen=True)
class Source:
    """Un contenu rangé (une « source »)."""

    id: int
    session_id: str
    label: str
    digest: str
    byte_size: int
    chunk_count: int
    tokens_before: int
    anchor_tokens: int
    redactions: int
    created_at: float


class Drawer:
    """Stockage et recherche dans le tiroir."""

    def __init__(self, database: Database) -> None:
        self.db = database

    # -- schéma -----------------------------------------------------------
    def ensure_schema(self) -> None:
        """Crée la table FTS5 si le moteur la supporte."""
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS context_chunks_fts "
                    "USING fts5(body, content='context_chunks', content_rowid='id')"
                )
        except sqlite3.OperationalError:
            # SQLite sans FTS5 : le tiroir fonctionne en mode dégradé
            # (recherche par LIKE). Aucune donnée n'est perdue.
            pass

    def fts_available(self) -> bool:
        return self.db.has_table(FTS_TABLE)

    # -- écriture ---------------------------------------------------------
    def store_source(
        self,
        session_id: str,
        label: str,
        content: str,
        units: Sequence[str],
        anchor: str,
        invariants: Sequence[Invariant],
        tokens_before: int,
        anchor_tokens: int,
        redactions: int = 0,
    ) -> tuple[int, list[int]]:
        """Enregistre une source, ses fragments, son ancre et ses invariants."""
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = time.time()
        chunk_ids: list[int] = []
        with self.db.transaction() as cur:
            cur.execute(
                "INSERT INTO context_sources"
                " (session_id, label, digest, byte_size, chunk_count, tokens_before,"
                "  anchor_tokens, redactions, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (session_id, label, digest, len(content.encode("utf-8")), len(units),
                 tokens_before, anchor_tokens, redactions, now),
            )
            source_id = int(cur.lastrowid or 0)
            for index, unit in enumerate(units):
                cur.execute(
                    "INSERT INTO context_chunks"
                    " (session_id, source_digest, chunk_index, label, body, tokens,"
                    "  created_at, hits)"
                    " VALUES (?,?,?,?,?,?,?,0)",
                    (session_id, digest, index, label, unit, 0, now),
                )
                chunk_id = int(cur.lastrowid or 0)
                chunk_ids.append(chunk_id)
                if self.fts_available():
                    cur.execute(
                        f"INSERT INTO {FTS_TABLE}(rowid, body) VALUES (?,?)",
                        (chunk_id, unit),
                    )
            cur.execute(
                "UPDATE context_sources SET first_chunk_id=?, last_chunk_id=? WHERE id=?",
                (chunk_ids[0] if chunk_ids else None,
                 chunk_ids[-1] if chunk_ids else None, source_id),
            )
            cur.execute(
                "INSERT INTO context_anchors"
                " (session_id, source_id, label, body, tokens, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (session_id, source_id, label, anchor, anchor_tokens, now),
            )
            for inv in invariants:
                kind, value, priority = inv.as_row()
                cur.execute(
                    "INSERT INTO context_invariants"
                    " (source_id, session_id, kind, value, priority) VALUES (?,?,?,?,?)",
                    (source_id, session_id, kind, value, priority),
                )
        return source_id, chunk_ids

    # -- lecture ----------------------------------------------------------
    def chunk(self, chunk_id: int) -> Chunk | None:
        with self.db.cursor(commit=False) as cur:
            row = cur.execute(
                "SELECT id, session_id, source_digest, chunk_index, label, body, tokens,"
                " created_at, hits FROM context_chunks WHERE id=?",
                (chunk_id,),
            ).fetchone()
            if row is None:
                return None
            cur.execute("UPDATE context_chunks SET hits = hits + 1 WHERE id=?", (chunk_id,))
        return Chunk(
            id=row["id"], session_id=row["session_id"], source_digest=row["source_digest"],
            chunk_index=row["chunk_index"], label=row["label"], body=row["body"],
            tokens=row["tokens"], created_at=row["created_at"], hits=row["hits"] + 1,
        )

    def chunks_of_session(self, session_id: str) -> list[Chunk]:
        with self.db.cursor(commit=False) as cur:
            rows = cur.execute(
                "SELECT id, session_id, source_digest, chunk_index, label, body, tokens,"
                " created_at, hits FROM context_chunks WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [
            Chunk(id=r["id"], session_id=r["session_id"], source_digest=r["source_digest"],
                  chunk_index=r["chunk_index"], label=r["label"], body=r["body"],
                  tokens=r["tokens"], created_at=r["created_at"], hits=r["hits"])
            for r in rows
        ]

    def sources(self, session_id: str | None = None) -> list[Source]:
        sql = ("SELECT id, session_id, label, digest, byte_size, chunk_count,"
               " tokens_before, anchor_tokens, redactions, created_at FROM context_sources")
        args: tuple[Any, ...] = ()
        if session_id:
            sql += " WHERE session_id=?"
            args = (session_id,)
        sql += " ORDER BY id"
        with self.db.cursor(commit=False) as cur:
            rows = cur.execute(sql, args).fetchall()
        return [
            Source(id=r["id"], session_id=r["session_id"], label=r["label"],
                   digest=r["digest"], byte_size=r["byte_size"],
                   chunk_count=r["chunk_count"], tokens_before=r["tokens_before"],
                   anchor_tokens=r["anchor_tokens"], redactions=r["redactions"],
                   created_at=r["created_at"])
            for r in rows
        ]

    def source(self, source_id: int) -> Source | None:
        for src in self.sources():
            if src.id == source_id:
                return src
        return None

    def anchor(self, source_id: int) -> str | None:
        with self.db.cursor(commit=False) as cur:
            row = cur.execute(
                "SELECT body FROM context_anchors WHERE source_id=? ORDER BY id DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        return row["body"] if row else None

    def invariants(self, source_id: int) -> list[Invariant]:
        with self.db.cursor(commit=False) as cur:
            rows = cur.execute(
                "SELECT kind, value, priority FROM context_invariants WHERE source_id=?"
                " ORDER BY priority, id",
                (source_id,),
            ).fetchall()
        return [Invariant(kind=r["kind"], value=r["value"], priority=r["priority"])
                for r in rows]

    def anchor_for_session(self, session_id: str) -> str:
        with self.db.cursor(commit=False) as cur:
            rows = cur.execute(
                "SELECT a.body FROM context_anchors a JOIN context_sources s"
                " ON a.source_id = s.id WHERE a.session_id=? ORDER BY a.id DESC LIMIT 1",
                (session_id,),
            ).fetchall()
        return rows[0]["body"] if rows else ""

    # -- recherche --------------------------------------------------------
    def search(self, query: str, session_id: str | None = None, limit: int = 5) -> list[dict]:
        """Recherche lexicale (FTS5 si disponible, sinon LIKE)."""
        limit = max(1, min(int(limit), 100))
        pattern = _fts_pattern(query)
        if pattern and self.fts_available():
            sql = (
                f"SELECT c.id, c.session_id, c.label, c.body, c.tokens,"
                f" bm25({FTS_TABLE}) AS score"
                f" FROM {FTS_TABLE} f JOIN context_chunks c ON c.id = f.rowid"
                f" WHERE {FTS_TABLE} MATCH ?"
            )
            args: list[Any] = [pattern]
            if session_id:
                sql += " AND c.session_id = ?"
                args.append(session_id)
            sql += " ORDER BY score LIMIT ?"
            args.append(limit)
            try:
                with self.db.cursor(commit=False) as cur:
                    rows = cur.execute(sql, tuple(args)).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        sql = "SELECT id, session_id, label, body, tokens, 0 AS score FROM context_chunks"
        args = []
        if session_id:
            sql += " WHERE session_id = ?"
            args.append(session_id)
        sql += " AND body LIKE ? ORDER BY id LIMIT ?"
        args.extend([f"%{query}%", limit])
        with self.db.cursor(commit=False) as cur:
            rows = cur.execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    # -- maintenance ------------------------------------------------------
    def rebuild_index(self) -> dict[str, int]:
        """Reconstruit l'index FTS5 à la demande (jamais à chaque écriture)."""
        if not self.fts_available():
            return {"rebuilt": 0, "skipped": 1}
        with self.db.transaction() as cur:
            cur.execute(f"DELETE FROM {FTS_TABLE}")
            cur.execute(
                f"INSERT INTO {FTS_TABLE}(rowid, body) SELECT id, body FROM context_chunks"
            )
            count = int(cur.execute(f"SELECT COUNT(*) FROM {FTS_TABLE}").fetchone()[0])
        return {"rebuilt": count, "skipped": 0}

    def stats(self) -> dict[str, Any]:
        with self.db.cursor(commit=False) as cur:
            chunks = int(cur.execute("SELECT COUNT(*) FROM context_chunks").fetchone()[0])
            sources = int(cur.execute("SELECT COUNT(*) FROM context_sources").fetchone()[0])
            invariants = int(cur.execute(
                "SELECT COUNT(*) FROM context_invariants").fetchone()[0])
            tokens_before = int(cur.execute(
                "SELECT COALESCE(SUM(tokens_before),0) FROM context_sources").fetchone()[0])
            anchor_tokens = int(cur.execute(
                "SELECT COALESCE(SUM(anchor_tokens),0) FROM context_sources").fetchone()[0])
        return {
            "chunks": chunks,
            "sources": sources,
            "invariants": invariants,
            "tokens_before": tokens_before,
            "anchor_tokens": anchor_tokens,
            "fts_available": self.fts_available(),
        }


def _fts_pattern(query: str) -> str:
    """Construit une expression FTS5 sûre à partir d'une saisie libre."""
    tokens = [t for t in query.replace('"', " ").split() if t]
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)
