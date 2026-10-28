"""Extraction des invariants : ce qui doit survivre à la compression.

Un invariant est une **valeur structurée** du texte (nombre, chemin, URL,
version, empreinte, port, date, contrainte explicite). L'ancre est construite
à partir de ces invariants : c'est ce qui permet de garantir qu'aucune
information critique n'est perdue, et donc de compressed sans perte.

Choix de conception (corrige les régressions des outils existants) :

* le **port** est bien produit (kind ``port``), avec son numéro ;
* les **dates** ont leur propre kind (``date``) et ne sont pas confondues
  avec de simples nombres ;
* en profil ``strict``, **aucun invariant n'est écarté** : les kinds rares
  restent présents, quitte à allonger l'ancre. La compression ne doit pas
  coûter de l'information.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

#: Priorités d'affichage : les kinds les plus structurants d'abord.
KIND_PRIORITY = {
    "port": 1,
    "path": 2,
    "url": 3,
    "version": 4,
    "hash": 5,
    "date": 6,
    "constraint": 7,
    "number": 8,
}


@dataclass(frozen=True)
class Invariant:
    """Une valeur à préserver."""

    kind: str
    value: str
    priority: int = 9

    def as_row(self) -> tuple[str, str, int]:
        return (self.kind, self.value, self.priority)


# --- motifs ---------------------------------------------------------------
_URL = re.compile(r"\b[a-z][a-z0-9+.\-]{1,20}://[^\s<>\"']+", re.IGNORECASE)
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:[\\/][^\s<>\"'|]+")
_UNIX_PATH = re.compile(r"(?<![\w.])/(?:[\w.\-]+/)+[\w.\-]*")
_VERSION = re.compile(r"\bv?\d+\.\d+(?:\.\d+){0,3}(?:[-+][\w.]+)?\b")
_SHA = re.compile(r"\b[0-9a-f]{32,64}\b", re.IGNORECASE)
_PORT = re.compile(r"(?i)\b(?:port|listen(?:ing)?(?:\s+on)?|:)\s*(\d{2,5})\b")
_PORT_INLINE = re.compile(r"(?i)\b(?:on|at)\s+(?:port)\s+(\d{2,5})\b")
_DATE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?"
    r"|\d{2}/\d{2}/\d{4}|\d{1,2}\s+(?:janvier|février|mars|avril|mai|juin|juillet|août"
    r"|septembre|octobre|novembre|décembre)[a-z]*\s+\d{4})\b",
    re.IGNORECASE,
)
_NUMBER = re.compile(r"(?<![\w.,])\d+(?:\.\d+)?(?![\w.])")
_CONSTRAINT = re.compile(
    r"(?i)\b(doit|must|ne doit pas|never|always|toujours|jamais|"
    r"interdit|obligatoire|requis|required|forbidden|only|uniquement)\b"
)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = value.strip().rstrip(".,;:!?")
        if not cleaned or cleaned.lower() in seen:
            continue
        seen.add(cleaned.lower())
        out.append(cleaned)
    return out


def extract(text: str, max_invariants: int = 40) -> list[Invariant]:
    """Extrait les invariants d'un texte, triés par priorité de kind."""
    if not text:
        return []

    found: dict[str, list[str]] = {}

    def _add(kind: str, value: str) -> None:
        found.setdefault(kind, []).append(value)

    for match in _URL.finditer(text):
        _add("url", match.group(0))
    for match in _WINDOWS_PATH.finditer(text):
        _add("path", match.group(0))
    for match in _UNIX_PATH.finditer(text):
        candidate = match.group(0)
        # Une URL déjà capturée ne doit pas être reclassée en chemin.
        if not any(candidate in url for url in found.get("url", [])):
            _add("path", candidate)
    for match in _VERSION.finditer(text):
        _add("version", match.group(0))
    for match in _SHA.finditer(text):
        _add("hash", match.group(0))
    for match in _PORT.finditer(text):
        _add("port", match.group(1))
    for match in _PORT_INLINE.finditer(text):
        _add("port", match.group(1))
    for match in _DATE.finditer(text):
        _add("date", match.group(0))
    for match in _CONSTRAINT.finditer(text):
        _add("constraint", match.group(0))

    # Les nombres « nus » : on retire ceux qui appartiennent déjà à une
    # version, un port, une date ou un chemin pour éviter le bruit.
    protected: set[str] = set()
    for kind in ("version", "port", "date", "path", "hash"):
        for value in found.get(kind, []):
            for number in _NUMBER.findall(value):
                protected.add(number)
    for match in _NUMBER.finditer(text):
        value = match.group(0)
        if value in protected:
            continue
        _add("number", value)

    invariants: list[Invariant] = []
    for kind, values in found.items():
        for value in _dedupe(values):
            invariants.append(Invariant(kind=kind, value=value,
                                        priority=KIND_PRIORITY.get(kind, 9)))
    invariants.sort(key=lambda inv: (inv.priority, inv.kind, inv.value))
    return invariants[:max_invariants] if max_invariants > 0 else invariants


def coverage(invariants: Iterable[Invariant], haystack: str) -> dict[str, object]:
    """Proportion d'invariants présents dans `haystack`."""
    invariants = list(invariants)
    if not invariants:
        return {"total": 0, "present": 0, "missing": [], "ratio": 1.0}
    missing = [f"{inv.kind}:{inv.value}" for inv in invariants
               if inv.value.lower() not in haystack.lower()]
    present = len(invariants) - len(missing)
    return {
        "total": len(invariants),
        "present": present,
        "missing": missing,
        "ratio": round(present / len(invariants), 4),
    }
