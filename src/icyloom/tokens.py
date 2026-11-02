"""Répartition des tokens par catégorie : où part vraiment l'argent.

Six catégories :

``system_prompt``  le prompt système envoyé au modèle ;
``tool_schemas``   les schémas d'outils exposés au modèle ;
``tool_results``   les résultats d'outils versés dans l'historique ;
``cache_read``     les tokens relus depuis le cache (donc pas facturés) ;
``cache_write``    les tokens écrits dans le cache ;
``llm_calls``      les appels LLM eux-mêmes.

Le compteur vit dans le ledger : on additionne ce qu'IcyLoom a réellement
observé, pas une estimation de labeur.
"""

from __future__ import annotations

from typing import Any

from .db import Database
from .ledger import Ledger, LedgerEntry

#: Catégories reconnues.
CATEGORIES = ("system_prompt", "tool_schemas", "tool_results",
              "cache_read", "cache_write", "llm_calls")


class TokenMeter:
    """Compteur de tokens par catégorie, adossé au ledger."""

    def __init__(self, db: Database, ledger: Ledger | None = None) -> None:
        self.db = db
        self.ledger = ledger

    def record(self, category: str, tokens: int, **meta: Any) -> None:
        """Enregistre ``tokens`` dans une catégorie."""
        if category not in CATEGORIES:
            raise ValueError(f"catégorie inconnue : {category}")
        tokens = int(tokens)
        if tokens <= 0:
            return
        if self.ledger is not None:
            self.ledger.record(LedgerEntry(kind=f"token_{category}",
                                           tokens_in=tokens, meta=meta))
            return
        import json

        with self.db.transaction() as cur:
            cur.execute(
                "INSERT INTO ledger_entries (ts,kind,tokens_in,meta)"
                " VALUES (datetime('now'),?,?,?)",
                (f"token_{category}", tokens, json.dumps(meta, ensure_ascii=False)),
            )

    def summary(self, days: int = 30) -> dict[str, Any]:
        """Répartition des tokens par catégorie sur la période."""
        with self.db.cursor(commit=False) as cur:
            rows = cur.execute(
                "SELECT kind, COALESCE(SUM(tokens_in),0) AS tokens FROM ledger_entries"
                " WHERE kind LIKE 'token_%'"
                " AND ts >= datetime('now', ?) GROUP BY kind",
                (f"-{int(days)} days",),
            ).fetchall()
        totals = {category: 0 for category in CATEGORIES}
        for row in rows:
            category = row["kind"][len("token_"):]
            if category in totals:
                totals[category] = int(row["tokens"])
        total = sum(totals.values())
        return {
            "days": int(days),
            "total": total,
            "by_category": totals,
            "share_pct": {category: (round(100.0 * value / total, 2) if total else 0.0)
                          for category, value in totals.items()},
        }
