"""Moteur de normalisation et clé de cache.

Objectif : deux requêtes qui disent la même chose doivent partager la même
clé de cache, quelle que soit leur formulation (casse, accents, ponctuation,
politesse, nombres en toutes lettres), et la clé doit rester **stable dans le
temps** pour qu'une entrée de cache déjà écrite reste retrouvable.

Le moteur est piloté par un fichier de règles **externe** (`data/
normalization_rules.json`) : les règles sont une donnée, pas du code. Le hash
dépend d'elles ; les modifier invalide les entrées existantes.

Chaîne de traitement d'un texte :

1. décomposition Unicode (NFKD) ;
2. mise en minuscules ;
3. application des règles (Aho-Corasick, avec respect des frontières de mot) ;
4. nombres en toutes lettres → chiffres, et verbes arithmétiques → symboles ;
5. suppression des diacritiques ;
6. suppression de la ponctuation de phrase (les opérateurs sont conservés) ;
7. compactage des espaces ;
8. retrait du suffixe volatil injecté par un client.

Le corps de requête est ensuite canonisé en JSON trié, puis haché en SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

#: Champs du corps qui n'ont aucun effet sur la réponse du modèle : exclus de
#: la clé de cache.
DEFAULT_IGNORED_FIELDS: tuple[str, ...] = ("stream", "stream_options", "user", "metadata")

#: Champs textuels dont le contenu est normalisé.
TEXT_FIELDS: tuple[str, ...] = ("messages", "system", "prompt", "input")

#: Suffixes « volatils » : blocs réinjectés à chaque requête par un client
#: (index de compétences, récupérations, etc.). Ils sont retirés **de la clé
#: uniquement** — le corps envoyé au fournisseur n'est jamais modifié.
DEFAULT_VOLATILE_SUFFIXES: tuple[str, ...] = (
    r"retrieved skills\W*top\W?k relevant to your query.*$",
)

#: Ponctuation de phrase retirée. Les opérateurs (+ - * / = < > % ^ & | …)
#: sont volontairement conservés : "12+2" et "1+22" ne doivent pas partager
#: une clé de cache.
_SENTENCE_PUNCTUATION = re.compile(
    r"[!?.,;:'\"`\u00b4\u201c\u201d\u2018\u2019\u00ab\u00bb\u00a1\u00bf\u2026]", re.UNICODE
)
_WHITESPACE = re.compile(r"\s+")

#: Verbes arithmétiques → symboles (après conversion des nombres).
_ARITHMETIC_WORDS = (
    (" plus ", "+"),
    (" minus ", "-"),
    (" times ", "*"),
    (" divided by ", "/"),
)

#: Nombres en toutes lettres → chiffres.
_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}


def canonical_json(payload: Any) -> str:
    """JSON canonique : clés triées, séparateurs compacts, UTF-8 préservé."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _is_word_char(char: str) -> bool:
    """Caractère qui interdit une frontière de mot (alphanumérique, _ ou diacritique)."""
    return char.isalnum() or char == "_" or unicodedata.category(char) == "Mn"


@dataclass(frozen=True)
class Rule:
    """Une règle de substitution : motif textuel → remplacement."""

    pattern: str
    replacement: str


class RuleEngine:
    """Applique un ensemble de règles de substitution sur du texte.

    L'implémentation utilise Aho-Corasick quand la bibliothèque est
    disponible (une seule passe), sinon une repli par expressions régulières.
    Les deux chemins produisent le même résultat.
    """

    def __init__(self, rules: Iterable[Rule]) -> None:
        self._rules: list[Rule] = list(rules)
        self._automaton = None
        self._compiled: list[tuple[re.Pattern[str], str]] = []
        self._build()

    def _build(self) -> None:
        try:
            import ahocorasick

            automaton = ahocorasick.Automaton()
            for rule in self._rules:
                pattern = rule.pattern.lower()
                if pattern:
                    automaton.add_word(pattern, (pattern, rule.replacement))
            automaton.make_automaton()
            self._automaton = automaton
        except Exception:
            self._automaton = None
            self._compiled = [
                (re.compile(r"\b" + re.escape(rule.pattern.lower()) + r"\b", re.IGNORECASE),
                 rule.replacement)
                for rule in self._rules
                if rule.pattern
            ]

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    def apply(self, text: str) -> str:
        """Applique toutes les règles à `text`."""
        if not text or not self._rules:
            return text
        if self._automaton is not None:
            return self._apply_automaton(text)
        for pattern, replacement in self._compiled:
            text = pattern.sub(replacement, text)
        return text

    def _apply_automaton(self, text: str) -> str:
        candidates: list[tuple[int, int, str]] = []
        for end_index, (pattern, replacement) in self._automaton.iter(text):
            start = end_index - len(pattern) + 1
            stop = end_index + 1
            # Une règle ne s'applique pas au milieu d'un mot.
            if start > 0 and _is_word_char(pattern[0]) and _is_word_char(text[start - 1]):
                continue
            if stop < len(text) and _is_word_char(pattern[-1]) and _is_word_char(text[stop]):
                continue
            candidates.append((start, stop, replacement))
        # Priorité au début, puis à la plus longue occurrence.
        candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
        pieces: list[str] = []
        cursor = 0
        for start, stop, replacement in candidates:
            if start < cursor:
                continue
            pieces.append(text[cursor:start])
            pieces.append(replacement)
            cursor = stop
        pieces.append(text[cursor:])
        return "".join(pieces)


class Canonicalizer:
    """Normalise un texte et un corps de requête, puis calcule la clé."""

    def __init__(
        self,
        rules_path: Path | None = None,
        ignored_fields: Iterable[str] = DEFAULT_IGNORED_FIELDS,
        volatile_suffixes: Iterable[str] = DEFAULT_VOLATILE_SUFFIXES,
    ) -> None:
        self.rules_path = Path(rules_path) if rules_path else None
        self.ignored_fields = tuple(ignored_fields)
        self._volatile = tuple(
            re.compile(pattern, re.IGNORECASE | re.DOTALL) for pattern in volatile_suffixes
        )
        self._engine = RuleEngine(self._load_rules())
        self._number_patterns = [
            (re.compile(rf"\b{word}\b", re.IGNORECASE), digit)
            for word, digit in _NUMBER_WORDS.items()
        ]

    # -- règles ------------------------------------------------------------
    def _load_rules(self) -> list[Rule]:
        if self.rules_path is None or not self.rules_path.is_file():
            return []
        try:
            raw = json.loads(self.rules_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        entries = raw.get("rules") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return []
        rules: list[Rule] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            pattern = entry.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                continue
            replacement = entry.get("replace")
            rules.append(Rule(pattern=pattern, replacement=replacement if isinstance(replacement, str) else ""))
        return rules

    @property
    def rule_count(self) -> int:
        return self._engine.rule_count

    # -- texte -------------------------------------------------------------
    def fold_text(self, text: str) -> str:
        """Normalise un texte brut (forme canonique de la clé)."""
        if not isinstance(text, str) or not text:
            return ""
        folded = unicodedata.normalize("NFKD", text).lower()
        folded = self._engine.apply(folded)
        for pattern, digit in self._number_patterns:
            folded = pattern.sub(digit, folded)
        for word, symbol in _ARITHMETIC_WORDS:
            folded = folded.replace(word, symbol)
        stripped = "".join(
            char
            for char in unicodedata.normalize("NFD", folded)
            if unicodedata.category(char) != "Mn"
        )
        stripped = _SENTENCE_PUNCTUATION.sub("", stripped)
        stripped = _WHITESPACE.sub(" ", stripped).strip()
        for volatile in self._volatile:
            stripped = volatile.sub("", stripped).strip()
        return stripped

    def fold_blocks(self, content: Any) -> Any:
        """Normalise le contenu textuel d'un message (blocs non textuels intacts)."""
        if isinstance(content, str):
            return self.fold_text(content)
        if isinstance(content, list):
            folded: list[Any] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(
                    block.get("text"), str
                ):
                    copy = dict(block)
                    copy["text"] = self.fold_text(block["text"])
                    folded.append(copy)
                else:
                    folded.append(block)
            return folded
        return content

    # -- corps -------------------------------------------------------------
    def canonical_body(self, body: Mapping[str, Any] | None) -> dict[str, Any]:
        """Forme canonique d'un corps de requête (champs volatils exclus)."""
        if not isinstance(body, Mapping):
            return {}
        canonical = {k: v for k, v in body.items() if k not in self.ignored_fields}
        for field in TEXT_FIELDS:
            if field not in canonical:
                continue
            value = canonical[field]
            if field == "messages" and isinstance(value, list):
                canonical[field] = [
                    {**m, "content": self.fold_blocks(m.get("content"))}
                    if isinstance(m, dict)
                    else m
                    for m in value
                ]
            elif field == "system" and isinstance(value, list):
                canonical[field] = self.fold_blocks(value)
            else:
                canonical[field] = self.fold_blocks(value)
        return canonical

    def digest(self, body: Mapping[str, Any] | None, scope: Mapping[str, Any] | None = None) -> str:
        """Clé de cache : SHA-256 du corps canonique (+ périmètre optionnel)."""
        payload: Any = self.canonical_body(body)
        if scope:
            payload = {"__scope": dict(scope), "body": payload}
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
