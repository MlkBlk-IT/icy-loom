"""Tool calling OpenAI et Anthropic (texte puis appel d'outil) : cache + rejeu brut.

Faux amont local, port 8159 (plage de tests 8154-8159).
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

PORT = 8159
CALLS: list[str] = []


def _ev(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _d(data: dict) -> str:
    return "data: " + json.dumps(data) + "\n\n"


def build_fake() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def openai(request: Request):
        body = await request.json()
        CALLS.append("openai")
        cut = "CUT" in json.dumps(body)
        finish = "length" if cut else "tool_calls"
        if body.get("stream"):
            return StreamingResponse(iter([
                _d({"choices": [{"delta": {"content": "Je vérifie. "}}]}),
                _d({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "get_weather", "arguments": "{\"city\":"}}]}}]}),
                _d({"choices": [{"delta": {"tool_calls": [{"index": 0,
                    "function": {"arguments": "\"Paris\"}"}}]}}]}),
                _d({"choices": [{"delta": {}, "finish_reason": finish}]}),
                _d({"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 5}}),
                "data: [DONE]\n\n",
            ]), media_type="text/event-stream")
        return JSONResponse({"choices": [{"index": 0, "finish_reason": finish, "message": {
            "role": "assistant", "content": "Je vérifie.",
            "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": "get_weather", "arguments": "{\"city\":\"Paris\"}"}}]}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5}})

    @app.post("/v1/messages")
    async def anthropic(request: Request):
        body = await request.json()
        CALLS.append("anthropic")
        cut = "CUT" in json.dumps(body)
        stop = "max_tokens" if cut else "tool_use"
        if body.get("stream"):
            return StreamingResponse(iter([
                _ev("message_start", {"type": "message_start", "message": {
                    "id": "msg_1", "type": "message", "role": "assistant", "content": [],
                    "model": "claude-x", "stop_reason": None,
                    "usage": {"input_tokens": 20, "output_tokens": 1}}}),
                _ev("content_block_start", {"type": "content_block_start", "index": 0,
                                            "content_block": {"type": "text", "text": ""}}),
                _ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                            "delta": {"type": "text_delta", "text": "Je vérifie."}}),
                _ev("content_block_stop", {"type": "content_block_stop", "index": 0}),
                _ev("content_block_start", {"type": "content_block_start", "index": 1,
                    "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                                      "input": {}}}),
                _ev("content_block_delta", {"type": "content_block_delta", "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": "{\"city\":\"Paris\"}"}}),
                _ev("content_block_stop", {"type": "content_block_stop", "index": 1}),
                _ev("message_delta", {"type": "message_delta",
                                      "delta": {"stop_reason": stop, "stop_sequence": None},
                                      "usage": {"output_tokens": 5}}),
                _ev("message_stop", {"type": "message_stop"}),
            ]), media_type="text/event-stream")
        return JSONResponse({"id": "msg_1", "type": "message", "role": "assistant",
            "model": "claude-x", "stop_reason": stop,
            "content": [{"type": "text", "text": "Je vérifie."},
                        {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                         "input": {"city": "Paris"}}],
            "usage": {"input_tokens": 20, "output_tokens": 5}})

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
    monkeypatch.setenv("ICYTEST_ANT", "sk-ant-test")
    CALLS.clear()
    base = f"http://127.0.0.1:{PORT}"
    cfg = Config(
        database=DatabaseConfig(path=str(tmp_path / "t.db")),
        upstreams=[
            UpstreamConfig(name="oa", base_url=base + "/v1", priority=10, kind="openai"),
            UpstreamConfig(name="ant", base_url=base, priority=50, kind="anthropic",
                           api_key_env="ICYTEST_ANT"),
        ],
    )
    with TestClient(create_app(cfg)) as c:
        yield c


TOOLS_OA = [{"type": "function", "function": {
    "name": "get_weather", "description": "meteo",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
TOOLS_ANT = [{"name": "get_weather", "description": "meteo",
              "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}]


def _req(api: str, stream: bool, text: str = "Meteo a Paris ?") -> tuple[str, dict]:
    if api == "openai":
        return "/v1/chat/completions", {"model": "m", "stream": stream, "tools": TOOLS_OA,
                                        "messages": [{"role": "user", "content": text}]}
    return "/v1/messages", {"model": "claude-x", "max_tokens": 64, "stream": stream,
                            "tools": TOOLS_ANT,
                            "messages": [{"role": "user", "content": text}]}


@pytest.mark.parametrize("api", ["openai", "anthropic"])
def test_tool_call_nonstream_cached_identical(client, api):
    path, body = _req(api, False)
    r1 = client.post(path, json=body)
    r2 = client.post(path, json=body)
    assert len(CALLS) == 1
    assert r2.headers["x-icyloom-cache"] == "hit"
    assert r2.json() == r1.json()


@pytest.mark.parametrize("api", ["openai", "anthropic"])
def test_text_then_tool_call_stream_cached_verbatim(client, api):
    path, body = _req(api, True)
    r1 = client.post(path, json=body)
    r2 = client.post(path, json=body)
    assert len(CALLS) == 1
    assert r2.headers["x-icyloom-cache"] == "hit"
    assert r2.text == r1.text
    assert "get_weather" in r2.text and ("tool_use" in r2.text or "tool_calls" in r2.text)


@pytest.mark.parametrize("api", ["openai", "anthropic"])
@pytest.mark.parametrize("stream", [False, True])
def test_truncated_tool_call_is_never_cached(client, api, stream):
    path, body = _req(api, stream, text="Meteo CUT")
    client.post(path, json=body)
    client.post(path, json=body)
    assert len(CALLS) == 2
