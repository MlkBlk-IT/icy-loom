"""Diagnostic : écart entre le moteur courant et un cache historique.

Usage :
    python tools/diag_norm.py <chemin/vers/cache.db>

La base est lue en lecture seule. Sans argument, le script compare le moteur
courant aux entrées du fichier de test de la suite.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from icyloom.normalize import Canonicalizer  # noqa: E402
from icyloom.paths import data_file  # noqa: E402

c = Canonicalizer(rules_path=data_file("normalization_rules.json"))

if len(sys.argv) < 2:
    print("usage : python tools/diag_norm.py <chemin/vers/cache.db>")
    raise SystemExit(2)

conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
rows = conn.execute(
    "SELECT original_prompt, normalized_prompt FROM cache "
    "WHERE original_prompt IS NOT NULL LIMIT 400").fetchall()
conn.close()

same = 0
diffs = []
for orig, stored in rows:
    got = c.fold_text(orig)
    if got == stored:
        same += 1
    elif len(diffs) < 14:
        diffs.append((orig, stored, got))

print(f"identiques : {same}/{len(rows)} = {same/len(rows):.1%}\n")
for orig, stored, got in diffs:
    print("original   :", repr(orig[:90]))
    print("  stocké   :", repr(stored[:90]))
    print("  recalculé:", repr(got[:90]))
    print()
