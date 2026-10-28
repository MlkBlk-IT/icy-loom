"""Ledger : une seule source de vérité sur ce qui coûte et ce qui économise.

Chaque événement économique (appel LLM, succès de cache, compression de
contexte, appel d'outil) écrit **une** ligne dans ``ledger_entries``. La
question « combien de tokens m'ont-ils coûté, et combien m'ont-ils fait
gagner ? » se répond alors avec une seule requête.

Vocabulaire :

``tokens_in`` / ``tokens_out``
    Tokens **réels** remontés par l'amont (``usage`` de la réponse). Jamais
    estimés : si l'amont n'envoie pas de ``usage``, la ligne porte 0 et non
    une approximation.
``tokens_saved``
    Tokens réels qui n'ont **pas** été payés parce qu'un cache a servi la
    réponse, ou parce qu'une compression a retiré le contenu du contexte.
``tool_tokens_saved``
    Tokens évités côté outils (résultat mis en cache et rejoué).

Le gain se calcule ainsi : sans IcyLoom, le même travail aurait coûté
``payé + économisé``. D'où ``gain = économisé / (payé + économisé)``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .db import Database

#: Seuils d'exploitation retenus par la spec : le système est utile si le
#: gain mesuré dépasse ce seuil.
DEFAULT_THRESHOLD_PCT = 35.0


@dataclass
class LedgerEntry:
    """Une ligne de ledger, prête à être écrite."""

    kind: str
    session_id: str | None = None
    client: str | None = None
    upstream: str | None = None
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_saved: int = 0
    tool_tokens_saved: int = 0
    latency_ms: int = 0
    saved_latency_ms: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


class Ledger:
    """Écriture et lecture du journal économique."""

    def __init__(self, database: Database, enabled: bool = True) -> None:
        self.db = database
        self.enabled = enabled

    def record(self, entry: LedgerEntry) -> None:
        """Ajoute une ligne au ledger (no-op si le ledger est désactivé)."""
        if not self.enabled:
            return
        with self.db.transaction() as cur:
            cur.execute(
                "INSERT INTO ledger_entries"
                " (kind, session_id, client, upstream, model, tokens_in, tokens_out,"
                "  tokens_saved, tool_tokens_saved, latency_ms, saved_latency_ms, meta)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry.kind,
                    entry.session_id,
                    entry.client,
                    entry.upstream,
                    entry.model,
                    int(entry.tokens_in),
                    int(entry.tokens_out),
                    int(entry.tokens_saved),
                    int(entry.tool_tokens_saved),
                    int(entry.latency_ms),
                    int(entry.saved_latency_ms),
                    json.dumps(entry.meta, ensure_ascii=False) if entry.meta else None,
                ),
            )

    # -- agrégats ---------------------------------------------------------
    def summary(self, threshold_pct: float = DEFAULT_THRESHOLD_PCT) -> dict[str, Any]:
        """Bilan global : payé, économisé, gain, et franchissement du seuil."""
        with self.db.cursor(commit=False) as cur:
            row = cur.execute(
                "SELECT"
                " COALESCE(SUM(tokens_in), 0)          AS tokens_in,"
                " COALESCE(SUM(tokens_out), 0)         AS tokens_out,"
                " COALESCE(SUM(tokens_saved), 0)       AS tokens_saved,"
                " COALESCE(SUM(tool_tokens_saved), 0) AS tool_tokens_saved,"
                " COALESCE(SUM(latency_ms), 0)        AS latency_ms,"
                " COALESCE(SUM(saved_latency_ms), 0)  AS saved_latency_ms,"
                " COUNT(*)                             AS entries"
                " FROM ledger_entries"
            ).fetchone()
            by_kind = cur.execute(
                "SELECT kind, COUNT(*) AS n, COALESCE(SUM(tokens_saved), 0) AS saved,"
                " COALESCE(SUM(tokens_in + tokens_out), 0) AS spent"
                " FROM ledger_entries GROUP BY kind ORDER BY n DESC"
            ).fetchall()
            by_upstream = cur.execute(
                "SELECT upstream, COUNT(*) AS n,"
                " COALESCE(SUM(tokens_in + tokens_out), 0) AS spent,"
                " COALESCE(SUM(tokens_saved), 0) AS saved"
                " FROM ledger_entries WHERE upstream IS NOT NULL"
                " GROUP BY upstream ORDER BY spent DESC"
            ).fetchall()
            recent = cur.execute(
                "SELECT ts, kind, upstream, model, tokens_in, tokens_out, tokens_saved,"
                " tool_tokens_saved, latency_ms FROM ledger_entries ORDER BY id DESC LIMIT 50"
            ).fetchall()

        paid = int(row["tokens_in"]) + int(row["tokens_out"])
        saved = int(row["tokens_saved"]) + int(row["tool_tokens_saved"])
        gain = (saved / (paid + saved) * 100.0) if (paid + saved) else 0.0
        return {
            "entries": int(row["entries"]),
            "tokens_paid": paid,
            "tokens_in": int(row["tokens_in"]),
            "tokens_out": int(row["tokens_out"]),
            "tokens_saved": saved,
            "tool_tokens_saved": int(row["tool_tokens_saved"]),
            "latency_ms": int(row["latency_ms"]),
            "saved_latency_ms": int(row["saved_latency_ms"]),
            "gain_pct": round(gain, 2),
            "threshold_pct": threshold_pct,
            "threshold_met": gain >= threshold_pct,
            "by_kind": [dict(r) for r in by_kind],
            "by_upstream": [dict(r) for r in by_upstream],
            "recent": [dict(r) for r in recent],
            "tokens_source": "usage amont (réels). Aucune estimation.",
        }

    def daily(self, days: int = 14) -> list[dict[str, Any]]:
        """Séries journalières (paid / saved / gain) sur `days` jours."""
        days = max(1, min(int(days), 365))
        with self.db.cursor(commit=False) as cur:
            rows = cur.execute(
                "SELECT date(ts) AS day,"
                " COALESCE(SUM(tokens_in + tokens_out), 0) AS paid,"
                " COALESCE(SUM(tokens_saved + tool_tokens_saved), 0) AS saved,"
                " COUNT(*) AS entries"
                " FROM ledger_entries WHERE ts >= datetime('now', ?)"
                " GROUP BY day(ts) ORDER BY day",
                (f"-{days} days",),
            ).fetchall()
        series = []
        for r in rows:
            paid = int(r["paid"])
            saved = int(r["saved"])
            total = paid + saved
            series.append({
                "day": r["day"],
                "paid": paid,
                "saved": saved,
                "entries": int(r["entries"]),
                "gain_pct": round(saved / total * 100.0, 2) if total else 0.0,
            })
        return series
