"""Est-ce qu'un vrai client MCP peut utiliser IcyLoom ?

Ce script **joue le rôle du client** (Claude Code, Cursor, OpenAI…) : il
démarre le serveur exactement comme un client le ferait, puis appelle les
outils.

    .\\.venv\\Scripts\\python.exe tools\\check_client.py

Équivalent CLI : ``icyloom check`` (voir ``icyloom\clientcheck.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from icyloom.clientcheck import TRANSPORTS, client_config, run_checks  # noqa: E402

OK = "\033[32m✔\033[0m"
KO = "\033[31m✘\033[0m"


def main() -> int:
    print("\033[1mIcyLoom — test comme un vrai client MCP\033[0m")
    ok, checks = run_checks()
    for flag, label in checks:
        print(f"  {OK if flag else KO} {label}")
    failed = [label for flag, label in checks if not flag]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} vérifications réussies")
    print("\n\033[1mConfiguration à coller dans votre client\033[0m")
    import json

    print(json.dumps(client_config(), indent=2, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
