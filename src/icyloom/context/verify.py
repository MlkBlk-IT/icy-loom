"""Vérification de couverture : la preuve que rien d'important n'est perdu.

``verify_anchor`` compare la liste des invariants d'un contenu à l'ancre
construite : chaque invariant doit se retrouver dans l'ancre. Un écart est
un **bug**, pas une approximation : il est remonté explicitement.

``verify_roundtrip`` vérifie que les fragments stockés redonnent exactement
le contenu d'origine, octet pour octet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .anchors import Invariant, coverage


@dataclass(frozen=True)
class VerificationResult:
    """Résultat d'une vérification."""

    ok: bool
    total: int
    present: int
    missing: list[str]
    ratio: float
    roundtrip_ok: bool | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "invariants_total": self.total,
            "invariants_present": self.present,
            "invariants_missing": self.missing,
            "coverage": self.ratio,
            "roundtrip_ok": self.roundtrip_ok,
        }


def verify_anchor(invariants: Iterable[Invariant], anchor: str) -> VerificationResult:
    """Vérifie que l'ancre contient tous les invariants."""
    report = coverage(invariants, anchor)
    missing = list(report["missing"])  # type: ignore[arg-type]
    return VerificationResult(
        ok=not missing,
        total=int(report["total"]),
        present=int(report["present"]),
        missing=missing,
        ratio=float(report["ratio"]),
    )


def verify_roundtrip(original: str, chunks: Sequence[str]) -> bool:
    """Vrai si la concaténation des fragments redonne le texte d'origine."""
    return "".join(chunks) == original
