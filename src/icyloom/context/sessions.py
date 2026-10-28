"""Sessions du tiroir de contexte.

Identifiant **UUIDv7** : tri par ordre chronologique (utile pour lister et
pour paginer), tout en restant un UUID standard.

Chaque session porte une durée de vie (TTL). Une session expirée est
« logiquement » absente : les lectures ne la renvoient plus, et la purge
physique peut la supprimer.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from ..db import Database

DEFAULT_TTL_DAYS = 30


def new_session_id() -> str:
    """UUIDv7 : 48 bits de temps + 74 bits aléatoires, tri par création."""
    millis = int(time.time() * 1000)
    rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (millis << 80) | (0x7 << 76) | (rand_a << 64) | rand_b
    hexed = f"{value:032x}"
    return (
        f"{hexed[0:8]}-{hexed[8:12]}-{hexed[12:16]}-"
        f"{hexed[16:20]}-{hexed[20:32]}"
    )


@dataclass(frozen=True)
class Session:
    """Une session du tiroir."""

    session_id: str
    label: str
    created_at: float
    updated_at: float
    ttl_days: int

    def is_expired(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return (now - self.updated_at) > self.ttl_days * 86400


class SessionStore:
    """Persistance des sessions dans la base unifiée."""

    def __init__(self, database: Database, ttl_days: int = DEFAULT_TTL_DAYS) -> None:
        self.db = database
        self.ttl_days = ttl_days

    # -- écriture ---------------------------------------------------------
    def create(self, session_id: str | None = None, label: str = "",
               ttl_days: int | None = None) -> str:
        sid = session_id or new_session_id()
        now = time.time()
        with self.db.transaction() as cur:
            cur.execute(
                "INSERT INTO context_sessions"
                " (session_id, label, created_at, updated_at, ttl_days)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " label=excluded.label, updated_at=excluded.updated_at,"
                " ttl_days=excluded.ttl_days",
                (sid, label, now, now, int(ttl_days or self.ttl_days)),
            )
        return sid

    def touch(self, session_id: str) -> None:
        with self.db.transaction() as cur:
            cur.execute(
                "UPDATE context_sessions SET updated_at=? WHERE session_id=?",
                (time.time(), session_id),
            )

    # -- lecture ----------------------------------------------------------
    def get(self, session_id: str, include_expired: bool = False) -> Session | None:
        with self.db.cursor(commit=False) as cur:
            row = cur.execute(
                "SELECT session_id, label, created_at, updated_at, ttl_days"
                " FROM context_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        session = Session(
            session_id=row["session_id"],
            label=row["label"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            ttl_days=int(row["ttl_days"]),
        )
        if not include_expired and session.is_expired():
            return None
        return session

    def list(self, include_expired: bool = False) -> list[Session]:
        with self.db.cursor(commit=False) as cur:
            rows = cur.execute(
                "SELECT session_id, label, created_at, updated_at, ttl_days"
                " FROM context_sessions ORDER BY updated_at DESC"
            ).fetchall()
        sessions = [
            Session(
                session_id=r["session_id"],
                label=r["label"],
                created_at=float(r["created_at"]),
                updated_at=float(r["updated_at"]),
                ttl_days=int(r["ttl_days"]),
            )
            for r in rows
        ]
        if include_expired:
            return sessions
        now = time.time()
        return [s for s in sessions if not s.is_expired(now)]

    def stats(self) -> dict[str, Any]:
        sessions = self.list(include_expired=True)
        now = time.time()
        expired = [s for s in sessions if s.is_expired(now)]
        with self.db.cursor(commit=False) as cur:
            chunks = int(cur.execute("SELECT COUNT(*) FROM context_chunks").fetchone()[0])
            sources = int(cur.execute("SELECT COUNT(*) FROM context_sources").fetchone()[0])
        return {
            "sessions": len(sessions),
            "active_sessions": len(sessions) - len(expired),
            "expired_sessions": len(expired),
            "chunks": chunks,
            "sources": sources,
        }

    # -- suppression ------------------------------------------------------
    def purge(self, session_id: str) -> dict[str, Any]:
        """Supprime une session et tout son contenu. Renvoie le compte."""
        with self.db.transaction() as cur:
            chunk_ids = [
                r[0] for r in cur.execute(
                    "SELECT id FROM context_chunks WHERE session_id=?", (session_id,)
                ).fetchall()
            ]
            if chunk_ids:
                placeholders = ",".join("?" * len(chunk_ids))
                cur.execute(
                    f"DELETE FROM context_chunks_fts WHERE rowid IN ({placeholders})",
                    tuple(chunk_ids),
                )
            cur.execute("DELETE FROM context_chunks WHERE session_id=?", (session_id,))
            cur.execute("DELETE FROM context_invariants WHERE session_id=?", (session_id,))
            cur.execute("DELETE FROM context_anchors WHERE session_id=?", (session_id,))
            cur.execute("DELETE FROM context_sources WHERE session_id=?", (session_id,))
            cur.execute("DELETE FROM context_sessions WHERE session_id=?", (session_id,))
        return {"session_id": session_id, "purged": True}

    def purge_expired(self) -> dict[str, Any]:
        """Supprime toutes les sessions expirées."""
        expired = [s.session_id for s in self.list(include_expired=True) if s.is_expired()]
        for sid in expired:
            self.purge(sid)
        return {"purged_sessions": len(expired), "session_ids": expired}
