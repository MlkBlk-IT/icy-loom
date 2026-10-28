"""Faux amont qui fait **écho** de la requête.

Contrairement au faux amont générique (toujours la même réponse), celui-ci
renvoie une empreinte déterministe de la requête reçue. C'est ce qui permet
de prouver qu'un hit de cache rend **la bonne** réponse : si le cache se
mélangeait, la réponse contiendrait la empreinte d'un autre prompt.

Empreinte : `echo:<sha256 du corps canonique, 12 caractères>`.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

import uvicorn
from fastapi import FastAPI, Request


def echo_digest(body: Any) -> str:
    """Empreinte stable d'un corps de requête."""
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


class EchoUpstream:
    """Serveur OpenAI-compatible qui renvoie l'empreinte de la requête."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8164,
                 delay_seconds: float = 0.0) -> None:
        self.host = host
        self.port = port
        self.delay_seconds = delay_seconds
        self.calls = 0
        self.bodies: list[Any] = []
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.app = self._build()

    # -- application ------------------------------------------------------
    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.get("/v1/models")
        async def models():
            return {"object": "list",
                    "data": [{"id": "echo-model", "object": "model"}]}

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
            digest = echo_digest(body)
            prompt_tokens = 11
            completion_tokens = 7
            return {
                "id": f"echo-{digest}",
                "object": "chat.completion",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": f"echo:{digest}"},
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
    def start(self) -> "EchoUpstream":
        config = uvicorn.Config(self.app, host=self.host, port=self.port,
                                log_level="error", access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self._server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("le faux amont (echo) n'a pas démarré")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)

    def __enter__(self) -> "EchoUpstream":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
