"""Cache LLM : stockage SQLite, expiration (TTL), éviction LRU.

Le cache est **une seule table** de la base unifiée. Les entrées sont
adressées par la clé canonique calculée par le moteur de normalisation :
deux requêtes équivalentes partagent donc la même entrée.

Politique d'éviction : le plafond d'entrées est respecté en retirant les
entrées les moins récemment utilisées (``last_accessed``). Une entrée
expirée est ignorée à la lecture et purgée par ``purge_expired``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .db import Database


@dataclass(frozen=True)
class CacheEntry:
    """Une entrée de cache lue depuis la base."""

    key: str
    normalized: str
    original: str | None
    response: str
    is_stream: bool
    provider: str | None
    model: str | None
    created_at: str
    last_accessed: str
    access_count: int
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def payload(self) -> Any:
        """Réponse décodée (JSON si possible, sinon texte brut)."""
        try:
            return json.loads(self.response)
        except (json.JSONDecodeError, TypeError):
            return self.response


class CacheStore:
    """Interface du cache LLM sur la base unifiée."""

    def __init__(self, database: Database, ttl_seconds: int = 604800,
                 max_entries: int = 50000) -> None:
        self.db = database
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries

    # -- lecture ----------------------------------------------------------
    def get(self, key: str, bump: bool = True) -> CacheEntry | None:
        """Entrée non expirée pour `key` (None si absente ou expirée).

        `bump` incrémente le compteur d'accès et l'horodatage de dernier
        usage. L'appelant qui possède déjà une transaction (la passerelle)
        passe `bump=False` et appelle `bump_access` dans *sa* transaction :
        une seule écriture WAL par requête.
        """
        with self.db.cursor(commit=False) as cur:
            row = cur.execute(
                "SELECT key, normalized, original, response, is_stream, provider, model,"
                " created_at, last_accessed, access_count, prompt_tokens, completion_tokens"
                " FROM cache_entries WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            if self._expired(str(row["created_at"])):
                return None
        if bump:
            self.touch(key)
        return CacheEntry(
            key=row["key"],
            normalized=row["normalized"],
            original=row["original"],
            response=row["response"],
            is_stream=bool(row["is_stream"]),
            provider=row["provider"],
            model=row["model"],
            created_at=row["created_at"],
            last_accessed=row["last_accessed"],
            access_count=int(row["access_count"]),
            prompt_tokens=int(row["prompt_tokens"] or 0),
            completion_tokens=int(row["completion_tokens"] or 0),
        )

    def touch(self, key: str) -> None:
        """Compteur d'accès + horodatage, dans une transaction dédiée."""
        with self.db.transaction() as cur:
            self.bump_access(cur, key)

    def bump_access(self, cur: Any, key: str) -> None:
        """Incrémente le compteur d'accès sur un curseur déjà ouvert.

        Utilisé par la passerelle : le journal, le ledger et ce compteur
        partagent alors **une seule** transaction.
        """
        cur.execute(
            "UPDATE cache_entries SET access_count = access_count + 1,"
            " last_accessed = datetime('now') WHERE key = ?",
            (key,),
        )

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Recherche d'entrées par fragment du texte normalisé."""
        limit = max(1, min(int(limit), 100))
        with self.db.cursor(commit=False) as cur:
            rows = cur.execute(
                "SELECT key, normalized, provider, model, created_at, access_count"
                " FROM cache_entries WHERE normalized LIKE ?"
                " ORDER BY access_count DESC, last_accessed DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
        return [
            {
                "key": r["key"],
                "normalized": r["normalized"],
                "provider": r["provider"],
                "model": r["model"],
                "created_at": r["created_at"],
                "access_count": r["access_count"],
            }
            for r in rows
        ]

    # -- écriture ---------------------------------------------------------
    def set(
        self,
        key: str,
        normalized: str,
        response: str,
        original: str | None = None,
        is_stream: bool = False,
        provider: str | None = None,
        model: str | None = None,
        raw_hash: str | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        """Enregistre une réponse et applique la politique d'éviction."""
        with self.db.transaction() as cur:
            cur.execute(
                "INSERT INTO cache_entries"
                " (key, normalized, original, response, is_stream, provider, model, raw_hash,"
                "  prompt_tokens, completion_tokens)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET"
                " normalized=excluded.normalized, original=excluded.original,"
                " response=excluded.response, is_stream=excluded.is_stream,"
                " provider=excluded.provider, model=excluded.model,"
                " prompt_tokens=excluded.prompt_tokens,"
                " completion_tokens=excluded.completion_tokens,"
                " created_at=datetime('now'), last_accessed=datetime('now'),"
                " access_count=cache_entries.access_count + 1",
                (key, normalized, original, response, int(bool(is_stream)), provider, model,
                 raw_hash, int(prompt_tokens), int(completion_tokens)),
            )
            total = int(cur.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0])
            if total > self.max_entries:
                excess = total - self.max_entries
                cur.execute(
                    "DELETE FROM cache_entries WHERE key IN ("
                    " SELECT key FROM cache_entries ORDER BY last_accessed ASC LIMIT ?)",
                    (excess,),
                )

    # -- maintenance ------------------------------------------------------
    def clear(self, key: str | None = None) -> int:
        """Vide tout le cache ou une entrée précise. Renvoie le nombre supprimé."""
        with self.db.transaction() as cur:
            if key:
                cur.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
                return int(cur.rowcount or 0)
            cur.execute("DELETE FROM cache_entries")
            return int(cur.rowcount or 0)

    def purge_expired(self) -> int:
        """Supprime les entrées plus anciennes que le TTL."""
        with self.db.transaction() as cur:
            cur.execute(
                "DELETE FROM cache_entries WHERE created_at < datetime('now', ?)",
                (f"-{int(self.ttl_seconds)} seconds",),
            )
            return int(cur.rowcount or 0)

    def stats(self) -> dict[str, Any]:
        """Compteurs d'usage (taux de succès = entrées réellement servies)."""
        with self.db.cursor(commit=False) as cur:
            total = int(cur.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0])
            hits = int(cur.execute(
                "SELECT COALESCE(SUM(access_count - 1), 0) FROM cache_entries"
            ).fetchone()[0] or 0)
            by_provider = {
                r[0]: r[1]
                for r in cur.execute(
                    "SELECT provider, COUNT(*) FROM cache_entries GROUP BY provider"
                ).fetchall()
            }
            by_model = {
                r[0]: r[1]
                for r in cur.execute(
                    "SELECT model, COUNT(*) FROM cache_entries GROUP BY model"
                    " ORDER BY COUNT(*) DESC LIMIT 20"
                ).fetchall()
            }
            oldest = cur.execute(
                "SELECT MIN(created_at) FROM cache_entries"
            ).fetchone()[0]
        return {
            "entries": total,
            "reuse_count": hits,
            "by_provider": by_provider,
            "by_model": by_model,
            "oldest_entry": oldest,
            "ttl_seconds": self.ttl_seconds,
            "max_entries": self.max_entries,
        }

    # -- interne ----------------------------------------------------------
    def _expired(self, created_at: str) -> bool:
        try:
            with self.db.cursor(commit=False) as cur:
                row = cur.execute(
                    "SELECT (julianday('now') - julianday(?)) * 86400 AS age FROM ?",
                    (created_at, created_at),
                ).fetchone()
            age = float(row["age"]) if row and row["age"] is not None else 0.0
        except sqlite3.Error:
            return False
        return age > self.ttl_seconds
