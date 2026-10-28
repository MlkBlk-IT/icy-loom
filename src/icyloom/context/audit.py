"""Journal d'audit (JSONL, rotation).

Une ligne JSON par événement d'appel outil : horodatage, outil, session,
résultat, durée. La rotation évite qu'un fichier grossisse indéfiniment :
au-delà de la taille maximale, le fichier est renommé et l'ancien est purgé
au-delà de la rétention.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class AuditLog:
    """Écriture d'audit en JSONL avec rotation."""

    def __init__(self, directory: Path, retention_days: int = 30,
                 max_bytes: int = 10 * 1024 * 1024) -> None:
        self.directory = Path(directory)
        self.retention_days = max(1, int(retention_days))
        self.max_bytes = max(4096, int(max_bytes))
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self.directory / "audit.jsonl"

    def record(self, tool: str, session_id: str, outcome: str, latency_ms: float = 0.0,
               error: str | None = None, **extra: Any) -> None:
        """Ajoute une entrée d'audit (échec silencieux : l'audit ne casse rien)."""
        entry = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "tool": tool,
            "session_id": session_id,
            "outcome": outcome,
            "latency_ms": round(float(latency_ms), 3),
        }
        if error:
            entry["error"] = error[:2000]
        if extra:
            entry["extra"] = extra
        try:
            with self._lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _rotate_if_needed(self) -> None:
        if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
            return
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        self.path.rename(self.directory / f"audit-{stamp}.jsonl")
        self.purge_old()

    def purge_old(self) -> int:
        """Supprime les journaux hors rétention. Renvoie le nombre supprimé."""
        if not self.directory.exists():
            return 0
        limit = time.time() - self.retention_days * 86400
        removed = 0
        for candidate in self.directory.glob("audit-*.jsonl"):
            try:
                if candidate.stat().st_mtime < limit:
                    candidate.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def summary(self, limit: int = 200) -> list[dict[str, Any]]:
        """Dernières entrées d'audit."""
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return []
        entries: list[dict[str, Any]] = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
