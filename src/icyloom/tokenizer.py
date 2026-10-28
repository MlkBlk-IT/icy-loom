"""Comptage de tokens local.

Deux compteurs possibles :

* **tiktoken** (encodeur d'un fournisseur réel) — précis, mais il peut
  nécessiter un téléchargement du fichier d'encodage au premier usage ;
* **estimateur** (repli local, ~4 caractères par token) — toujours
  disponible, approximatif.

Le compteur réellement utilisé est signalé à chaque fois : les chiffres
issus d'un estimateur ne doivent jamais être confondus avec des tokens
remontés par un fournisseur.

À noter : la compression raisonne sur des tokens **locaux**. Les tokens
facturés, eux, ne sont comptés que s'ils viennent de l'amont.
"""

from __future__ import annotations

import functools
import logging
import threading

log = logging.getLogger("icyloom.tokenizer")

#: Nom du compteur actif ("tiktoken:cl100k_base" ou "heuristic").
COUNTER_NAME = "heuristic"

_encoder = None
_encoder_lock = threading.Lock()


def _load_encoder():
    """Charge l'encodeur tiktoken (silencieux en cas d'échec réseau)."""
    global _encoder, COUNTER_NAME
    with _encoder_lock:
        if _encoder is not None:
            return _encoder
        try:
            import tiktoken

            _encoder = tiktoken.get_encoding("cl100k_base")
            COUNTER_NAME = "tiktoken:cl100k_base"
        except Exception as exc:  # pragma: no cover - dépend du réseau/cache
            log.warning("tiktoken indisponible (%s) : estimateur local utilisé", exc)
            _encoder = None
            COUNTER_NAME = "heuristic"
        return _encoder


@functools.lru_cache(maxsize=1)
def counter_name() -> str:
    """Nom du compteur de tokens effectivement utilisé."""
    _load_encoder()
    return COUNTER_NAME


def count_tokens(text: str) -> int:
    """Nombre de tokens d'un texte."""
    if not text:
        return 0
    encoder = _load_encoder()
    if encoder is not None:
        try:
            return len(encoder.encode(text, disallowed_special=()))
        except Exception:  # pragma: no cover
            pass
    return _estimate(text)


def _estimate(text: str) -> int:
    """Estimateur local : ~4 caractères par token, minimum 1."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)
