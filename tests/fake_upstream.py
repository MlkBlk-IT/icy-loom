"""Faux amont OpenAI-compatible, utilisé par les tests.

Démarre un vrai serveur HTTP sur un port de test (8154-8159) pour que le
chemin complet soit exercé : pool de clients, circuit breaker, gateway.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Request


class FakeUpstream:
    """Serveur OpenAI-compatible contrôlable (échecs, latence, compteurs)."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8154,
                 delay_seconds: float = 0.0) -> None:
        self.host = host
        self.port = port
        self.delay_seconds = delay_seconds
        self.calls = 0
        self.bodies: list[Any] = []
        self.fail_status: int | None = None
        self.fail_times = 0
        #: quand True, la réponse renvoie le dernier message de l'utilisateur
        #: au lieu d'un texte constant : on peut alors vérifier que le cache
        #: rend bien la réponse de *sa* requête.
        self.echo = False
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self.app = self._build()

    # -- application ------------------------------------------------------
    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.get("/v1/models")
        async def models():
            return {"object": "list", "data": [{"id": "fake-model", "object": "model"}]}

        @app.post("/v1/chat/completions")
        @app.post("/v1/messages")
        async def chat(request: Request):
            self.calls += 1
            try:
                body = await request.json()
            except Exception:
                body = {}
            self.bodies.append(body)
            if self.delay_seconds:
                time.sleep(self.delay_seconds)
            if self.fail_status and (self.fail_times <= 0 or self.fail_times > 0):
                if self.fail_times > 0:
                    self.fail_times -= 1
                payload = {"error": {"message": "upstream en échec", "type": "server_error"}}
                return payload, self.fail_status
            prompt_tokens = 11
            completion_tokens = 7
            content = "réponse factice"
            if self.echo:
                messages = body.get("messages") or []
                for message in reversed(messages):
                    if isinstance(message, dict) and message.get("content"):
                        content = str(message["content"])
                        break
            if body.get("stream"):
                import json as _json
                from fastapi.responses import StreamingResponse

                def _gen():
                    for piece in content.split(" "):
                        yield "data: " + _json.dumps(
                            {"choices": [{"delta": {"content": piece + " "}}]},
                            ensure_ascii=False) + "\n\n"
                    yield "data: " + _json.dumps({"choices": [], "usage": {
                        "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                    }}) + "\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(_gen(), media_type="text/event-stream")
            return {
                "id": f"fake-{self.calls}",
                "object": "chat.completion",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }

        @app.get("/__stats")
        async def stats():
            return {"calls": self.calls}

        return app

    # -- cycle de vie -----------------------------------------------------
    def start(self) -> "FakeUpstream":
        config = uvicorn.Config(self.app, host=self.host, port=self.port,
                                log_level="error", access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self._server.started:
                self._started.set()
                return self
            time.sleep(0.05)
        raise RuntimeError("le faux amont n'a pas démarré")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)

    def fail_for(self, status: int, times: int = 1) -> None:
        """Prochaines requêtes en erreur."""
        self.fail_status = status
        self.fail_times = times

    def __enter__(self) -> "FakeUpstream":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
