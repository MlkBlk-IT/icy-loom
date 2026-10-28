"""Construction de l'ancre : la forme compacte qui reste dans le contexte.

Une ancre est un texte court qui décrit un contenu long. Elle est bâtie à
partir des **invariants** (les valeurs structurées) et non d'un résumé
général : c'est ce qui permet de garantir l'absence de perte.

Trois profils :

``strict`` (défaut)
    Tous les invariants, aucun contenu inventé. Zéro perte.
``balanced``
    Même garantie, mise en forme plus compacte.
``aggressive``
    Résumé lossy. **Jamais activé par défaut**, et toujours signalé.

En profil ``strict`` et ``balanced``, aucun invariant n'est écarté : si la
liste dépasse la taille cible, c'est l'extrait de contenu qui est raccourci,
pas les invariants. C'est l'inverse du comportement fautif des outils
existants, où les kinds rares disparaissaient et où le profil strict
n'était plus lossless.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .anchors import Invariant, extract

#: Profils de compression.
PROFILES: dict[str, dict[str, object]] = {
    "strict": {"max_anchor_chars": 6000, "lossy": False, "head_chars": 800},
    "balanced": {"max_anchor_chars": 3000, "lossy": False, "head_chars": 400},
    "aggressive": {"max_anchor_chars": 1200, "lossy": True, "head_chars": 200},
}

DEFAULT_PROFILE = "strict"


@dataclass(frozen=True)
class Profile:
    """Profil de compression résolu."""

    name: str
    max_anchor_chars: int
    lossy: bool
    head_chars: int

    @classmethod
    def resolve(cls, name: str, max_anchor_chars: int | None = None) -> "Profile":
        raw = PROFILES.get((name or DEFAULT_PROFILE).lower(), PROFILES[DEFAULT_PROFILE])
        return cls(
            name=(name or DEFAULT_PROFILE).lower(),
            max_anchor_chars=int(max_anchor_chars or raw["max_anchor_chars"]),
            lossy=bool(raw["lossy"]),
            head_chars=int(raw["head_chars"]),
        )


def split_units(content: str, target_chars: int = 1200) -> list[str]:
    """Découpe un contenu en fragments cohérents (parAGRaphe puis ligne)."""
    if not content:
        return []
    target = max(200, int(target_chars))
    units: list[str] = []
    current: list[str] = []
    size = 0
    for line in content.splitlines(keepends=True):
        current.append(line)
        size += len(line)
        if size >= target and (line.endswith("\n\n") or size >= target * 2):
            units.append("".join(current))
            current = []
            size = 0
    if current:
        units.append("".join(current))
    # Un contenu sans saut de ligne peut dépasser la cible : on le découpe.
    result: list[str] = []
    for unit in units:
        if len(unit) <= target * 2:
            result.append(unit)
            continue
        for start in range(0, len(unit), target):
            result.append(unit[start:start + target])
    return result


def build_anchor(content: str, profile: Profile | str = DEFAULT_PROFILE,
                 max_invariants: int = 40,
                 invariants: Iterable[Invariant] | None = None) -> tuple[str, list[Invariant]]:
    """Construit l'ancre et retourne (ancre, invariants retenus)."""
    resolved = profile if isinstance(profile, Profile) else Profile.resolve(profile)
    found = list(invariants) if invariants is not None else extract(content, max_invariants)

    lines: list[str] = [f"# ancre ({resolved.name})", f"# taille originale: {len(content)} car."]
    if found:
        lines.append(f"# invariants: {len(found)}")
        for inv in found:
            lines.append(f"- {inv.kind}: {inv.value}")
    else:
        lines.append("# invariants: aucun")
    if resolved.lossy:
        lines.append("# MODE LOSSY : le contenu a été résumé, des informations peuvent manquer.")

    # Extrait de contenu : raccourci pour tenir dans le budget, JAMAIS les
    # invariants (c'est la garantie lossless).
    head = content[:resolved.head_chars]
    lines.append("# extrait:")
    lines.append(head.rstrip())

    anchor = "\n".join(lines)
    if not resolved.lossy and len(anchor) > resolved.max_anchor_chars:
        # On rogne l'extrait, pas les invariants.
        overhead = len(anchor) - len(head.rstrip())
        allowed = max(0, resolved.max_anchor_chars - overhead)
        if allowed < len(head.rstrip()):
            lines[-1] = head[:allowed].rstrip()
            anchor = "\n".join(lines)
    return anchor, found
