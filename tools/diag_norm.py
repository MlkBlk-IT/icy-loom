"""Diagnostic : écart entre le moteur courant et le cache historique."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from icyloom.normalize import Canonicalizer  # noqa: E402

c = Canonicalizer(rules_path=Path(r"D:\ICY-LOOM\data\normalization_rules.json"))
conn = sqlite3.connect("file:D:/IcyCache/db/cache.db?mode=ro", uri=True)
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
