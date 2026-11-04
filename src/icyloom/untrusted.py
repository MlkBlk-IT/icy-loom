"""Détection de contenu non fiable dans les résultats d'outils.

La spécification MCP traite la description d'un outil **et** le contenu de son
résultat comme des entrées non fiables : un serveur descendant peut tenter
d'injecter des instructions dans le contexte du modèle (« ignore les
instructions précédentes », « envoie la clé d'API à ... »). Un relais qui
avale ces résultats sans les examiner transmet l'injection au modèle.

Ce module repère les familles d'injection les plus courantes. Il ne prouve
rien : un résultat signalé est **à traiter comme hostile**, pas à exécuter.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Familles d'injection : (nom de la règle, motif).
RULES: tuple[tuple[str, str], ...] = (
    ("instruction_override",
     r"(?i)\b(ignore|disregard|forget|override)\b[^.\n]{0,60}"
     r"\b(previous|prior|above|earlier|all)\b[^.\n]{0,30}"
     r"\b(instruction|instructions|prompt|rule|rules)\b"),
    ("role_hijack",
     r"(?i)\byou are now\b|\bact as (a|an)\b|\bsystem prompt\b|\bnew instructions\b"),
    ("tool_directive",
     r"(?i)\b(you must|always|immediately|important|urgent)\b[^.\n]{0,40}"
     r"\b(call|invoke|execute|run)\b[^.\n]{0,40}\b(tool|function|mcp)\b"),
    ("secret_exfiltration",
     r"(?i)\b(exfiltrat\w*|send|post|upload|forward)\b[^.\n]{0,60}"
     r"\b(api[_ -]?key|access[_ -]?token|password|secret|credential|private key"
     r"|\.env|ssh key)\b"),
    ("credential_leak",
     r"(?i)\b(api[_ -]?key|access[_ -]?token|password|passwd|secret|bearer)\b"
     r"\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{8,}"),
    ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("hidden_instruction",
     r"(?i)<!--[^>]{0,300}-->|\u200b|\u200c|\u200d"),
    ("chatml", r"<\|im_(start|end)\|>"),
    ("conceal_from_user",
     r"(?i)\bdo not (tell|mention|inform|reveal)\b[^.\n]{0,30}"
     r"\b(user|human|operator)\b"),
    ("forged_tool_call",
     r"(?i)[\"']?(tool_use|tool_call|function_call)[\"']?\s*[:=]"),
)

_COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern)) for name, pattern in RULES)


def scan(text: str, *, max_chars: int = 120_000) -> list[dict[str, Any]]:
    """Repère les motifs d'injection dans un texte.

    Args:
        text: contenu à examiner
        max_chars: portion de texte réellement analysée (garde-fou de coût)

    Returns:
        Liste de ``{"rule": nom, "sample": extrait}`` (vide si rien).
    """
    if not text:
        return []
    excerpt = text[:max_chars]
    hits: list[dict[str, Any]] = []
    for name, pattern in _COMPILED:
        match = pattern.search(excerpt)
        if match is None:
            continue
        start = max(0, match.start() - 20)
        hits.append({
            "rule": name,
            "sample": excerpt[start:match.end() + 20].replace("\n", " ")[:160],
        })
    return hits


def scan_payload(payload: Any, *, max_chars: int = 120_000) -> list[dict[str, Any]]:
    """Scanne le texte utile d'un résultat d'outil (blocs texte, puis structuré)."""
    texts: list[str] = []
    if isinstance(payload, dict):
        for item in payload.get("content") or []:
            if isinstance(item, str):
                texts.append(item)
        structured = payload.get("structured")
        if structured is not None and not texts:
            texts.append(json.dumps(structured, ensure_ascii=False, default=str))
    elif isinstance(payload, str):
        texts.append(payload)
    return scan("\n".join(texts), max_chars=max_chars)
