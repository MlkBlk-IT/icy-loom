"""Tool calling Gemini à travers la passerelle : faux amont local, zéro réseau.

Ce que ces tests prouvent (ou font échouer) :
  1. le corps envoyé à l'amont est identique à celui du client
     (tools, toolConfig, thoughtSignature, functionResponse) ;
  2. la réponse, en non-stream et en stream, est rendue sans altération ;
  3. une réponse qui contient un functionCall n'est JAMAIS rejouée depuis le cache.

Port de test : 8158 (plage 8154-8159).
"""

from __future__ import annotations

import json
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from icyloom.config import Config, DatabaseConfig, UpstreamConfig
from icyloom.gateway import create_app

PORT = 8158
CALLS: list[dict] = []

SIG = "ZmFrZS10aG91Z2h0LXNpZ25hdHVyZQ=="  # opaque : la passerelle ne doit pas y toucher
FC_PARIS = {"name": "get_weather", "args": {"city": "Paris"}}
FC_LONDON = {"name": "get_weather", "args": {"city": "London"}}
USAGE = {"promptTokenCount": 20, "candidatesTokenCount": 5}

TOOLS = [{"functionDeclarations": [{
    "name": "get_weather",
    "description": "Météo d'une ville",
    "parameters": {"type": "OBJECT",
                   "properties": {"city": {"type": "STRING"}},
                   "required": ["city"]},
}]}]
TOOL_CONFIG = {"functionCallingConfig": {"mode": "ANY",
                                         "allowedFunctionNames": ["get_weather"]}}


def _parts_for(name: str) -> list[dict]:
    if name == "parallel":  # la signature n'est portée que par le premier functionCall
        return [{"functionCall": FC_PARIS, "thoughtSignature": SIG},
                {"functionCall": FC_LONDON}]
    if name == "mixed":  # texte puis functionCall dans la même réponse
        return [{"text": "Je vérifie la météo."},
                {"functionCall": FC_PARIS, "thoughtSignature": SIG}]
    if name == "final":
        return [{"text": "Il fait beau à Paris."}]
    return [{"functionCall": FC_PARIS, "thoughtSignature": SIG}]


def _chunk(parts: list[dict], finish: str | None = None, usage: bool = False) -> str:
    cand: dict = {"content": {"role": "model", "parts": parts}, "index": 0}
    if finish:
        cand["finishReason"] = finish
    out: dict = {"candidates": [cand], "modelVersion": "fake"}
    if usage:
        out["usageMetadata"] = USAGE
    return "data: " + json.dumps(out) + "\n\n"


def build_fake() -> FastAPI:
    app = FastAPI()

    @app.post("/v1beta/models/{m}")
    async def gemini(m: str, request: Request):
        body = await request.json()
        CALLS.append({"model": m, "body": body})
        name, _, action = m.partition(":")
        parts = _parts_for(name)
        finish = "MAX_TOKENS" if name == "cut" else "STOP"
        if action == "streamGenerateContent":
            chunks = [_chunk([p]) for p in parts[:-1]]
            chunks.append(_chunk([parts[-1]], finish=finish, usage=True))
            return StreamingResponse(iter(chunks), media_type="text/event-stream")
        return JSONResponse({
            "candidates": [{"content": {"role": "model", "parts": parts},
                            "finishReason": finish, "index": 0}],
            "usageMetadata": USAGE, "modelVersion": "fake",
        })

    return app


@pytest.fixture(scope="module", autouse=True)
def fake_upstream():
    server = uvicorn.Server(uvicorn.Config(build_fake(), host="127.0.0.1", port=PORT,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ICYLOOM_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ICYTEST_GEM", "gem-test")
    CALLS.clear()
    cfg = Config(
        database=DatabaseConfig(path=str(tmp_path / "t.db")),
        upstreams=[UpstreamConfig(name="gem", base_url=f"http://127.0.0.1:{PORT}",
                                  priority=10, kind="gemini", api_key_env="ICYTEST_GEM")],
    )
    with TestClient(create_app(cfg)) as c:
        yield c


def _body(text: str = "Quelle météo à Paris ?") -> dict:
    return {"contents": [{"role": "user", "parts": [{"text": text}]}],
            "tools": TOOLS, "toolConfig": TOOL_CONFIG,
            "generationConfig": {"temperature": 0}}


def _sse_parts(text: str) -> list[dict]:
    out: list[dict] = []
    for line in text.splitlines():
        if line.startswith("data: "):
            for cand in json.loads(line[6:]).get("candidates", []):
                out.extend(cand.get("content", {}).get("parts", []))
    return out


def _fcs(parts: list[dict]) -> list[dict]:
    return [p for p in parts if "functionCall" in p]


def _events(client) -> list[dict]:
    gw = client.app.state.gateway
    with gw.db.cursor(commit=False) as cur:
        return [dict(r) for r in cur.execute(
            "SELECT kind, stream, prompt_tokens, completion_tokens FROM llm_events ORDER BY id")]


# --- 1. pass-through non-stream --------------------------------------------

def test_nonstream_functioncall_passthrough(client):
    body = _body()
    r = client.post("/v1beta/models/single:generateContent", json=body)
    assert r.status_code == 200
    parts = r.json()["candidates"][0]["content"]["parts"]
    assert parts == [{"functionCall": FC_PARIS, "thoughtSignature": SIG}]
    assert CALLS[0]["body"] == body  # tools + toolConfig intacts
    ev = _events(client)[0]
    assert (ev["prompt_tokens"], ev["completion_tokens"]) == (20, 5)


def test_nonstream_parallel_signature_only_on_first(client):
    r = client.post("/v1beta/models/parallel:generateContent", json=_body())
    parts = r.json()["candidates"][0]["content"]["parts"]
    assert len(_fcs(parts)) == 2
    assert parts[0]["thoughtSignature"] == SIG
    assert "thoughtSignature" not in parts[1]


# --- 2. pass-through stream ------------------------------------------------

@pytest.mark.parametrize("model,expected_calls", [("single", 1), ("parallel", 2)])
def test_stream_functioncall_passthrough(client, model, expected_calls):
    body = _body()
    r = client.post(f"/v1beta/models/{model}:streamGenerateContent?alt=sse", json=body)
    assert r.status_code == 200
    calls = _fcs(_sse_parts(r.text))
    assert len(calls) == expected_calls
    assert calls[0]["thoughtSignature"] == SIG
    assert calls[0]["functionCall"] == FC_PARIS
    assert CALLS[0]["body"] == body
    ev = _events(client)[0]
    assert (ev["prompt_tokens"], ev["completion_tokens"]) == (20, 5)


# --- 3. aller-retour complet : le corps part tel quel ----------------------

def test_roundtrip_body_forwarded_unchanged(client):
    history = [
        {"role": "user", "parts": [{"text": "Quelle météo à Paris ?"}]},
        {"role": "model", "parts": [{"functionCall": FC_PARIS, "thoughtSignature": SIG}]},
        {"role": "user", "parts": [{"functionResponse": {
            "name": "get_weather", "response": {"temp_c": 18, "sky": "clear"}}}]},
    ]
    body = {"contents": history, "tools": TOOLS, "toolConfig": TOOL_CONFIG,
            "generationConfig": {"temperature": 0}}
    r = client.post("/v1beta/models/final:generateContent", json=body)
    assert r.status_code == 200
    assert CALLS[0]["body"] == body
    assert CALLS[0]["body"]["contents"][1]["parts"][0]["thoughtSignature"] == SIG
    assert r.json()["candidates"][0]["content"]["parts"][0]["text"] == "Il fait beau à Paris."


def test_functionresponse_value_is_part_of_cache_key(client):
    def req(temp: int) -> dict:
        return {"contents": [
            {"role": "user", "parts": [{"text": "Quelle météo à Paris ?"}]},
            {"role": "model", "parts": [{"functionCall": FC_PARIS, "thoughtSignature": SIG}]},
            {"role": "user", "parts": [{"functionResponse": {
                "name": "get_weather", "response": {"temp_c": temp}}}]},
        ], "tools": TOOLS, "generationConfig": {"temperature": 0}}

    client.post("/v1beta/models/final:generateContent", json=req(18))
    client.post("/v1beta/models/final:generateContent", json=req(19))
    assert len(CALLS) == 2  # deux résultats d'outil différents ne partagent pas de réponse


# --- 4. un functionCall SE MET EN CACHE et se rejoue à l'identique ---------

@pytest.mark.parametrize("model", ["single", "parallel", "mixed"])
def test_functioncall_cached_and_replayed_nonstream(client, model):
    body = _body()
    r1 = client.post(f"/v1beta/models/{model}:generateContent", json=body)
    r2 = client.post(f"/v1beta/models/{model}:generateContent", json=body)
    assert len(CALLS) == 1, "pas de hit : l'amont a été rappelé"
    assert r2.headers.get("x-icyloom-cache") == "hit"
    assert r2.json() == r1.json()  # functionCall + thoughtSignature identiques
    assert [e["kind"] for e in _events(client)] == ["miss", "hit"]


@pytest.mark.parametrize("model", ["single", "parallel", "mixed"])
def test_functioncall_cached_and_replayed_stream_verbatim(client, model):
    body = _body()
    path = f"/v1beta/models/{model}:streamGenerateContent?alt=sse"
    r1 = client.post(path, json=body)
    r2 = client.post(path, json=body)
    assert len(CALLS) == 1, "pas de hit : l'amont a été rappelé"
    assert r2.headers.get("x-icyloom-cache") == "hit"
    assert r2.text == r1.text  # octet pour octet
    assert _fcs(_sse_parts(r2.text)), "le rejeu a perdu le functionCall"
    assert [e["kind"] for e in _events(client)] == ["miss", "hit"]


def test_stream_replay_requires_same_query(client):
    body = _body()
    base = "/v1beta/models/single:streamGenerateContent"
    client.post(base + "?alt=sse", json=body)
    client.post(base, json=body)  # autre forme de flux : jamais de hit croisé
    assert len(CALLS) == 2


def test_truncated_functioncall_is_never_cached(client):
    body = _body()
    client.post("/v1beta/models/cut:generateContent", json=body)
    client.post("/v1beta/models/cut:generateContent", json=body)
    path = "/v1beta/models/cut:streamGenerateContent?alt=sse"
    client.post(path, json=body)
    client.post(path, json=body)
    assert len(CALLS) == 4, "une réponse MAX_TOKENS a été mise en cache"
