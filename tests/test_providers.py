"""P1 : passerelle multi-provider (OpenAI, Anthropic, Gemini) contre un faux amont local."""

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

PORT = 8156  # plage de tests 8154-8159
CALLS: list[dict] = []


def _sse(chunks: list[str]):
    def gen():
        for c in chunks:
            yield c
    return StreamingResponse(gen(), media_type="text/event-stream")


def _ev(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def build_fake() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def openai(request: Request):
        body = await request.json()
        CALLS.append({"path": request.url.path, "headers": dict(request.headers), "q": request.url.query})
        if body.get("stream"):
            if "TOOLCALL" in json.dumps(body):
                return _sse([
                    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"f","arguments":"{}"}}]}}]}\n\n',
                    'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n',
                    "data: [DONE]\n\n",
                ])
            return _sse([
                'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n',
                'data: {"choices":[{"delta":{"content":"world"}}]}\n\n',
                'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n',
                "data: [DONE]\n\n",
            ])
        return JSONResponse({"choices": [{"message": {"content": "ok"}}],
                             "usage": {"prompt_tokens": 7, "completion_tokens": 3}})

    @app.post("/v1/messages")
    async def anthropic(request: Request):
        body = await request.json()
        CALLS.append({"path": request.url.path, "headers": dict(request.headers), "q": request.url.query})
        if body.get("stream"):
            return _sse([
                _ev("message_start", {"type": "message_start", "message": {
                    "id": "msg_1", "type": "message", "role": "assistant", "content": [],
                    "model": "claude-x", "stop_reason": None,
                    "usage": {"input_tokens": 25, "output_tokens": 1}}}),
                _ev("content_block_start", {"type": "content_block_start", "index": 0,
                                            "content_block": {"type": "text", "text": ""}}),
                _ev("ping", {"type": "ping"}),
                _ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                            "delta": {"type": "text_delta", "text": "Hi "}}),
                _ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                            "delta": {"type": "text_delta", "text": "there"}}),
                _ev("content_block_stop", {"type": "content_block_stop", "index": 0}),
                _ev("message_delta", {"type": "message_delta",
                                      "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                      "usage": {"output_tokens": 15}}),
                _ev("message_stop", {"type": "message_stop"}),
            ])
        return JSONResponse({"id": "msg_1", "type": "message", "role": "assistant",
                             "content": [{"type": "text", "text": "ok"}], "model": "claude-x",
                             "stop_reason": "end_turn",
                             "usage": {"input_tokens": 11, "output_tokens": 4}})

    @app.post("/v1beta/models/{m}")
    async def gemini(m: str, request: Request):
        CALLS.append({"path": request.url.path, "headers": dict(request.headers), "q": request.url.query})
        if m.endswith(":streamGenerateContent"):
            return _sse([
                'data: {"candidates":[{"content":{"parts":[{"text":"Hel"}]},"index":0}],'
                '"usageMetadata":{"promptTokenCount":9,"candidatesTokenCount":1}}\n\n',
                'data: {"candidates":[{"content":{"parts":[{"text":"lo"}]},"finishReason":"STOP","index":0}],'
                '"usageMetadata":{"promptTokenCount":9,"candidatesTokenCount":2}}\n\n',
            ])
        return JSONResponse({"candidates": [{"content": {"parts": [{"text": "ok"}], "role": "model"},
                                             "finishReason": "STOP", "index": 0}],
                             "usageMetadata": {"promptTokenCount": 9, "candidatesTokenCount": 2}})

    return app


@pytest.fixture(scope="module", autouse=True)
def fake_upstream():
    server = uvicorn.Server(uvicorn.Config(build_fake(), host="127.0.0.1", port=PORT, log_level="error"))
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
    monkeypatch.setenv("ICYTEST_ANT", "sk-ant-test")
    monkeypatch.setenv("ICYTEST_GEM", "gem-test")
    CALLS.clear()
    base = f"http://127.0.0.1:{PORT}"
    cfg = Config(
        database=DatabaseConfig(path=str(tmp_path / "t.db")),
        upstreams=[
            UpstreamConfig(name="oa", base_url=base + "/v1", priority=10, kind="openai"),
            UpstreamConfig(name="ant", base_url=base, priority=50, kind="anthropic",
                           api_key_env="ICYTEST_ANT"),
            UpstreamConfig(name="gem", base_url=base, priority=60, kind="gemini",
                           api_key_env="ICYTEST_GEM"),
        ],
    )
    with TestClient(create_app(cfg)) as c:
        yield c


def _events(client):
    gw = client.app.state.gateway
    with gw.db.cursor(commit=False) as cur:
        return [dict(r) for r in cur.execute(
            "SELECT kind, prompt_tokens, completion_tokens, upstream FROM llm_events ORDER BY id")]


def test_anthropic_auth_routing_and_usage(client):
    r = client.post("/v1/messages", json={"model": "claude-x", "max_tokens": 8,
                                          "messages": [{"role": "user", "content": "alpha one"}]})
    assert r.status_code == 200
    call = CALLS[0]
    assert call["headers"].get("x-api-key") == "sk-ant-test"
    assert call["headers"].get("anthropic-version") == "2023-06-01"
    assert "authorization" not in call["headers"]
    ev = _events(client)[0]
    assert (ev["upstream"], ev["prompt_tokens"], ev["completion_tokens"]) == ("ant", 11, 4)


def test_anthropic_stream_usage_and_replay_format(client):
    body = {"model": "claude-x", "max_tokens": 8, "stream": True,
            "messages": [{"role": "user", "content": "beta two"}]}
    r1 = client.post("/v1/messages", json=body)
    assert "message_start" in r1.text and "message_stop" in r1.text
    ev = _events(client)[0]
    assert (ev["prompt_tokens"], ev["completion_tokens"]) == (25, 15)
    n_calls = len(CALLS)
    r2 = client.post("/v1/messages", json=body)
    assert r2.headers["x-icyloom-cache"] == "hit"
    assert len(CALLS) == n_calls
    texts = []
    for block in r2.text.split("\n\n"):
        lines = dict(l.split(": ", 1) for l in block.splitlines() if ": " in l)
        if lines.get("event") == "content_block_delta":
            texts.append(json.loads(lines["data"])["delta"]["text"])
    assert "".join(texts) == "Hi there"
    assert "event: message_start" in r2.text and "event: message_stop" in r2.text


def test_gemini_auth_query_usage_and_replay(client):
    path = "/v1beta/models/gem-1:streamGenerateContent?alt=sse"
    body = {"contents": [{"role": "user", "parts": [{"text": "gamma three"}]}]}
    r1 = client.post(path, json=body)
    assert r1.status_code == 200
    call = CALLS[0]
    assert call["headers"].get("x-goog-api-key") == "gem-test"
    assert call["q"] == "alt=sse"
    ev = _events(client)[0]
    assert (ev["upstream"], ev["prompt_tokens"], ev["completion_tokens"]) == ("gem", 9, 2)
    n_calls = len(CALLS)
    r2 = client.post(path, json=body)
    assert r2.headers["x-icyloom-cache"] == "hit" and len(CALLS) == n_calls
    out = ""
    for line in r2.text.splitlines():
        if line.startswith("data: "):
            for cand in json.loads(line[6:]).get("candidates", []):
                for part in cand["content"]["parts"]:
                    out += part.get("text", "")
    assert out == "Hello"


def test_gemini_model_in_url_is_part_of_cache_key(client):
    body = {"contents": [{"role": "user", "parts": [{"text": "delta four"}]}]}
    client.post("/v1beta/models/gem-a:generateContent", json=body)
    client.post("/v1beta/models/gem-b:generateContent", json=body)
    assert len(CALLS) == 2
    client.post("/v1beta/models/gem-a:generateContent", json=body)
    assert len(CALLS) == 2


def test_openai_stream_usage_is_recorded(client):
    body = {"model": "m", "stream": True, "messages": [{"role": "user", "content": "epsilon five"}]}
    client.post("/v1/chat/completions", json=body)
    ev = _events(client)[0]
    assert (ev["upstream"], ev["prompt_tokens"], ev["completion_tokens"]) == ("oa", 7, 3)


def test_stream_tool_call_is_cached_and_replayed_verbatim(client):
    body = {"model": "m", "stream": True,
            "messages": [{"role": "user", "content": "zeta six TOOLCALL"}]}
    r1 = client.post("/v1/chat/completions", json=body)
    r2 = client.post("/v1/chat/completions", json=body)
    assert len(CALLS) == 1
    assert r2.headers["x-icyloom-cache"] == "hit"
    assert r2.text == r1.text
