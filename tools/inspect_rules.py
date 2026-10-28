"""Inspection des règles de normalisation (outil de travail, hors paquet)."""
import json
import pathlib
import re
import sys

p = pathlib.Path(r"D:\ICY-LOOM\data\normalization_rules.json")
data = json.loads(p.read_text(encoding="utf-8"))
rules = data["rules"]
print("racine:", list(data.keys()))
print("nb rules:", len(rules))
print("exemple:", json.dumps(rules[0], ensure_ascii=False))
kinds = {}
for r in rules:
    kinds[r.get("kind", r.get("type", "?"))] = kinds.get(r.get("kind", r.get("type", "?")), 0) + 1
print("kinds:", kinds)

for sym in "+*/=<>":
    hits = [r for r in rules if sym in str(r.get("pattern", ""))]
    print(f"pattern contient {sym!r}: {len(hits)} -> {hits[:4]}")

# regles dont le pattern est un symbole seul
solo = [r for r in rules if len(str(r.get("pattern", ""))) == 1 and not str(r.get("pattern", "")).isalnum()]
print("patterns symboles seuls:", len(solo), solo[:12])

# les nombres-mots
words = [r for r in rules if str(r.get("pattern", "")).lower() in
         {"one","two","three","four","five","six","seven","eight","nine","ten","zero"}]
print("regles nombres-mots:", len(words), words[:4])
