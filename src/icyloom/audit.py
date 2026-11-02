"""Trace d'audit : qui demande quoi, et pourquoi le proxy a décidé ainsi.

Pour chaque invocation d'outil on conserve : l'acteur et son rôle,
l'intention, l'outil appelé, l'empreinte des arguments, la décision de
politique et son motif, ce que le proxy a compressé, et le résultat final.

C'est ce qui permet de corriger une mauvaise sélection d'outil, d'auditer
les actions et de mesurer le coût réel.
"""

from __future__ import annotations

import json
from typing import Any

from .db import Database


class AuditTrail:
    """Journal d'audit des invocations d'outils."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, *, actor: str = "", role: str = "", intent: str = "",
               server: str = "", tool: str = "", args_hash: str = "",
               decision: str = "", reason: str = "", compacted: str = "",
               tokens_in: int = 0, tokens_out: int = 0, tokens_saved: int = 0,
               latency_ms: int = 0, detail: str = "") -> None:
        """Écrit une ligne d'audit (jamais bloquante pour l'appel en cours)."""
        try:
            with self.db.transaction() as cur:
                cur.execute(
                    "INSERT INTO tool_audit (ts,actor,role,intent,server,tool,args_hash,"
                    "decision,reason,compacted,tokens_in,tokens_out,tokens_saved,"
                    "latency_ms,detail) VALUES (datetime('now'),?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (actor, role, intent, server, tool, args_hash, decision, reason,
                     compacted, int(tokens_in), int(tokens_out), int(tokens_saved),
                     int(latency_ms), detail),
                )
        except Exception:      # pragma: no cover - l'audit ne doit rien casser
            pass

    def tail(self, limit: int = 50, *, server: str = "", tool: str = "") -> list[dict[str, Any]]:
        """Dernières lignes d'audit."""
        sql = "SELECT * FROM tool_audit"
        clauses: list[str] = []
        params: list[Any] = []
        if server:
            clauses.append("server=?")
            params.append(server)
        if tool:
            clauses.append("tool=?")
            params.append(tool)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with self.db.cursor(commit=False) as cur:
            rows = cur.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        """Répartition des décisions : le meilleur indicateur de santé du proxy."""
        with self.db.cursor(commit=False) as cur:
            total = cur.execute("SELECT COUNT(*) FROM tool_audit").fetchone()[0]
            by_decision = cur.execute(
                "SELECT decision, COUNT(*) AS n FROM tool_audit GROUP BY decision"
            ).fetchall()
            by_tool = cur.execute(
                "SELECT server, tool, COUNT(*) AS n FROM tool_audit"
                " GROUP BY server, tool ORDER BY n DESC LIMIT 10"
            ).fetchall()
            saved = cur.execute(
                "SELECT COALESCE(SUM(tokens_saved),0) FROM tool_audit"
            ).fetchone()[0]
        return {
            "entries": int(total),
            "tokens_saved": int(saved or 0),
            "by_decision": {row["decision"]: int(row["n"]) for row in by_decision},
            "top_tools": [{"server": row["server"], "tool": row["tool"],
                           "calls": int(row["n"])} for row in by_tool],
        }

    def export(self, path: str) -> str:
        """Export JSONL des lignes d'audit (pour une revue externe)."""
        with open(path, "w", encoding="utf-8") as handle:
            for row in self.tail(limit=100000):
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path
