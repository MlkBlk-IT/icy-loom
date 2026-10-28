"""Tests de la passerelle HTTP (couche 1).

Exercicen un vrai serveur : un faux amont tourne sur 8154, la passerelle est
appelée via son client ASGI.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from conftest import build_config

from icyloom.gateway import CACHE_HEADER, create_app


@pytest.fixture()
def client(config):
    app = create_app(config)
    with TestClient(app) as test_client:
        yield test_client


def _body(prompt: str = "Bonjour !", model: str = "fake-model") -> dict:
    return {"model": model, "messages": [{"role": "user", "content": prompt}]}


def test_chat_completion_returns_200(client, upstream) -> None:
    response = client.post("/v1/chat/completions", json=_body())
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["choices"][0]["message"]["content"] == "réponse factice"
    assert payload["usage"]["prompt_tokens"] == 11
    assert upstream.calls == 1


def test_second_identical_request_is_a_cache_hit(client, upstream) -> None:
    first = client.post("/v1/chat/completions", json=_body())
    second = client.post("/v1/chat/completions", json=_body())
    assert first.headers.get(CACHE_HEADER) == "miss"
    assert second.headers.get(CACHE_HEADER) == "hit"
    assert second.json()["choices"] == first.json()["choices"]
    assert upstream.calls == 1, "l'amont ne doit pas être rappelé"


def test_cache_event_is_recorded_with_real_tokens(client, upstream, config) -> None:
    client.post("/v1/chat/completions", json=_body())
    client.post("/v1/chat/completions", json=_body())
    from icyloom.db import Database

    with Database(config) as db:
        with db.cursor(commit=False) as cur:
            rows = cur.execute(
                "SELECT kind, prompt_tokens, completion_tokens, saved_prompt, saved_completion"
                " FROM llm_events ORDER BY id"
            ).fetchall()
    kinds = [r["kind"] for r in rows]
    assert kinds == ["miss", "hit"], kinds
    assert rows[0]["prompt_tokens"] == 11
    assert rows[0]["completion_tokens"] == 7
    assert rows[1]["saved_prompt"] == 11
    assert rows[1]["saved_completion"] == 7


def test_ledger_records_cost_and_saving(client, upstream, config) -> None:
    client.post("/v1/chat/completions", json=_body())
    client.post("/v1/chat/completions", json=_body())
    from icyloom.db import Database

    with Database(config) as db:
        with db.cursor(commit=False) as cur:
            rows = cur.execute(
                "SELECT kind, tokens_in, tokens_out, tokens_saved FROM ledger_entries ORDER BY id"
            ).fetchall()
    kinds = {r["kind"] for r in rows}
    assert "llm_call" in kinds and "llm_cache_hit" in kinds
    call = next(r for r in rows if r["kind"] == "llm_call")
    hit = next(r for r in rows if r["kind"] == "llm_cache_hit")
    assert call["tokens_in"] == 11 and call["tokens_out"] == 7
    assert hit["tokens_saved"] == 18
    assert hit["tokens_in"] == 0 and hit["tokens_out"] == 0


def test_different_prompt_is_a_miss(client, upstream) -> None:
    client.post("/v1/chat/completions", json=_body("alpha"))
    response = client.post("/v1/chat/completions", json=_body("beta"))
    assert response.headers.get(CACHE_HEADER) == "miss"
    assert upstream.calls == 2


def test_models_endpoint(client, upstream) -> None:
    response = client.get("/v1/models")
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == "fake-model"


def test_prefix_routing_selects_upstream(config, upstream) -> None:
    """Le préfixe d'URL l'emporte sur le corps."""
    from icyloom.config import UpstreamConfig

    cfg = config.model_copy(deep=True)
    cfg.upstreams = [
        UpstreamConfig(name="fake", base_url="http://127.0.0.1:8154/v1", priority=10),
        UpstreamConfig(name="second", base_url="http://127.0.0.1:8154/v1", priority=20),
    ]
    app = create_app(cfg)
    with TestClient(app) as client:
        client.post("/second/v1/chat/completions", json={**_body(), "upstream": "fake"})
        routed = app.state.gateway.registry.resolve(prefix="second", body={"upstream": "fake"})
    assert routed.upstream.name == "second"
    assert routed.source == "prefix"


def test_failing_upstream_falls_back_to_next(tmp_path, upstream) -> None:
    """Un amont en échec n'est jamais définitivement écarté : on bascule."""
    from icyloom.config import UpstreamConfig

    cfg = build_config(tmp_path)
    cfg.upstreams = [
        UpstreamConfig(name="broken", base_url="http://127.0.0.1:8159/v1", priority=1),
        UpstreamConfig(name="fake", base_url="http://127.0.0.1:8154/v1", priority=10),
    ]
    app = create_app(cfg)
    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json=_body())
    assert response.status_code == 200, response.text
    assert upstream.calls == 1


def test_circuit_breaker_opens_and_resets_for_real(config) -> None:
    """Le breaker s'ouvre après N échecs et se réarme après le délai."""
    from icyloom.breaker import BreakerState, BreakerRegistry

    registry = BreakerRegistry(failure_threshold=2, reset_seconds=0.4, half_open_probes=1)
    breaker = registry.for_name("x")
    assert breaker.state is BreakerState.CLOSED
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert not breaker.allows(), "un breaker ouvert doit refuser"
    time.sleep(0.45)
    assert breaker.state is BreakerState.HALF_OPEN, "le réarmement doit être réel"
    assert breaker.allows(), "une sonde doit être autorisée en demi-ouvert"
    assert not breaker.allows(), "une seule sonde en demi-ouvert"
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


def test_cache_clear_requires_confirmation(client, upstream) -> None:
    client.post("/v1/chat/completions", json=_body())
    assert client.post("/api/cache-clear", json={}).status_code == 400
    confirmed = client.post("/api/cache-clear", json={}, headers={"X-IcyLoom-Confirm": "1"})
    assert confirmed.status_code == 200
    assert confirmed.json()["removed"] >= 1


def test_health_and_metrics(client, upstream) -> None:
    assert client.get("/health").json()["status"] == "healthy"
    client.post("/v1/chat/completions", json=_body())
    metrics = client.get("/api/metrics").json()
    assert metrics["calls"] == 1
    assert metrics["tokens_real"] == 18
    assert "tokens réels" in metrics["note"]


def test_cache_never_serves_another_prompt(config, upstream) -> None:
    """Le cache doit rendre la réponse de SA requête, jamais celle d'une autre.

    L'amont fait écho au prompt : on peut vérifier que chaque hit rend la
    réponse du bon prompt, y compris quand les requêtes sont entrelacées.
    """
    upstream.echo = True
    app = create_app(config)
    prompts = ["Rédige le plan de migration du loader",
               "Quels sont les risques du cache LRU ?",
               "Écris un test pytest pour access_count",
               "Explique FTS5 versus LIKE"]
    with TestClient(app) as client:
        answers: dict[str, str] = {}
        order = [0, 1, 2, 3, 0, 2, 1, 3]
        for index in order:
            body = {"model": "fake-model",
                    "messages": [{"role": "user", "content": prompts[index]}]}
            response = client.post("/v1/chat/completions", json=body)
            content = response.json()["choices"][0]["message"]["content"]
            answers.setdefault(index, content)
            assert content == prompts[index], f"mélange : {content!r}"
    assert len(set(answers.values())) == len(prompts)
    assert upstream.calls == len(prompts), "chaque prompt distinct doit aller à l'amont"


def test_iteration_of_the_same_task_is_not_served_from_cache(client, upstream) -> None:
    """Deux versions d'une même demande (« v1 », « v2 ») = deux misses."""
    upstream.echo = True
    first = client.post("/v1/chat/completions", json=_body("Rédige le plan, v1"))
    second = client.post("/v1/chat/completions", json=_body("Rédige le plan, v2"))
    assert first.headers.get(CACHE_HEADER) == "miss"
    assert second.headers.get(CACHE_HEADER) == "miss", "pas de sur-service"
    assert first.json()["choices"][0]["message"]["content"] != \
        second.json()["choices"][0]["message"]["content"]


def test_streaming_completes_and_is_archived(client, upstream, config) -> None:
    """Régression : le flux SSE se terminait par un NameError.

    ``text, usage_payload.update(...)`` était interprété comme un tuple :
    ``text`` n'était jamais assigné et le générateur levait une NameError
    **après** l'envoi des octets. Le client recevait une coupure
    (« peer closed connection without sending complete message body ») et le
    flux n'était ni journalisé ni mis en cache.
    """
    body = {"model": "fake-model", "stream": True,
            "messages": [{"role": "user", "content": "flux de test"}]}
    response = client.post("/v1/chat/completions", json=body)
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")

    from icyloom.db import Database

    with Database(config) as db:
        with db.cursor(commit=False) as cur:
            rows = cur.execute(
                "SELECT stream FROM llm_events WHERE detail IS NOT NULL"
                " OR stream = 1"
            ).fetchall()
            cached = cur.execute(
                "SELECT COUNT(*) FROM cache_entries"
            ).fetchone()[0]
    assert any(int(r["stream"]) == 1 for r in rows), "le flux n'a pas été journalisé"
    assert cached >= 1, "le texte du flux n'a pas été mis en cache"


def test_streaming_is_replayed_from_cache(client, upstream) -> None:
    """Un flux deja assemble est rejoue en SSE sans retourner a l'amont."""
    body = {"model": "fake-model", "stream": True,
            "messages": [{"role": "user", "content": "flux rejoue depuis le cache"}]}
    first = client.post("/v1/chat/completions", json=body)
    second = client.post("/v1/chat/completions", json=body)
    assert upstream.calls == 1, "le second flux doit etre servi par le cache"
    assert second.headers.get("x-icyloom-cache") == "hit"
    assert "[DONE]" in second.text
    assert second.headers["content-type"].startswith("text/event-stream")


def test_malformed_json_is_rejected_with_400(client, upstream) -> None:
    """Un corps JSON invalide ne doit jamais etre transmis a l'amont."""
    r = client.post("/v1/chat/completions", content=b"{",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    r = client.post("/v1/chat/completions", content=b"[]",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert upstream.calls == 0
