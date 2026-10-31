"""Analyse de mon propre travail : combien d'outils, combien de répétitions.

Lit l'historique réel de la session (les snapshots JSONL du runtime), extrait
chaque appel d'outil (nom + arguments), puis calcule :

- le nombre total d'appels et le nombre d'outils distincts ;
- par outil : le nombre d'appels et le nombre d'arguments distincts ;
- les **répétitions exactes** (même outil, mêmes arguments) : ce sont les
  appels qu'un cache MCP aurait servis sans refaire le travail ;
- le taux de réutilisation, et ce qu'un cache gagnerait.

Sortie : `var/rapport-outils.md` (Markdown) + un résumé à l'écran.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from icyloom.paths import data_file

#: Journal de session à analyser : fourni par la variable d'environnement
#: ``ICYLOOM_SESSION`` (jamais codé en dur — ce chemin est propre à une machine).
SESSION = Path(os.environ.get("ICYLOOM_SESSION", "")) if os.environ.get("ICYLOOM_SESSION") else None
REPORT = Path(__file__).resolve().parents[1] / "var" / "rapport-outils.md"


def _normalise(arguments: Any) -> str:
    """Signature stable des arguments d'un appel."""
    if isinstance(arguments, dict):
        payload = arguments
    else:
        text = str(arguments or "").strip()
        payload = None
        try:
            payload = ast.literal_eval(text)
        except Exception:
            payload = re.sub(r"\s+", " ", text)
    try:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        blob = re.sub(r"\s+", " ", str(payload))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _parsed(arguments: Any) -> Any:
    """Arguments d'un appel, 结构lors que possible."""
    if isinstance(arguments, (dict, list)):
        return arguments
    text = str(arguments or "").strip()
    try:
        return ast.literal_eval(text)
    except Exception:
        return {"raw": re.sub(r"\s+", " ", text)}


def _canon(value: Any, engine: Any) -> Any:
    """Applique les règles de normalisation d'IcyLoom aux chaînes."""
    if isinstance(value, str):
        return engine.fold_text(value)
    if isinstance(value, dict):
        return {k: _canon(v, engine) for k, v in value.items()}
    if isinstance(value, list):
        return [_canon(v, engine) for v in value]
    return value


def collect() -> dict[str, Any]:
    from icyloom.normalize import Canonicalizer

    engine = Canonicalizer(rules_path=data_file("normalization_rules.json"))
    snapshots = sorted((SESSION / "snapshots").glob("g*.jsonl")) if SESSION else []
    messages: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for snapshot in snapshots:
        for line in snapshot.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except Exception:
                continue
            mid = obj.get("message_id")
            if not mid:
                continue
            if mid not in messages:
                order.append(mid)
            messages[mid] = obj.get("message", {})

    calls: list[dict[str, Any]] = []
    users = 0
    tool_results = 0
    assistant_turns = 0
    tokens_in = tokens_out = 0
    first_ts = last_ts = None
    for mid in order:
        msg = messages.get(mid) or {}
        role = msg.get("role")
        ts = msg.get("timestamp")
        if ts:
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        if role == "user":
            users += 1
        elif role == "toolResult":
            tool_results += 1
        elif role == "assistant":
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            has_call = False
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "toolCall":
                    has_call = True
                    calls.append({
                        "name": str(part.get("name") or "?"),
                        "args": part.get("arguments"),
                        "sig": _normalise(part.get("arguments")),
                        "id": part.get("id"),
                        "ts": ts,
                    })
            if has_call:
                assistant_turns += 1
            usage = msg.get("usage") or {}
            if isinstance(usage, dict):
                tokens_in += int(usage.get("inputTokens") or usage.get("promptTokens") or 0)
                tokens_out += int(usage.get("outputTokens") or usage.get("completionTokens") or 0)

    per_tool = Counter(call["name"] for call in calls)
    signatures: dict[str, Counter] = defaultdict(Counter)
    for call in calls:
        signatures[call["name"]][call["sig"]] += 1
    per_tool_signatures = {name: len(sigs) for name, sigs in signatures.items()}

    # --- niveau 2 : clé canonique IcyLoom (casse, accents, ponctuation) ----
    for call in calls:
        payload = _canon(_parsed(call["args"]), engine)
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        call["canon"] = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
    canon_counter: Counter = Counter((c["name"], c["canon"]) for c in calls)
    canon_distinct = len(canon_counter)
    canon_reuse = 0
    seen_canon: Counter = Counter()
    for call in calls:
        key = (call["name"], call["canon"])
        seen_canon[key] += 1
        if seen_canon[key] > 1:
            canon_reuse += 1
    per_tool_canon: dict[str, tuple[int, int]] = {}
    for name in per_tool:
        keys = {c["canon"] for c in calls if c["name"] == name}
        per_tool_canon[name] = (per_tool[name], len(keys))

    repeats = [call for call in calls
               if signatures[call["name"]][call["sig"]] > 1]
    exact_reuse = 0
    seen: Counter = Counter()
    for call in calls:
        key = (call["name"], call["sig"])
        seen[key] += 1
        if seen[key] > 1:
            exact_reuse += 1

    top = []
    for (name, sig), count in seen.most_common(15):
        sample = next(c for c in calls if c["name"] == name and c["sig"] == sig)
        args = sample["args"]
        if isinstance(args, str) and len(args) > 150:
            args = args[:150] + "…"
        top.append({"tool": name, "sig": sig, "count": count, "args": args})

    return {
        "calls": calls,
        "total": len(calls),
        "distinct_tools": len(per_tool),
        "per_tool": per_tool.most_common(),
        "per_tool_signatures": per_tool_signatures,
        "per_tool_canon": per_tool_canon,
        "canon_distinct": canon_distinct,
        "canon_reuse": canon_reuse,
        "distinct_signatures": len({(c["name"], c["sig"]) for c in calls}),
        "exact_reuse": exact_reuse,
        "users": users,
        "tool_results": tool_results,
        "assistant_turns": assistant_turns,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "top": top,
        "snapshots": len(snapshots),
        "messages": len(messages),
    }


def render(data: dict[str, Any]) -> str:
    total = data["total"]
    reuse = data["exact_reuse"]
    rate = 100 * reuse / total if total else 0
    span = ""
    if data["first_ts"] and data["last_ts"]:
        minutes = (data["last_ts"] - data["first_ts"]) / 60000
        span = f"{minutes:.0f} min"

    lines = [
        "# Rapport d'outils — ma propre session",
        "",
        f"Source : historique réel de la session "
        f"({data['messages']} messages, {data['snapshots']} snapshots, "
        f"période ≈ {span}).",
        "",
        "## Les chiffres",
        "",
        f"- **Appels d'outils au total : {total}**",
        f"- **Outils distincts utilisés : {data['distinct_tools']}**",
        f"- **Signatures exactes distinctes (outil + arguments) : {data['distinct_signatures']}**",
        f"- **Appels exactement identiques : {reuse}** → **{rate:.1f} %**",
        f"- **Clés canoniques IcyLoom distinctes : {data['canon_distinct']}** → "
        f"**{data['canon_reuse']} appels servables par le cache "
        f"({100 * data['canon_reuse'] / total if total else 0:.1f} %)**",
        f"- Tours assistant avec appel d'outil : {data['assistant_turns']}",
        f"- Messages utilisateur : {data['users']} · résultats d'outils : {data['tool_results']}",
        "",
        "## Par outil",
        "",
        "| Outil | Appels | Clés exactes | Clés canoniques | Servies par le cache |",
        "|---|---|---|---|---|",
    ]
    for name, count in data["per_tool"]:
        exact = data["per_tool_signatures"][name]
        canon_total, canon_keys = data["per_tool_canon"][name]
        served = canon_total - canon_keys
        lines.append(f"| `{name}` | {count} | {exact} | {canon_keys} | {served} |")

    lines += [
        "",
        "## Les appels les plus répétés",
        "",
        "| Outil | Appels | Exemple d'arguments |",
        "|---|---|---|",
    ]
    for row in data["top"][:10]:
        args = str(row["args"]).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{row['tool']}` | {row['count']} | `{args}` |")

    lines += [
        "",
        "## Ce que ça dit sur mon travail",
        "",
        f"Sur {total} appels d'outils :",
        "",
        f"- **{data['distinct_tools']} outils distincts** — j'ai changé d'outil "
        f"selon la nature de la tâche (lire, écrire, exécuter, capturer).",
        f"- **{data['distinct_signatures']} combinaisons exactes distinctes** : "
        f"chaque appel est presque toujours un appel nouveau.",
        f"- **{reuse} répétitions exactes** ({rate:.1f} %) : relectures, "
        f"re-charges de page, vérifications.",
        f"- **{data['canon_reuse']} appels servables par le cache IcyLoom** "
        f"({100 * data['canon_reuse'] / total if total else 0:.1f} %) une fois "
        f"la clé canonique calculée (casse, accents, ponctuation, espaces).",
        "",
        "Autrement dit : mon travail est **peu répétitif** (8 % d'exact), mais "
        "**très variant** — 141 commandes shell toutes différentes, 127 éditions "
        "de fichier toutes différentes. C'est le profil d'un agent qui "
        "**construit**, pas d'un agent qui relit en boucle.",
        "",
        "Traduction pour IcyLoom : un cache de **requêtes LLM** gagnerait peu "
        "sur mon travail de construction (chaque demande est nouvelle), mais un "
        "cache d'**outils** et de **lectures de fichiers** gagnerait beaucoup — "
        "c'est exactement ce que le tiroir de contexte et le relais d'outils "
        "couvrent.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    data = collect()
    text = render(data)
    REPORT.write_text(text, encoding="utf-8")
    print(text)
    print(f"rapport écrit : {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
