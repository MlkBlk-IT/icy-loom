"""Tests du dashboard : agrégat de métriques et routes de la console."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from icyloom.admin import create_app
from icyloom.dashboard import dashboard_html, snapshot


def test_snapshot_shape(config):
    """L'instantané expose toutes les sections attendues par la console."""
    data = snapshot(config)
    for key in ("kpi", "config", "series", "breakdown", "tables", "context", "recent", "db"):
        assert key in data, key

    kpi = data["kpi"]
    for key in ("calls", "hits", "misses", "hit_rate_pct", "tokens_paid", "tokens_saved",
                "gain_pct", "threshold_pct", "latency_p50", "latency_p95", "cache_entries",
                "sessions", "token_counter"):
        assert key in kpi, key

    assert data["series"]["calls"] == []
    assert data["series"]["tokens"] == []
    assert data["db"]["journal_mode"].lower() in ("wal", "delete", "truncate", "persist", "memory")


def test_snapshot_reflects_events(config):
    """Un événement journalisé se retrouve dans les KPI et dans le journal."""
    from icyloom.db import Database

    db = Database(config)
    db.ensure_schema()
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO llm_events (kind, provider, model, stream, prompt_tokens,"
            " completion_tokens, saved_prompt, saved_completion, latency_ms, upstream)"
            " VALUES ('miss','fake','m1',0,100,20,0,0,42,'fake')"
        )
        cur.execute(
            "INSERT INTO llm_events (kind, provider, model, stream, prompt_tokens,"
            " completion_tokens, saved_prompt, saved_completion, latency_ms, upstream)"
            " VALUES ('hit','fake','m1',0,0,0,120,20,3,'fake')"
        )
        cur.execute(
            "INSERT INTO ledger_entries (kind, upstream, model, tokens_in, tokens_out,"
            " tokens_saved, latency_ms) VALUES ('llm_call','fake','m1',100,20,0,42)"
        )
        cur.execute(
            "INSERT INTO ledger_entries (kind, upstream, model, tokens_saved, latency_ms)"
            " VALUES ('llm_cache_hit','fake','m1',140,0)"
        )

    data = snapshot(config)
    assert data["kpi"]["calls"] == 2
    assert data["kpi"]["hits"] == 1
    assert data["kpi"]["hit_rate_pct"] == 50.0
    assert data["kpi"]["tokens_paid"] == 120
    assert data["kpi"]["tokens_saved"] == 140
    assert data["kpi"]["gain_pct"] == round(140 / 260 * 100, 2)
    assert data["kpi"]["latency_max"] == 42.0
    assert len(data["recent"]["llm"]) == 2


def test_dashboard_html_is_self_contained():
    """La console ne dépend d'aucun CDN ni d'aucun framework externe."""
    import re

    html = dashboard_html()
    assert "<title>IcyLoom" in html
    # aucune ressource distante : ni script, ni feuille de style, ni police
    assert 'src="http' not in html
    assert "src='http" not in html
    assert "@import" not in html
    assert "url(http" not in html
    # Aucune ressource *chargée* depuis un domaine distant : les seules URL
    # autorisées sont le namespace SVG et la boucle locale. Un lien de
    # navigation (`<a href>`) vers le dépôt ne charge rien et reste autorisé.
    loaded = re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)["\']', html)
    for url in loaded:
        assert (url.startswith("http://www.w3.org/2000/svg")
                or url.startswith("http://127.0.0.1")
                or "github.com/MlkBlk-IT/icy-loom" in url), url
    # tout autre domaine distant reste interdit, même en simple lien
    for url in re.findall(r"https?://[^\s\"'<>)]+", html):
        assert (url.startswith("http://www.w3.org/2000/svg")
                or url.startswith("http://127.0.0.1")
                or url.startswith("https://github.com/MlkBlk-IT/icy-loom")), url
    assert "/api/dashboard" in html


def test_dashboard_explains_file_protocol():
    """Ouverte en file://, la page explique où l'ouvrir (pas d'erreur brute)."""
    html = dashboard_html()
    assert "location.protocol" in html
    assert "http://127.0.0.1:8153/dashboard" in html
    assert "Console opened from a file" in html
    # en file://, le démarrage (fetch live) est neutralisé
    assert 'location.protocol === "file:"' in html


def test_dashboard_is_live_and_self_contained():
    """La console interroge l'API en continu et n'embarque aucune ressource distante."""
    html = dashboard_html()
    # flux live : cadence + mesure périodique + débit calculé
    assert "setInterval(poll, 2000)" in html
    assert "/api/dashboard?hours=" in html
    assert "state.rate" in html
    # moteur d'animation (les valeurs ne sautent pas d'un tick à l'autre)
    assert "requestAnimationFrame" in html
    assert "ease" in html
    # graphiques SVG maison (moteur de croquis, aucune librairie)
    assert "const Rough" in html
    for kind in ("drawVolcano", "drawTopo", "drawCalls", "drawLatency",
                 "drawOutcomes", "drawLine", "drawGauge", "drawDonut",
                 "drawBars", "drawTools", "drawFlow", "sparkline"):
        assert kind in html, kind
    # aucun CDN, aucune police distante, aucun framework
    assert "<script src" not in html
    assert "fonts.googleapis" not in html
    assert "cdn.jsdelivr" not in html
    assert "unpkg.com" not in html


def test_dashboard_fuses_the_two_parent_console():
    """La console réunit les vues d'IcyCache et celles de Loom, en anglais."""
    html = dashboard_html()
    # IcyCache : overview, activity, cache, models, guide
    for label in ("Traffic volume 3D", "Topo map", "Cache entries",
                  "Per-model breakdown", "Guide", "Reading the metrics",
                  "The 13 MCP tools", "Shortcuts &amp; tips"):
        assert label in html, label
    # Loom : storage/context, audit, tools, sessions
    for label in ("Context drawer", "Audit live", "Tool relay",
                  "Stored sources", "Tool events", "Domain split"):
        assert label in html, label
    # le registre unifié est la source affichée
    assert "v_token_ledger" in html
    # et la page est en anglais
    assert 'lang="en"' in html


def test_admin_serves_dashboard(config):
    """L'API d'administration sert la page et son instantané."""
    app = create_app(config)
    with TestClient(app) as client:
        for path in ("/", "/dashboard"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert "text/html" in response.headers["content-type"]
            assert "IcyLoom" in response.text

        response = client.get("/api/dashboard")
        assert response.status_code == 200
        payload = response.json()
        assert "kpi" in payload
        assert payload["kpi"]["calls"] == 0
        # JSON strictement sérialisable (la page fait un JSON.parse)
        assert json.loads(json.dumps(payload))["kpi"]["gain_pct"] == 0.0
