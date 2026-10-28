"""Rédaction des secrets avant stockage.

Un contenu rangé dans le tiroir ne doit jamais contenir de secret en clair :
clé d'API, jeton, mot de passe. La rédaction est appliquée **avant** le
découpage, et le nombre de substitutions est journalisé (l'appelant peut
l'annoncer à l'agent).

Le motif de détection est volontairement conservateur : on masque ce qui est
sans ambiguïté un secret, on ne masque pas les mots ordinaires.
"""

from __future__ import annotations

import re
from typing import Iterable

#: (nom du motif, expression régulière)
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bearer", re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._\-]{8,})")),
    ("sk_key", re.compile(r"\b(sk-[A-Za-z0-9._\-]{12,})\b")),
    ("gh_token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,})\b")),
    ("aws_key", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
    ("password", re.compile(r"(?i)\b(password|passwd|pwd)\s*[=:]\s*(\S{4,})")),
    ("secret", re.compile(r"(?i)\b(secret|api[_-]?key|access[_-]?token|auth[_-]?token)\s*[=:]\s*(\S{4,})")),
    ("jwt", re.compile(r"\b(eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})\b")),
    ("url_credentials", re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^\s/:@]+):([^\s/@]{3,})@")),
)

#: Chaîne de remplacement : on garde le nom du champ, on efface la valeur.
_MASK = "[REDACTED]"


def redact_text(text: str) -> tuple[str, int]:
    """Masque les secrets. Renvoie (texte redacté, nombre de substitutions)."""
    if not text:
        return text, 0
    count = 0

    def _replace_named(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)} {_MASK}"

    def _replace_whole(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return _MASK

    def _replace_url(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group(1)}:{_MASK}@"

    for name, pattern in _PATTERNS:
        if name == "bearer":
            text = pattern.sub(_replace_named, text)
        elif name in ("password", "secret"):
            text = pattern.sub(_replace_named, text)
        elif name == "url_credentials":
            text = pattern.sub(_replace_url, text)
        else:
            text = pattern.sub(_replace_whole, text)
    return text, count


def redact_chunks(chunks: Iterable[str]) -> tuple[list[str], int]:
    """Applique la rédaction à une liste de fragments."""
    out: list[str] = []
    total = 0
    for chunk in chunks:
        redacted, count = redact_text(chunk)
        out.append(redacted)
        total += count
    return out, total


def contains_secret(text: str) -> bool:
    """Vrai si le texte contient un motif de secret (contrôle de non-régression)."""
    for _name, pattern in _PATTERNS:
        if pattern.search(text):
            return True
    return False
