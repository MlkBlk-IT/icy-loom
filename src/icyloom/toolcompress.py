"""Compression des schémas d'outils et des résultats d'appels.

Deux exigences opposées :

* le contexte du modèle doit rétrécir — chaque schéma et chaque résultat
  restent dans l'historique pour toute la session ;
* le cache de prompt ne doit pas casser — l'**ordre** des propriétés et
  l'**ordre** des schémas exposés restent stables d'un appel à l'autre.

La compression est donc *structurelle* (on retire ce qui ne sert pas à
l'agent) et non pas *editoriale* (on ne réécrit pas les descriptions).
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any

from .tokenizer import count_tokens

#: Clés de schéma retirées : redondantes ou coûteuses sans valeur d'exécution.
DROPPED_SCHEMA_KEYS = ("title", "$schema", "$id", "examples", "x-codegen")

#: Signatures de secrets : un résultat qui en contient ne doit pas être
#: renvoyé tel quel dans l'historique du modèle.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{20,}")),
    ("password", re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*\S{4,}")),
    ("api_key", re.compile(r"(?i)\b(api[_-]?key|secret|token)\s*[:=]\s*\S{8,}")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
)

_HTML_TAG = re.compile(r"<[^>]+>")
_DIFF_HUNK = re.compile(r"^@@")


def shorten(text: str, limit: int) -> str:
    """Raccourcit un texte à une frontière de phrase ou de mot."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "; ", ", "):
        pos = cut.rfind(sep)
        if pos > limit * 0.5:
            return cut[:pos].rstrip() + "…"
    return cut.rstrip() + "…"


def compress_schema(schema: Any, max_description_chars: int = 160,
                    drop_examples: bool = True) -> Any:
    """Copie compressée d'un JSON Schema.

    L'ordre des clés est conservé (donc l'ordre des arguments reste stable :
    c'est la condition du cache de prompt).
    """
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for key, value in schema.items():
            if key in DROPPED_SCHEMA_KEYS:
                continue
            if key == "examples" and not drop_examples:
                out[key] = value
                continue
            if key == "description" and isinstance(value, str):
                out[key] = shorten(value, max_description_chars)
                continue
            if key == "properties" and isinstance(value, dict):
                # Les NOMS de propriétés ne sont pas des métadonnées : un
                # argument qui s'appelle « title » doit survivre.
                out[key] = {name: compress_schema(sub, max_description_chars,
                                                 drop_examples)
                            for name, sub in value.items()}
                continue
            out[key] = compress_schema(value, max_description_chars, drop_examples)
        return out
    if isinstance(schema, list):
        return [compress_schema(item, max_description_chars, drop_examples)
                for item in schema]
    return schema


def schema_tokens(schema: Any) -> int:
    """Tokens occupés par un schéma (estimation, même compteur que le reste)."""
    from .tokenizer import count_tokens

    return count_tokens(json.dumps(schema, ensure_ascii=False, default=str))


def detect_sensitive(text: str) -> list[str]:
    """Types de secrets présents dans un texte."""
    if not text:
        return []
    found: list[str] = []
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            found.append(label)
    return found


class _TextExtractor(HTMLParser):
    """Extracteur de texte : le HTML d'une page ne vaut pas son texte."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    """HTML -> texte lisible."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:      # pragma: no cover - HTML malformé
        return re.sub(r"\s+", " ", _HTML_TAG.sub(" ", html)).strip()
    return "\n".join(parser.parts)


def _summarize_json(data: Any, max_chars: int) -> str:
    """JSON : on garde la structure et les premiers éléments, pas tout."""
    if isinstance(data, list):
        kept: list[Any] = []
        size = 2
        for item in data:
            rendered = json.dumps(item, ensure_ascii=False, default=str)
            if size + len(rendered) > max_chars and kept:
                break
            kept.append(item)
            size += len(rendered) + 2
        hidden = len(data) - len(kept)
        text = json.dumps(kept, ensure_ascii=False, indent=2, default=str)
        if hidden:
            text += f"\n… {hidden} élément(s) masqué(s) sur {len(data)} au total."
        return text
    if isinstance(data, dict):
        kept = {}
        size = 2
        for key, value in data.items():
            rendered = json.dumps(value, ensure_ascii=False, default=str)
            if size + len(rendered) > max_chars and kept:
                kept[key] = "…"
                continue
            kept[key] = value
            size += len(rendered) + 2
        return json.dumps(kept, ensure_ascii=False, indent=2, default=str)
    return json.dumps(data, ensure_ascii=False, default=str)


def _summarize_diff(text: str, max_chars: int) -> str:
    """Diff : entêtes, statistiques et début des hunks."""
    lines = text.splitlines()
    headers: list[str] = []
    hunks: list[str] = []
    added = removed = 0
    for line in lines:
        if line.startswith(("diff --git", "index ", "--- ", "+++ ", "new file", "deleted file")):
            headers.append(line)
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
        if _DIFF_HUNK.match(line):
            hunks.append(line)
    out = list(headers)
    out.append(f"… {added} ligne(s) ajoutée(s), {removed} supprimée(s), "
               f"{len(hunks)} hunk(s).")
    body = "\n".join(lines[:max(1, max_chars // 120)])
    if len(body) < len(text):
        body += "\n… diff tronqué."
    return "\n".join(out) + "\n" + body


def _summarize_log(text: str, max_chars: int) -> str:
    """Log : on garde le début et la fin, le milieu est le moins utile."""
    lines = text.splitlines()
    if len(lines) <= 40:
        return text[:max_chars]
    head, tail = lines[:5], lines[-20:]
    hidden = len(lines) - len(head) - len(tail)
    return "\n".join(head + [f"… {hidden} ligne(s) masquée(s) …"] + tail)


def compress_result(payload: Any, max_chars: int = 4000) -> tuple[str, dict[str, Any]]:
    """Compresse le contenu d'un résultat d'outil.

    Renvoie ``(texte, meta)``. ``meta`` décrit ce qui a été fait : type
    detected, taille d'origine, taille finale, secrets repérés.
    """
    meta: dict[str, Any] = {"kind": "raw", "original_chars": 0,
                            "compressed_chars": 0, "truncated": False,
                            "sensitive": []}
    if isinstance(payload, str):
        blocks = [payload]
        structured: Any = None
    elif isinstance(payload, dict):
        blocks = [b for b in (payload.get("content") or []) if isinstance(b, str)]
        structured = payload.get("structured")
    else:
        blocks = [json.dumps(payload, ensure_ascii=False, default=str)]
        structured = None
    text = "\n".join(blocks)
    if structured is not None:
        rendered = json.dumps(structured, ensure_ascii=False, default=str)
        if len(rendered) > len(text):
            text = rendered
            meta["kind"] = "structured"
    meta["original_chars"] = len(text)
    meta["original_tokens"] = count_tokens(text)
    if len(text) <= max_chars:
        meta["compressed_chars"] = len(text)
        meta["compressed_tokens"] = count_tokens(text)
        meta["saved_tokens"] = 0
        meta["sensitive"] = detect_sensitive(text)
        return text, meta

    stripped = text.lstrip()
    if stripped[:1] in "{[":
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if data is not None:
            text = _summarize_json(data, max_chars)
            meta["kind"] = "json"
    elif "<" in text and text.count("<") > 12:
        text = html_to_text(text)
        meta["kind"] = "html"
    elif "diff --git" in text or _DIFF_HUNK.search(text):
        text = _summarize_diff(text, max_chars)
        meta["kind"] = "diff"
    else:
        text = _summarize_log(text, max_chars)
        meta["kind"] = "log"
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    meta["compressed_chars"] = len(text)
    meta["compressed_tokens"] = count_tokens(text)
    meta["saved_tokens"] = max(0, meta["original_tokens"] - meta["compressed_tokens"])
    meta["truncated"] = True
    meta["sensitive"] = detect_sensitive(text)
    return text, meta
