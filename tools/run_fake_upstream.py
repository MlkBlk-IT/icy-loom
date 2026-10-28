"""Lance le faux amont de la démo (port 8154) — utile pour relancer la démo
sans écraser l'historique de la base.

    .\\.venv\\Scripts\\python.exe tools\\run_fake_upstream.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tests"))

from fake_upstream import FakeUpstream  # noqa: E402

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8154
    upstream = FakeUpstream(port=port).start()
    print(f"faux amont sur http://127.0.0.1:{port} (ctrl+c pour arreter)")
    try:
        while True:
            import time

            time.sleep(1)
    except KeyboardInterrupt:
        upstream.stop()
