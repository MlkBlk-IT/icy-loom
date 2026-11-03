"""Raccourci de développement : délègue au module du paquet ``icyloom.tray``.

Le system tray vit désormais **dans le paquet** (``src/icyloom/tray.py``) pour
être installé avec IcyLoom. Ce fichier reste pour l'usage direct depuis un
checkout : ``python tools\\tray.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from icyloom.tray import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
