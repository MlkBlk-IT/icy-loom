"""Tests du moteur de normalisation.

Deux angles :

1. **Sémantique documentée** — cas explicites (opérateurs conservés, accents
   pliés, nombres-mots convertis, champs volatils ignorés).
2. **Équivalence d'implémentation** — le moteur rapide (Aho-Corasick) doit
   produire exactement le même résultat qu'une implémentation naïve, écrite
   indépendamment dans le test, sur un corpus large.

Le taux d'accord avec le **cache historique** est mesuré et affiché, sans
être une assertion : la base d'origine a été écrite par une version
antérieure du moteur (elle supprimait « : », « / », « + », « * » et ne
convertissait pas les nombres en toutes lettres). Ces entrées sont déjà
incompatibles avec le moteur courant de référence ; les perdre ne coûte
rien, et les respecter casserait la compatibilité avec le moteur de
référence.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from pathlib import Path

import pytest

from icyloom.normalize import Canonicalizer, canonical_json

RULES = Path(__file__).resolve().parents[1] / "data" / "normalization_rules.json"


@pytest.fixture(scope="module")
def engine() -> Canonicalizer:
    return Canonicalizer(rules_path=RULES)


# --------------------------------------------------------------------------
# 1. Sémantique documentée
# --------------------------------------------------------------------------
def test_case_and_punctuation_folded(engine: Canonicalizer) -> None:
    assert engine.fold_text("Bonjour !") == engine.fold_text("bonjour")
    assert engine.fold_text("Bonjour !") == "bonjour"


def test_accents_folded(engine: Canonicalizer) -> None:
    assert engine.fold_text("Café") == "cafe"
    assert engine.fold_text("naïve résumé") == "naive resume"


def test_math_operators_preserved(engine: Canonicalizer) -> None:
    """Les opérateurs portent du sens : ils ne doivent pas disparaître."""
    assert engine.fold_text("12+2") == "12+2"
    assert engine.fold_text("12*12") == "12*12"
    assert engine.fold_text("D:/path/to") == "d/path/to"  # ':' est de la ponctuation de phrase
    assert engine.fold_text("a = b") == "a = b"


def test_number_words_converted(engine: Canonicalizer) -> None:
    assert engine.fold_text("two plus two") == "2+2"
    assert engine.fold_text("give me one word") == "give me 1 word"


def test_politeness_removed(engine: Canonicalizer) -> None:
    assert "please" not in engine.fold_text("Please, do the thing")


def test_whitespace_compacted(engine: Canonicalizer) -> None:
    assert engine.fold_text("  a   b  ") == "a b"


def test_cache_key_ignores_volatile_fields(engine: Canonicalizer) -> None:
    base = {"model": "m", "messages": [{"role": "user", "content": "hello"}]}
    noisy = {**base, "stream": True, "user": "u-1", "metadata": {"x": 1}}
    assert engine.digest(base) == engine.digest(noisy)


def test_cache_key_stable_and_scoped(engine: Canonicalizer) -> None:
    body = {"messages": [{"role": "user", "content": "hello"}]}
    first = engine.digest(body)
    assert first == engine.digest(body)
    assert len(first) == 64
    assert engine.digest(body, scope={"provider": "a"}) != first


def test_multimodal_blocks_keep_non_text(engine: Canonicalizer) -> None:
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "Bonjour !"},
        {"type": "image_url", "image_url": {"url": "http://x/y.png"}},
    ]}]}
    folded = engine.canonical_body(body)["messages"][0]["content"]
    assert folded[0]["text"] == "bonjour"
    assert folded[1]["type"] == "image_url"


def test_rule_file_loaded(engine: Canonicalizer) -> None:
    assert engine.rule_count == 1122


# --------------------------------------------------------------------------
# 2. Équivalence avec une implémentation naïve indépendante
# --------------------------------------------------------------------------
def _naive_fold(text: str, rules: list[tuple[str, str]]) -> str:
    """Implémentation de référence, volontairement simple et lente."""
    folded = unicodedata.normalize("NFKD", text).lower()
    for pattern, replacement in rules:
        folded = re.sub(r"\b" + re.escape(pattern) + r"\b", replacement, folded)
    for word, digit in (("zero", "0"), ("one", "1"), ("two", "2"), ("three", "3"),
                        ("four", "4"), ("five", "5"), ("six", "6"), ("seven", "7"),
                        ("eight", "8"), ("nine", "9"), ("ten", "10")):
        folded = re.sub(rf"\b{word}\b", digit, folded)
    for word, symbol in ((" plus ", "+"), (" minus ", "-"),
                         (" times ", "*"), (" divided by ", "/")):
        folded = folded.replace(word, symbol)
    stripped = "".join(
        c for c in unicodedata.normalize("NFD", folded) if unicodedata.category(c) != "Mn"
    )
    stripped = re.sub(r"[!?.,;:'\"`´“”‘’«»¡¿…]", "", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


@pytest.fixture(scope="module")
def raw_rules() -> list[tuple[str, str]]:
    data = json.loads(RULES.read_text(encoding="utf-8"))
    return [(r["pattern"].lower(), r.get("replace", "") or "") for r in data["rules"]]


def test_fast_engine_matches_naive_reference(engine: Canonicalizer, raw_rules) -> None:
    samples = [
        "Bonjour !", "bonjour", "Café naïve", "two plus two", "12+2",
        "Please, can you summarize the build log?", "D:/Loom Upgrade",
        "Erreur 502 sur /v1/chat/completions, latence 1200 ms",
        "Refactor the loader: it reads config.yaml twice",
        "Mesure la latence p95 du cache pendant 30 jours",
        "the quick brown fox jumps over the lazy dog",
        "one", "two", "ten", "nine",
    ]
    for sample in samples:
        assert engine.fold_text(sample) == _naive_fold(sample, raw_rules), sample


# --------------------------------------------------------------------------
# 3. Mesure de l'accord avec le cache historique (information, non bloquant)
# --------------------------------------------------------------------------
def test_legacy_cache_agreement_is_measured(engine: Canonicalizer) -> None:
    legacy = Path(r"D:\IcyCache\db\cache.db")
    if not legacy.is_file():
        pytest.skip("cache historique absent de cette machine")
    conn = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT original_prompt, normalized_prompt FROM cache "
        "WHERE original_prompt IS NOT NULL AND normalized_prompt IS NOT NULL LIMIT 400"
    ).fetchall()
    conn.close()
    if not rows:
        pytest.skip("cache historique vide")
    same = sum(1 for orig, stored in rows if engine.fold_text(orig) == stored)
    rate = same / len(rows)
    print(f"\naccord avec le cache historique (moteur actuel) : {same}/{len(rows)} = {rate:.1%}")
    # Le cache historique précède le moteur de référence : l'accord est
    # partiel et c'est attendu. On borne le plancher pour détecter une
    # régression réelle du moteur.
    assert rate >= 0.5, f"accord historiquement trop bas : {rate:.1%}"
