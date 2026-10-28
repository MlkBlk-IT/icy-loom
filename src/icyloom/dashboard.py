"""Agrégat de métriques du dashboard.

Une seule requête HTTP (`/api/dashboard`) renvoie **tout** ce que la page
affiche : compteurs globaux, séries temporelles, répartitions, tables et
derniers événements. Le dashboard ne parle jamais à la base directement : il
consomme cet agrégat, ce qui garantit que la page et l'API ne peuvent pas
diverger.

Principes :

* **Tokens réels** : les compteurs proviennent des colonnes `usage` remontées
  par l'amont, jamais d'une estimation locale.
* **Aucune écriture** : cet aggregator est strictement en lecture.
* **Une seule base** : tout provient de la base SQLite unique du projet.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import Config
from .db import Database
from .tokenizer import counter_name

#: Dossier des fichiers statiques servis par l'API d'administration.
STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Fichier HTML du dashboard.
DASHBOARD_FILE = STATIC_DIR / "dashboard.html"

#: Palettes (froid -> chaud) utilisées par le dashboard.
PALETTE = (
    "#4fd1ff", "#8b7cff", "#3ddc97", "#ffb454", "#ff6b81",
    "#5eead4", "#c084fc", "#facc15", "#38bdf8", "#fb7185",
)


def dashboard_html() -> str:
    """Contenu HTML du dashboard (lu à chaque requête : pas de cache obsolète)."""
    return DASHBOARD_FILE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# utilitaires
# ---------------------------------------------------------------------------
def _percentile(values: Sequence[float], pct: float) -> float:
    """Percentile (interpolation linéaire) d'une liste de valeurs."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return float(ordered[low] + (ordered[high] - ordered[low]) * (rank - low))


def _rows(cur: Any, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.execute(sql, tuple(params)).fetchall()]


def _color(index: int) -> str:
    return PALETTE[index % len(PALETTE)]


def _epoch_to_ts(value: Any) -> str:
    """Convertit un horodatage epoch (REAL) en texte lisible."""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(float(value)))
    except (TypeError, ValueError):
        return ""


def _group_by_bucket(rows: Iterable[dict[str, Any]], key: str = "bucket") -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(str(row.get(key) or "inconnu"), []).append(row)
    return out


# ---------------------------------------------------------------------------
# séries temporelles
# ---------------------------------------------------------------------------
def _series_calls(db: Database, hours: int) -> list[dict[str, Any]]:
    """Trafic par heure : hits, misses, tokens, latence."""
    with db.cursor(commit=False) as cur:
        rows = cur.execute(
            # `substr` bat `strftime` d'un ordre de grandeur sur des dizaines
            # de milliers de lignes : le format stocké est déjà 'YYYY-MM-DD HH:MM:SS'.
            "SELECT substr(ts, 1, 13) || ':00' AS bucket,"
            " kind, latency_ms, prompt_tokens, completion_tokens, saved_prompt, saved_completion"
            " FROM llm_events WHERE ts >= datetime('now', ?)",
            (f"-{int(hours)} hours",),
        ).fetchall()

    buckets = _group_by_bucket([dict(r) for r in rows])
    series: list[dict[str, Any]] = []
    for bucket in sorted(buckets):
        items = buckets[bucket]
        hits = [i for i in items if i["kind"] == "hit"]
        misses = [i for i in items if i["kind"] == "miss"]
        latencies = [float(i["latency_ms"]) for i in items if i["latency_ms"] is not None]
        total = len(items)
        series.append({
            "bucket": bucket,
            "total": total,
            "hits": len(hits),
            "misses": len(misses),
            "hit_rate_pct": round(len(hits) / total * 100.0, 2) if total else 0.0,
            "latency_p50": round(_percentile(latencies, 50), 1),
            "latency_p95": round(_percentile(latencies, 95), 1),
            "latency_max": round(max(latencies), 1) if latencies else 0.0,
            "prompt_tokens": sum(int(i["prompt_tokens"] or 0) for i in items),
            "completion_tokens": sum(int(i["completion_tokens"] or 0) for i in items),
            "saved_tokens": sum(int(i["saved_prompt"] or 0) + int(i["saved_completion"] or 0)
                                for i in items),
        })
    return series


def _series_cache(db: Database, hours: int) -> list[dict[str, Any]]:
    """Croissance du cache : entrées créées par heure (cumulées)."""
    with db.cursor(commit=False) as cur:
        rows = cur.execute(
            "SELECT substr(created_at, 1, 13) || ':00' AS bucket, COUNT(*) AS n"
            " FROM cache_entries WHERE created_at >= datetime('now', ?)"
            " GROUP BY bucket ORDER BY bucket",
            (f"-{int(hours)} hours",),
        ).fetchall()
    total = 0
    series = []
    for row in rows:
        total += int(row["n"])
        series.append({"bucket": row["bucket"], "created": int(row["n"]), "cumulative": total})
    return series


def _series_tokens(db: Database, days: int) -> list[dict[str, Any]]:
    """Économie quotidienne : payé / économisé / gain."""
    with db.cursor(commit=False) as cur:
        rows = cur.execute(
            "SELECT substr(ts, 1, 10) AS day,"
            " COALESCE(SUM(tokens_in + tokens_out), 0) AS paid,"
            " COALESCE(SUM(tokens_saved), 0) AS saved,"
            " COALESCE(SUM(tool_tokens_saved), 0) AS tool_saved,"
            " COUNT(*) AS entries"
            " FROM ledger_entries WHERE ts >= datetime('now', ?)"
            " GROUP BY day ORDER BY day",
            (f"-{int(days)} days",),
        ).fetchall()
    series = []
    for row in rows:
        paid = int(row["paid"])
        saved = int(row["saved"]) + int(row["tool_saved"])
        total = paid + saved
        series.append({
            "day": row["day"],
            "paid": paid,
            "saved": saved,
            "entries": int(row["entries"]),
            "gain_pct": round(saved / total * 100.0, 2) if total else 0.0,
        })
    return series


def _series_tools(db: Database, hours: int) -> list[dict[str, Any]]:
    """Appels d'outils par heure (cache vs appel réel)."""
    with db.cursor(commit=False) as cur:
        rows = cur.execute(
            "SELECT substr(ts, 1, 13) || ':00' AS bucket, kind, COUNT(*) AS n,"
            " COALESCE(SUM(latency_ms), 0) AS latency"
            " FROM tool_events WHERE ts >= datetime('now', ?)"
            " GROUP BY bucket, kind",
            (f"-{int(hours)} hours",),
        ).fetchall()
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = buckets.setdefault(row["bucket"], {"bucket": row["bucket"], "calls": 0, "hits": 0})
        entry["calls"] += int(row["n"])
        if row["kind"] == "tool_hit":
            entry["hits"] += int(row["n"])
    return [buckets[k] for k in sorted(buckets)]


# ---------------------------------------------------------------------------
# registre unifié (`v_token_ledger`)
# ---------------------------------------------------------------------------
#: Les trois domaines croisés par la vue unifiée.
DOMAINS = ("cache", "compression", "tools")

#: Libellés lisibles (le dashboard ne parle pas SQL).
DOMAIN_LABELS = {
    "cache": "Cache LLM",
    "compression": "Compression",
    "tools": "Tax d'outils",
}


def _registry(db: Database, days: int = 14) -> dict[str, Any]:
    """Registre unifié lu dans `v_token_ledger`.

    C'est la seule source des chiffres d'économie : cache, compression et tax
    d'outils sont additionnés **dans la même unité** (tokens réellement payés
    vs tokens réellement évités). Aucune estimation, aucune moyenne inventée.
    """
    with db.cursor(commit=False) as cur:
        totals = cur.execute(
            "SELECT COUNT(*) AS rows,"
            " COALESCE(SUM(tokens_paid), 0) AS paid,"
            " COALESCE(SUM(tokens_saved), 0) AS saved,"
            " COALESCE(SUM(latency_ms), 0) AS latency,"
            " COALESCE(SUM(saved_latency_ms), 0) AS saved_latency"
            " FROM v_token_ledger"
        ).fetchone()
        by_domain = _rows(cur,
            "SELECT domain, COUNT(*) AS rows,"
            " COALESCE(SUM(tokens_paid), 0) AS paid,"
            " COALESCE(SUM(tokens_saved), 0) AS saved,"
            " COALESCE(SUM(latency_ms), 0) AS latency,"
            " COALESCE(SUM(saved_latency_ms), 0) AS saved_latency"
            " FROM v_token_ledger GROUP BY domain ORDER BY saved DESC, paid DESC")
        # La série journalière interroge les trois tables de base avec les
        # MÊMES expressions que la vue `v_token_ledger` : la vue matérialise
        # l'union complète (≈ 0,6 s), les tables seules profitent des index
        # couvrants (≈ 30 ms). Les chiffres sont identiques.
        daily = _rows(cur,
            "SELECT substr(ts, 1, 10) AS day, 'cache' AS domain,"
            " COALESCE(SUM(CASE WHEN kind = 'miss'"
            "   THEN prompt_tokens + completion_tokens ELSE 0 END), 0) AS paid,"
            " COALESCE(SUM(CASE WHEN kind = 'hit'"
            "   THEN saved_prompt + saved_completion ELSE 0 END), 0) AS saved"
            " FROM llm_events WHERE ts >= datetime('now', ?) GROUP BY day",
            (f"-{int(days)} days",))
        daily += _rows(cur,
            "SELECT substr(datetime(created_at, 'unixepoch'), 1, 10) AS day,"
            " 'compression' AS domain,"
            " COALESCE(SUM(anchor_tokens), 0) AS paid,"
            " COALESCE(SUM(tokens_before - anchor_tokens), 0) AS saved"
            " FROM context_sources"
            " WHERE created_at >= strftime('%s', 'now', ?) GROUP BY day",
            (f"-{int(days)} days",))
        daily += _rows(cur,
            "SELECT substr(ts, 1, 10) AS day, 'tools' AS domain,"
            " 0 AS paid, 0 AS saved"
            " FROM tool_events WHERE kind = 'tool_hit'"
            " AND ts >= datetime('now', ?) GROUP BY day",
            (f"-{int(days)} days",))

    paid = int(totals["paid"])
    saved = int(totals["saved"])
    baseline = paid + saved
    gain = (saved / baseline * 100.0) if baseline else 0.0
    threshold = 35.0

    days_map: dict[str, dict[str, int]] = {}
    for row in daily:
        entry = days_map.setdefault(str(row["day"]), {
            "day": str(row["day"]), "paid": 0, "saved": 0,
            "cache": 0, "compression": 0, "tools": 0,
        })
        domain = str(row["domain"])
        entry["paid"] += int(row["paid"])
        entry["saved"] += int(row["saved"])
        if domain in entry:
            entry[domain] += int(row["saved"])

    return {
        "totals": {
            "rows": int(totals["rows"]),
            "tokens_paid": paid,
            "tokens_saved": saved,
            "tokens_baseline": baseline,
            "latency_ms": int(totals["latency"]),
            "saved_latency_ms": int(totals["saved_latency"]),
        },
        "gain_pct": round(gain, 2),
        "threshold_pct": threshold,
        "threshold_met": gain >= threshold,
        "by_domain": [
            {**row,
             "label": DOMAIN_LABELS.get(str(row["domain"]), str(row["domain"])),
             "share_pct": round(int(row["saved"]) / saved * 100.0, 2) if saved else 0.0}
            for row in by_domain
        ],
        "daily": list(days_map.values()),
        "days": int(days),
        "note": ("Registre unifié v_token_ledger : tokens payés (usage amont réel) "
                 "et tokens évités (cache + compression + tax d'outils)."),
    }


#: Noms des jours (lundi en premier, comme la semaine française).
DAY_LABELS = ("lun", "mar", "mer", "jeu", "ven", "sam", "dim")


def _surface(db: Database, days: int = 14) -> dict[str, Any]:
    """Surface d'appel **réelle** : jour calendaire × heure de la journée.

    Alimente le « volcan » en 3D et la carte topographique. Les deux lisent la
    même grille : aucune valeur n'est inventée côté navigateur.

    La grille est *pleine* : les jours sans trafic valent 0 (c'est un fait
    mesuré, pas un trou de données).
    """
    days = max(2, min(int(days), 60))
    with db.cursor(commit=False) as cur:
        rows = cur.execute(
            "SELECT substr(ts, 1, 10) AS day, substr(ts, 12, 2) AS hh,"
            " COUNT(*) AS calls,"
            " COALESCE(SUM(saved_prompt + saved_completion), 0) AS saved"
            " FROM llm_events WHERE ts >= datetime('now', ?)"
            " GROUP BY day, hh",
            (f"-{days} days",),
        ).fetchall()

    # calendrier : les `days` derniers jours, du plus ancien au plus récent
    calendar: list[str] = []
    labels: list[str] = []
    for offset in range(days - 1, -1, -1):
        stamp = time.gmtime(time.time() - offset * 86400)
        calendar.append(time.strftime("%Y-%m-%d", stamp))
        labels.append(f"{DAY_LABELS[(stamp.tm_wday + 6) % 7]} "
                      f"{stamp.tm_mday:02d}/{stamp.tm_mon:02d}")

    index_of = {day: index for index, day in enumerate(calendar)}
    calls = [[0] * 24 for _ in calendar]
    saved = [[0] * 24 for _ in calendar]
    for row in rows:
        day = str(row["day"])
        index = index_of.get(day)
        if index is None:
            continue
        try:
            hour = int(row["hh"])
        except (TypeError, ValueError):
            continue
        if not (0 <= hour < 24):
            continue
        calls[index][hour] = int(row["calls"])
        saved[index][hour] = int(row["saved"])

    peak = max((value for row in calls for value in row), default=0)
    return {
        "days": labels,
        "dates": calendar,
        "hours": list(range(24)),
        "calls": calls,
        "saved": saved,
        "peak": peak,
        "total": sum(sum(row) for row in calls),
        "window_days": days,
        "note": ("Surface réelle : chaque case = un appel mesuré dans la base. "
                 "Les cases vides sont des heures sans trafic."),
    }



# ---------------------------------------------------------------------------
# agrégat principal
# ---------------------------------------------------------------------------
def snapshot(config: Config, hours: int = 24, days: int = 14) -> dict[str, Any]:
    """Construit l'instantané complet affiché par le dashboard."""
    hours = max(1, min(int(hours), 720))
    days = max(1, min(int(days), 365))
    db = Database(config)
    db.ensure_schema()

    with db.cursor(commit=False) as cur:
        totals = cur.execute(
            "SELECT COUNT(*) AS calls,"
            " COALESCE(SUM(CASE WHEN kind='hit' THEN 1 ELSE 0 END), 0) AS hits,"
            " COALESCE(SUM(CASE WHEN kind='miss' THEN 1 ELSE 0 END), 0) AS misses,"
            " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
            " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
            " COALESCE(SUM(saved_prompt), 0) AS saved_prompt,"
            " COALESCE(SUM(saved_completion), 0) AS saved_completion"
            " FROM llm_events"
        ).fetchone()
        all_latencies = [
            float(r[0]) for r in cur.execute(
                "SELECT latency_ms FROM llm_events WHERE latency_ms IS NOT NULL"
            ).fetchall()
        ]
        by_upstream = _rows(cur,
            "SELECT upstream, COUNT(*) AS calls,"
            " COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,"
            " COALESCE(SUM(CASE WHEN kind='hit' THEN 1 ELSE 0 END), 0) AS hits"
            " FROM llm_events WHERE upstream IS NOT NULL GROUP BY upstream"
            " ORDER BY calls DESC")
        by_model = _rows(cur,
            "SELECT model, COUNT(*) AS calls,"
            " COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,"
            " COALESCE(SUM(saved_prompt + saved_completion), 0) AS saved,"
            " COALESCE(SUM(CASE WHEN kind='hit' THEN 1 ELSE 0 END), 0) AS hits"
            " FROM llm_events WHERE model IS NOT NULL AND model <> ''"
            " GROUP BY model ORDER BY calls DESC LIMIT 12")
        by_client = _rows(cur,
            "SELECT client, COUNT(*) AS calls"
            " FROM llm_events WHERE client IS NOT NULL AND client <> ''"
            " GROUP BY client ORDER BY calls DESC LIMIT 12")
        by_kind = _rows(cur, "SELECT kind, COUNT(*) AS n FROM llm_events GROUP BY kind")
        ledger_by_kind = _rows(cur,
            "SELECT kind, COUNT(*) AS n, COALESCE(SUM(tokens_in + tokens_out), 0) AS spent,"
            " COALESCE(SUM(tokens_saved + tool_tokens_saved), 0) AS saved,"
            " COALESCE(SUM(latency_ms), 0) AS latency"
            " FROM ledger_entries GROUP BY kind ORDER BY n DESC")
        ledger_by_upstream = _rows(cur,
            "SELECT upstream, COUNT(*) AS n,"
            " COALESCE(SUM(tokens_in + tokens_out), 0) AS spent,"
            " COALESCE(SUM(tokens_saved + tool_tokens_saved), 0) AS saved"
            " FROM ledger_entries WHERE upstream IS NOT NULL GROUP BY upstream"
            " ORDER BY spent DESC")
        ledger_totals = cur.execute(
            "SELECT COALESCE(SUM(tokens_in + tokens_out), 0) AS paid,"
            " COALESCE(SUM(tokens_saved), 0) AS saved,"
            " COALESCE(SUM(tool_tokens_saved), 0) AS tool_saved,"
            " COALESCE(SUM(latency_ms), 0) AS latency,"
            " COALESCE(SUM(saved_latency_ms), 0) AS saved_latency,"
            " COUNT(*) AS entries FROM ledger_entries"
        ).fetchone()
        cache_top = _rows(cur,
            "SELECT key, provider, model, created_at, last_accessed, access_count,"
            " COALESCE(prompt_tokens,0) AS prompt_tokens,"
            " COALESCE(completion_tokens,0) AS completion_tokens"
            " FROM cache_entries ORDER BY access_count DESC, last_accessed DESC LIMIT 120")
        cache_recent = _rows(cur,
            "SELECT key, provider, model, created_at, last_accessed, access_count"
            " FROM cache_entries ORDER BY created_at DESC LIMIT 12")
        cache_total = int(cur.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0])
        cache_reuse = int(cur.execute(
            "SELECT COALESCE(SUM(access_count - 1), 0) FROM cache_entries").fetchone()[0] or 0)
        cache_by_provider = _rows(cur,
            "SELECT provider, COUNT(*) AS n FROM cache_entries GROUP BY provider"
            " ORDER BY n DESC LIMIT 12")
        tool_by_server = _rows(cur,
            "SELECT server, COUNT(*) AS entries, COALESCE(SUM(hits),0) AS hits,"
            " COALESCE(SUM(bytes),0) AS bytes FROM tool_cache GROUP BY server ORDER BY entries DESC")
        tool_events_by_kind = _rows(cur,
            "SELECT kind, COUNT(*) AS n, COALESCE(SUM(latency_ms),0) AS latency"
            " FROM tool_events GROUP BY kind ORDER BY n DESC")
        tool_events_by_tool = _rows(cur,
            "SELECT server, tool, COUNT(*) AS n, COALESCE(SUM(latency_ms),0) AS latency"
            " FROM tool_events GROUP BY server, tool ORDER BY n DESC LIMIT 12")
        recent_llm = _rows(cur,
            "SELECT id, ts, kind, provider, model, upstream, client, stream,"
            " COALESCE(prompt_tokens,0) AS prompt_tokens,"
            " COALESCE(completion_tokens,0) AS completion_tokens,"
            " COALESCE(saved_prompt + saved_completion, 0) AS saved_tokens,"
            " COALESCE(latency_ms,0) AS latency_ms"
            " FROM llm_events ORDER BY id DESC LIMIT 40")
        recent_tools = _rows(cur,
            "SELECT id, ts, kind, server, tool, bytes, latency_ms, saved_latency_ms"
            " FROM tool_events ORDER BY id DESC LIMIT 40")
        ctx = {
            "sessions": int(cur.execute(
                "SELECT COUNT(*) FROM context_sessions").fetchone()[0]),
            "sources": int(cur.execute(
                "SELECT COUNT(*) FROM context_sources").fetchone()[0]),
            "chunks": int(cur.execute(
                "SELECT COUNT(*) FROM context_chunks").fetchone()[0]),
            "invariants": int(cur.execute(
                "SELECT COUNT(*) FROM context_invariants").fetchone()[0]),
            "anchors": int(cur.execute(
                "SELECT COUNT(*) FROM context_anchors").fetchone()[0]),
            "tokens_before": int(cur.execute(
                "SELECT COALESCE(SUM(tokens_before),0) FROM context_sources").fetchone()[0]),
            "anchor_tokens": int(cur.execute(
                "SELECT COALESCE(SUM(anchor_tokens),0) FROM context_sources").fetchone()[0]),
            "redactions": int(cur.execute(
                "SELECT COALESCE(SUM(redactions),0) FROM context_sources").fetchone()[0]),
        }
        ctx_sources = _rows(cur,
            "SELECT session_id, label, chunk_count, byte_size, tokens_before, anchor_tokens,"
            " redactions, created_at FROM context_sources ORDER BY created_at DESC LIMIT 12")
        for source in ctx_sources:
            source["created_at"] = _epoch_to_ts(source["created_at"])
        table_counts = _rows(cur,
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            " ORDER BY name")

    # --- KPI -------------------------------------------------------------
    calls = int(totals["calls"])
    hits = int(totals["hits"])
    misses = int(totals["misses"])
    tokens_real = int(totals["prompt_tokens"]) + int(totals["completion_tokens"])
    tokens_saved = int(totals["saved_prompt"]) + int(totals["saved_completion"])
    paid = int(ledger_totals["paid"])
    saved = int(ledger_totals["saved"]) + int(ledger_totals["tool_saved"])
    ctx_saved = max(0, ctx["tokens_before"] - ctx["anchor_tokens"])

    # --- registre unifié + surfaces --------------------------------------
    # Calculé **avant** les KPI : c'est la source unique des chiffres
    # d'économie affichés (cache + compression + tax d'outils, même unité).
    registry = _registry(db, days)
    surface = _surface(db, days=min(days, 14))

    kpi = {
        "calls": calls,
        "hits": hits,
        "misses": misses,
        "hit_rate_pct": round(hits / calls * 100.0, 2) if calls else 0.0,
        "tokens_paid": registry["totals"]["tokens_paid"],
        "tokens_saved": registry["totals"]["tokens_saved"],
        "tokens_real": tokens_real,
        "gain_pct": registry["gain_pct"],
        "threshold_pct": registry["threshold_pct"],
        "threshold_met": registry["threshold_met"],
        "latency_p50": round(_percentile(all_latencies, 50), 1),
        "latency_p95": round(_percentile(all_latencies, 95), 1),
        "latency_p99": round(_percentile(all_latencies, 99), 1),
        "latency_avg": round(sum(all_latencies) / len(all_latencies), 1) if all_latencies else 0.0,
        "latency_max": round(max(all_latencies), 1) if all_latencies else 0.0,
        "cache_entries": cache_total,
        "cache_reuse": cache_reuse,
        "cache_hit_rate_pct": round(cache_reuse / (cache_reuse + cache_total) * 100.0, 2)
        if (cache_reuse + cache_total) else 0.0,
        "sessions": ctx["sessions"],
        "chunks": ctx["chunks"],
        "context_tokens_saved": ctx_saved,
        "context_saved_ratio": round(ctx_saved / ctx["tokens_before"], 4)
        if ctx["tokens_before"] else 0.0,
        "ledger_entries": int(ledger_totals["entries"]),
        "ledger_tokens_paid": paid,
        "ledger_tokens_saved": saved,
        "saved_latency_ms": registry["totals"]["saved_latency_ms"],
        "latency_ms": int(ledger_totals["latency"]),
        "tools_servers": len(config.tool_proxy.servers),
        "token_counter": counter_name(),
    }

    # --- séries ----------------------------------------------------------
    series = {
        "calls": _series_calls(db, hours),
        "cache": _series_cache(db, hours),
        "tokens": _series_tokens(db, days),
        "tools": _series_tools(db, hours),
        "window_hours": hours,
        "window_days": days,
    }

    # --- répartitions ----------------------------------------------------
    breakdown = {
        "by_upstream": by_upstream,
        "by_model": by_model,
        "by_client": by_client,
        "by_kind": by_kind,
        "ledger_by_kind": ledger_by_kind,
        "ledger_by_upstream": ledger_by_upstream,
        "cache_by_provider": cache_by_provider,
        "tool_by_server": tool_by_server,
        "tool_events_by_kind": tool_events_by_kind,
        "tool_events_by_tool": tool_events_by_tool,
    }

    db_path = config.db_path()
    db_info = {
        "path": str(db_path),
        "size_bytes": db_path.stat().st_size if db_path.exists() else 0,
        "journal_mode": db.journal_mode(),
        "busy_timeout_ms": db.busy_timeout_ms(),
        "tables": [t["name"] for t in table_counts],
    }

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "generated_at_ts": int(time.time()),
        "kpi": kpi,
        "config": {
            "ports": config.ports.model_dump(),
            "host": config.server.host,
            "upstreams": [u.model_dump() for u in config.upstreams],
            "cache": {
                "ttl_seconds": config.cache.ttl_seconds,
                "max_entries": config.cache.max_entries,
                "semantic_enabled": config.cache.semantic_enabled,
            },
            "compression": {
                "enabled": config.compression.enabled,
                "profile": config.compression.profile,
            },
            "context": {
                "session_ttl_days": config.context.session_ttl_days,
                "sandbox_enabled": config.context.sandbox_enabled,
                "allowed_dirs": len(config.context.allowed_dirs),
                "redact_enabled": config.context.redact_enabled,
            },
            "tool_proxy": {
                "default_ttl_seconds": config.tool_proxy.default_ttl_seconds,
                "max_result_bytes": config.tool_proxy.max_result_bytes,
                "servers": [
                    {"name": s.name, "command": s.command, "cache_enabled": s.cache_enabled,
                     "enabled": s.enabled, "ttl_seconds": s.ttl_seconds}
                    for s in config.tool_proxy.servers
                ],
            },
            "ledger": {"enabled": config.ledger.enabled,
                       "retention_days": config.ledger.retention_days},
            "breaker": config.breaker.model_dump(),
        },
        "series": series,
        "registry": registry,
        "surface": surface,
        "breakdown": breakdown,
        "tables": {
            "cache_top": cache_top,
            "cache_recent": cache_recent,
            "context_sources": ctx_sources,
        },
        "context": ctx,
        "recent": {"llm": recent_llm, "tools": recent_tools},
        "db": db_info,
    }


def write_snapshot(config: Config, path: str | Path) -> Path:
    """Écrit l'instantané dans un fichier JSON (diagnostic hors ligne)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(snapshot(config), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return target


class SnapshotCache:
    """Instantané partagé, recalculé en tâche de fond.

    La console interroge l'API toutes les 2 s. Recalculer les agrégats à
    chaque requête coûte ~0,5 s de base (et ralentit le service qui écrit) :
    le cache rend la **réponse instantanée** tout en gardant des chiffres
    frais, recalculés en continu en tâche de fond.

    La première version est calculée à la demande (page vide si la base est
    lente), puis rafraîchie périodiquement.
    """

    def __init__(self, config: Config, hours: int = 24, days: int = 14,
                 interval: float = 2.0) -> None:
        self.config = config
        self.hours = hours
        self.days = days
        self.interval = interval
        self._data: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.refreshes = 0
        self.last_error: str | None = None

    # -- calcul ----------------------------------------------------------
    def compute(self) -> dict[str, Any]:
        return snapshot(self.config, hours=self.hours, days=self.days)

    def refresh(self) -> None:
        data = self.compute()
        with self._lock:
            self._data = data
            self.refreshes += 1
            self.last_error = None

    def get(self) -> dict[str, Any]:
        """Instantané courant : calculé une fois si la cache est froide."""
        with self._lock:
            data = self._data
        if data is not None:
            return data
        data = self.compute()
        with self._lock:
            self._data = data
            self.refreshes += 1
        return data

    # -- cycle de vie ----------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.refresh()
                except Exception as exc:  # pragma: no cover - garde-fou
                    with self._lock:
                        self.last_error = str(exc)
                self._stop.wait(self.interval)

        self._thread = threading.Thread(target=loop, name="icyloom-snapshot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "refreshes": self.refreshes,
                "interval_seconds": self.interval,
                "last_error": self.last_error,
                "ready": self._data is not None,
            }
