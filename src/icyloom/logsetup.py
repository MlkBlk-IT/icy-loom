"""Journalisation : tout sur stderr, jamais sur stdout.

Le transport stdio du MCP réserve **stdout** au JSON-RPC : une seule ligne de
log sur stdout casse la session. On force aussi l'UTF-8 explicite (le code
Windows `cp1252` fait échouer un accent écrit sur stdout).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

LOGGER_NAME = "icyloom"


def force_utf8() -> None:
    """Force l'encodage UTF-8 sur stdout/stderr (stderr)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="strict")
        except (AttributeError, ValueError):  # pragma: no cover - flux non reconfigurable
            pass


def configure(level: str = "INFO", logfile: Path | None = None) -> logging.Logger:
    """Configure le logger racine d'IcyLoom (console sur stderr + fichier)."""
    force_utf8()
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)

    if logfile is not None:
        try:
            logfile.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(logfile, encoding="utf-8")
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except OSError:  # pragma: no cover - fichier de log non inscriptible
            pass
    return logging.getLogger(LOGGER_NAME)
