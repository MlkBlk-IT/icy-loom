"""Passerelle HTTP : compatible OpenAI et Anthropic.

Responsabilités :

* router la requête vers un amont (préfixe > en-tête > corps > défaut) ;
* servir le cache LLM quand la clé canonique correspond ;
* streamer ou rejouer une réponse ;
* journaliser les **tokens réels** remontés par l'amont ;
* alimenter le ledger (coût / gain).

La passerelle ne modifie jamais le corps envoyé à l'amont : seule la forme
utilisée pour calculer la clé de cache est normalisée.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, AsyncIterator, Iterator, Mapping

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from . import paths
from .breaker import BreakerRegistry
from .cache import CacheStore
from .config import Config
from .db import Database
from .ledger import Ledger, LedgerEntry
from .normalize import Canonicalizer
from .pool import ClientPool
from .upstream import UpstreamRegistry

log = logging.getLogger("icyloom.gateway")

#: En-tête permettant de choisir l'amont.
UPSTREAM_HEADER = "X-IcyLoom-Upstream"

#: En-tête de réponse indiquant si la réponse vient du cache.
CACHE_HEADER = "X-IcyLoom-Cache"

#: Marqueur d'un flux stocké BRUT : l'amont est rejoué octet pour octet
#: (functionCall, tool_calls, tool_use, thoughtSignature, ids d'appel).
RAW_MAGIC = "icyloom-raw/1"

#: Fins de génération qui rendent une réponse rejouable. Une réponse tronquée
#: ou refusée (max_tokens, safety, appel d'outil mal formé...) n'est jamais cachée.
_GOOD_FINISH = frozenset({"stop", "tool_calls", "function_call", "end_turn",
                          "tool_use", "stop_sequence"})


def _event_finish_reasons(event: Any) -> list[str]:
    """Fins de génération annoncées par un événement ou une réponse (3 familles d'API)."""
    found: list[str] = []
    if not isinstance(event, dict):
        return found
    for holder in (event, event.get("delta"), event.get("message")):
        if isinstance(holder, dict) and isinstance(holder.get("stop_reason"), str):
            found.append(holder["stop_reason"])
    for choice in event.get("choices") or []:
        if isinstance(choice, dict) and isinstance(choice.get("finish_reason"), str):
            found.append(choice["finish_reason"])
    for cand in event.get("candidates") or []:
        if isinstance(cand, dict) and isinstance(cand.get("finishReason"), str):
            found.append(cand["finishReason"])
    return found


def _finish_ok(reasons: list[str]) -> bool:
    """Vrai si aucune fin de génération anormale n'a été vue."""
    return all(str(r).lower() in _GOOD_FINISH for r in reasons)


def _event_has_tool_call(event: Any) -> bool:
    """Vrai si l'événement porte un appel d'outil (OpenAI, Anthropic ou Gemini)."""
    if not isinstance(event, dict):
        return False
    for cand in event.get("candidates") or []:
        content = cand.get("content") if isinstance(cand, dict) else None
        for part in (content.get("parts") or []) if isinstance(content, dict) else []:
            if isinstance(part, dict) and "functionCall" in part:
                return True
    for choice in event.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        for holder in (choice.get("delta"), choice.get("message")):
            if isinstance(holder, dict) and (holder.get("tool_calls") or holder.get("function_call")):
                return True
    tool_types = ("tool_use", "server_tool_use", "mcp_tool_use")
    block = event.get("content_block")
    if isinstance(block, dict) and block.get("type") in tool_types:
        return True
    content = event.get("content")
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") in tool_types for b in content)
    return False


def _sse_scan(assembled: str) -> tuple[list[str], bool, int]:
    """(fins de génération, appel d'outil vu, nombre d'événements JSON) d'un flux SSE."""
    reasons: list[str] = []
    has_tool = False
    events = 0
    for block in assembled.split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            events += 1
            reasons.extend(_event_finish_reasons(event))
            has_tool = has_tool or _event_has_tool_call(event)
    return reasons, has_tool, events


def _parse_raw(response: Any) -> tuple[dict[str, Any], str] | None:
    """Décode une entrée de flux brut : (en-tête, corps). None si entrée ancienne."""
    if not isinstance(response, str) or not response.startswith(RAW_MAGIC + "\n"):
        return None
    head, _, body = response[len(RAW_MAGIC) + 1:].partition("\n")
    try:
        header = json.loads(head)
    except json.JSONDecodeError:
        return None
    return (header if isinstance(header, dict) else {}), body


class Gateway:
    """Assemble les composants de la passerelle autour d'une configuration."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = Database(config)
        self.db.ensure_schema()
        self.cache = CacheStore(
            self.db,
            ttl_seconds=config.cache.ttl_seconds,
            max_entries=config.cache.max_entries,
        )
        self.canonicalizer = Canonicalizer(
            rules_path=paths.data_file("normalization_rules.json"),
        )
        self.pool = ClientPool()
        self.breakers = BreakerRegistry(
            failure_threshold=config.breaker.failure_threshold,
            reset_seconds=config.breaker.reset_seconds,
            half_open_probes=config.breaker.half_open_probes,
        )
        self.registry = UpstreamRegistry.from_config(config.upstreams)
        self.ledger = Ledger(self.db, enabled=config.ledger.enabled)

    # -- fermeture --------------------------------------------------------
    async def aclose(self) -> None:
        await self.pool.aclose()


def _build_target(base_url: str, path: str, strip_v1: bool, query: str = "") -> str:
    """Assemble l'URL cible sans dupliquer `/v1` (ni toucher à `/v1beta`)."""
    base = base_url.rstrip("/")
    if (base.endswith("/v1") or strip_v1) and (path == "/v1" or path.startswith("/v1/")):
        path = path[3:] or "/"
    url = base + path
    return f"{url}?{query}" if query else url


def api_kind(path: str) -> str:
    """Famille d'API visée par un chemin : openai | anthropic | gemini."""
    if path.startswith("/v1beta") or ":generateContent" in path or ":streamGenerateContent" in path:
        return "gemini"
    if path.endswith("/messages"):
        return "anthropic"
    return "openai"


def _usage_tokens(payload: Any) -> tuple[int, int]:
    """Tokens réels d'une réponse (prompt, completion). 0 si l'amont n'envoie rien."""
    if not isinstance(payload, dict):
        return 0, 0
    meta = payload.get("usageMetadata")  # Gemini natif
    if isinstance(meta, dict):
        try:
            return int(meta.get("promptTokenCount") or 0), int(meta.get("candidatesTokenCount") or 0)
        except (TypeError, ValueError):
            return 0, 0
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    prompt = usage.get("prompt_tokens")
    if prompt is None:
        prompt = usage.get("input_tokens")
    completion = usage.get("completion_tokens")
    if completion is None:
        completion = usage.get("output_tokens")
    try:
        return int(prompt or 0), int(completion or 0)
    except (TypeError, ValueError):
        return 0, 0


def _forward_headers(request: Request, upstream_cfg) -> dict[str, str]:
    """En-têtes à transmettre à l'amont (les secrets ne sont jamais journalisés)."""
    headers: dict[str, str] = {}
    for name in ("authorization", "x-api-key", "x-goog-api-key", "anthropic-version",
                 "anthropic-beta", "content-type", "accept"):
        value = request.headers.get(name)
        if value:
            headers[name] = value
    present = {k.lower() for k in headers}
    token = __import__("os").environ.get(upstream_cfg.api_key_env) if upstream_cfg.api_key_env else None
    kind = getattr(upstream_cfg, "kind", "openai")
    if kind == "anthropic":
        if token and not ({"x-api-key", "authorization"} & present):
            headers["x-api-key"] = token
        if "anthropic-version" not in present:
            headers["anthropic-version"] = upstream_cfg.anthropic_version
    elif kind == "gemini":
        if token and not ({"x-goog-api-key", "authorization"} & present):
            headers["x-goog-api-key"] = token
    elif token and "authorization" not in present:
        headers["authorization"] = f"Bearer {token}"
    return headers


def create_app(config: Config) -> FastAPI:
    """Construit l'application FastAPI de la passerelle."""
    gateway = Gateway(config)

    app = FastAPI(title="IcyLoom Gateway", version="1.0.0")
    app.state.gateway = gateway

    # ------------------------------------------------------------------
    # interne
    # ------------------------------------------------------------------
    def _persist(cache_row: tuple | None = None,
                 llm_event: tuple | None = None,
                 ledger_row: tuple | None = None,
                 cache_bump: str | None = None) -> None:
        """Écrit le cache, le journal LLM et le ledger dans **une seule**
        transaction.

        Trois transactions successives coûtaient une vingtaine de millisecondes
        par requête (trois écritures WAL). Le gain du cache se mesure en
        millisecondes : l'overhead d'écriture doit rester marginal.

        `cache_bump` incrémente le compteur d'accès d'une entrée existante
        dans cette même transaction (chemin « hit »).
        """
        if cache_row is None and llm_event is None and ledger_row is None and cache_bump is None:
            return
        with gateway.db.transaction() as cur:
            if cache_row is not None:
                cur.execute(
                    "INSERT INTO cache_entries"
                    " (key, normalized, original, response, is_stream, provider, model,"
                    "  raw_hash, prompt_tokens, completion_tokens)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(key) DO UPDATE SET"
                    " normalized=excluded.normalized, original=excluded.original,"
                    " response=excluded.response, is_stream=excluded.is_stream,"
                    " provider=excluded.provider, model=excluded.model,"
                    " prompt_tokens=excluded.prompt_tokens,"
                    " completion_tokens=excluded.completion_tokens,"
                    " created_at=datetime('now'), last_accessed=datetime('now'),"
                    " access_count=cache_entries.access_count + 1",
                    cache_row,
                )
            if llm_event is not None:
                cur.execute(
                    "INSERT INTO llm_events"
                    " (kind, cache_key, provider, model, stream, prompt_tokens,"
                    "  completion_tokens, saved_prompt, saved_completion, latency_ms,"
                    "  upstream, client)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    llm_event,
                )
            if ledger_row is not None:
                cur.execute(
                    "INSERT INTO ledger_entries"
                    " (kind, session_id, client, upstream, model, tokens_in, tokens_out,"
                    "  tokens_saved, tool_tokens_saved, latency_ms, saved_latency_ms, meta)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    ledger_row,
                )
            if cache_bump is not None:
                gateway.cache.bump_access(cur, cache_bump)

    def _log_llm_event(kind: str, key: str, provider: str | None, model: str | None,
                        stream: bool, prompt_tokens: int, completion_tokens: int,
                        saved_prompt: int, saved_completion: int, latency_ms: int,
                        upstream: str | None, client: str | None) -> None:
        with gateway.db.transaction() as cur:
            cur.execute(
                "INSERT INTO llm_events"
                " (kind, cache_key, provider, model, stream, prompt_tokens,"
                "  completion_tokens, saved_prompt, saved_completion, latency_ms,"
                "  upstream, client)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (kind, key, provider, model, int(stream), prompt_tokens, completion_tokens,
                 saved_prompt, saved_completion, latency_ms, upstream, client),
            )

    def _cache_hit_response(entry, request: Request, key: str) -> JSONResponse:
        """Rejoue une entrée de cache et journalise l'économie de l'opération."""
        payload = entry.payload()
        prompt_tokens = int(entry.prompt_tokens or 0)
        completion_tokens = int(entry.completion_tokens or 0)
        _persist(
            llm_event=(
                "hit", key, entry.provider, entry.model, int(entry.is_stream),
                0, 0, prompt_tokens, completion_tokens, 0,
                entry.provider, request.headers.get("x-client"),
            ),
            ledger_row=(
                "llm_cache_hit", None, request.headers.get("x-client"), entry.provider,
                entry.model, 0, 0, prompt_tokens + completion_tokens, 0, 0, 0,
                json.dumps({"cache_key": key}, ensure_ascii=False),
            ),
            cache_bump=key,
        )
        return JSONResponse(
            payload if payload is not None else {"content": entry.response},
            headers={CACHE_HEADER: "hit"},
        )

    def _cache_hit_stream_response(entry, request: Request, key: str,
                                   fmt: str = "openai") -> StreamingResponse:
        """Rejoue en SSE, dans le format de l'API appelée, un flux deja assemble en cache."""
        prompt_tokens = int(entry.prompt_tokens or 0)
        completion_tokens = int(entry.completion_tokens or 0)
        _persist(
            llm_event=(
                "hit", key, entry.provider, entry.model, 1,
                0, 0, prompt_tokens, completion_tokens, 0,
                entry.provider, request.headers.get("x-client"),
            ),
            ledger_row=(
                "llm_cache_hit", None, request.headers.get("x-client"), entry.provider,
                entry.model, 0, 0, prompt_tokens + completion_tokens, 0, 0, 0,
                json.dumps({"cache_key": key, "stream": True}, ensure_ascii=False),
            ),
            cache_bump=key,
        )
        raw_entry = _parse_raw(entry.response)
        if raw_entry is not None:
            header, raw_text = raw_entry
            raw_pieces = [p for p in re.split(r"(?<=\n\n)|(?<=\r\n\r\n)", raw_text) if p]

            def _raw() -> Iterator[bytes]:
                for piece in raw_pieces:
                    yield piece.encode("utf-8")

            return StreamingResponse(_raw(), media_type=header.get("ct") or "text/event-stream",
                                     headers={CACHE_HEADER: "hit"})
        text = entry.response or ""
        model = entry.model or ""
        cid = f"chatcmpl-icyloom-{uuid.uuid4().hex[:12]}"
        pieces = re.findall(r"\S+\s*|\s+", text)

        def _ev(name: str, data: dict) -> str:
            return f"event: {name}\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"

        def _sse_anthropic() -> Iterator[str]:
            yield _ev("message_start", {"type": "message_start", "message": {
                "id": f"msg_icyloom_{uuid.uuid4().hex[:12]}", "type": "message",
                "role": "assistant", "content": [], "model": model,
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": prompt_tokens, "output_tokens": 1}}})
            yield _ev("content_block_start", {"type": "content_block_start", "index": 0,
                                              "content_block": {"type": "text", "text": ""}})
            yield _ev("ping", {"type": "ping"})
            for piece in pieces:
                yield _ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                  "delta": {"type": "text_delta", "text": piece}})
            yield _ev("content_block_stop", {"type": "content_block_stop", "index": 0})
            yield _ev("message_delta", {"type": "message_delta",
                                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                        "usage": {"output_tokens": completion_tokens}})
            yield _ev("message_stop", {"type": "message_stop"})

        def _sse_gemini() -> Iterator[str]:
            usage_meta = {"promptTokenCount": prompt_tokens,
                          "candidatesTokenCount": completion_tokens,
                          "totalTokenCount": prompt_tokens + completion_tokens}
            for piece in pieces:
                yield "data: " + json.dumps({
                    "candidates": [{"content": {"parts": [{"text": piece}], "role": "model"},
                                    "index": 0}],
                    "usageMetadata": usage_meta, "modelVersion": model,
                }, ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps({
                "candidates": [{"content": {"parts": [{"text": ""}], "role": "model"},
                                "finishReason": "STOP", "index": 0}],
                "usageMetadata": usage_meta, "modelVersion": model,
            }, ensure_ascii=False) + "\n\n"

        if fmt == "anthropic":
            return StreamingResponse(_sse_anthropic(), media_type="text/event-stream",
                                     headers={CACHE_HEADER: "hit"})
        if fmt == "gemini":
            return StreamingResponse(_sse_gemini(), media_type="text/event-stream",
                                     headers={CACHE_HEADER: "hit"})

        def _sse() -> Iterator[str]:
            for piece in re.findall(r"\S+\s*|\s+", text):
                yield "data: " + json.dumps({
                    "id": cid, "object": "chat.completion.chunk", "model": model,
                    "choices": [{"index": 0, "delta": {"content": piece},
                                 "finish_reason": None}],
                }, ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps({
                "id": cid, "object": "chat.completion.chunk", "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": prompt_tokens,
                          "completion_tokens": completion_tokens,
                          "total_tokens": prompt_tokens + completion_tokens},
            }, ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(_sse(), media_type="text/event-stream",
                                 headers={CACHE_HEADER: "hit"})

    # ------------------------------------------------------------------
    # routes LLM
    # ------------------------------------------------------------------
    async def _handle_llm(request: Request, path: str, prefix: str | None = None) -> Any:
        raw = await request.body()
        if raw.strip():
            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise HTTPException(status_code=400, detail={
                    "error": "invalid_json", "message": "corps JSON invalide"})
            if not isinstance(body, dict):
                raise HTTPException(status_code=400, detail={
                    "error": "invalid_body", "message": "le corps doit etre un objet JSON"})
        else:
            body = {}
        body.pop("upstream", None) if prefix else None

        client_key = request.headers.get("x-session-id") or request.client.host if request.client else None
        routed = gateway.registry.resolve(
            prefix=prefix,
            header=request.headers.get(UPSTREAM_HEADER),
            body=body,
            client_key=client_key,
        )
        fmt = api_kind(path)
        if fmt == "gemini":
            # Le modèle est dans l'URL, pas dans le corps : il entre dans la clé.
            model_in_path = path.split("/models/", 1)[-1].split(":", 1)[0]
            key = gateway.canonicalizer.digest(body, scope={"model": model_in_path})
            wants_stream = path.endswith(":streamGenerateContent")
        else:
            key = gateway.canonicalizer.digest(body)
            wants_stream = bool(body.get("stream"))

        entry = gateway.cache.get(key, bump=False)
        if entry is not None and not wants_stream and not entry.is_stream:
            return _cache_hit_response(entry, request, key)
        if (entry is not None and wants_stream and entry.is_stream
                and (fmt != "openai" or path.endswith("/chat/completions"))):
            raw_hit = _parse_raw(entry.response)
            # Forme du flux (ex. ?alt=sse) : on ne rejoue que pour la meme query-string.
            if raw_hit is None or raw_hit[0].get("q", "") == request.url.query:
                return _cache_hit_stream_response(entry, request, key, fmt)

        candidates = [routed.upstream] + gateway.registry.fallback_chain(routed.upstream)
        if routed.source not in ("prefix", "header", "body"):
            same_kind = [c for c in candidates if c.kind == fmt]
            if same_kind:
                candidates = same_kind
        last_error: str | None = None
        for candidate in candidates:
            breaker = gateway.breakers.for_name(candidate.name)
            if not breaker.allows():
                last_error = f"circuit ouvert pour l'amont '{candidate.name}'"
                continue
            target = _build_target(candidate.base_url, path, candidate.strip_v1,
                                   request.url.query)
            headers = _forward_headers(request, candidate)
            started = time.perf_counter()
            try:
                if wants_stream:
                    return await _proxy_stream(request, candidate, target, headers, body,
                                               key, started)
                return await _proxy_json(request, candidate, target, headers, body,
                                         key, started)
            except httpx.HTTPError as exc:
                breaker.record_failure()
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("amont %s en échec (%s) : bascule", candidate.name, last_error)
                continue
        raise HTTPException(status_code=502, detail={
            "error": "upstream_unavailable",
            "message": last_error or "aucun amont disponible",
            "attempted": [c.name for c in candidates],
        })

    async def _proxy_json(request: Request, candidate, target: str, headers: dict[str, str],
                          body: dict[str, Any], key: str, started: float) -> JSONResponse:
        response = await gateway.pool.request(
            request.method, target, candidate.base_url, json=body, headers=headers
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        text = response.text
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        prompt_tokens, completion_tokens = _usage_tokens(payload)
        breaker = gateway.breakers.for_name(candidate.name)
        if response.status_code >= 500:
            breaker.record_failure()
        else:
            breaker.record_success()

        if response.status_code >= 400:
            # Erreur métier : on la remonte telle quelle, sans la mettre en cache.
            _log_llm_event("error", key, candidate.name, body.get("model"),
                            bool(body.get("stream")), prompt_tokens, completion_tokens,
                            0, 0, latency_ms, candidate.name,
                            request.headers.get("x-client"))
            return JSONResponse(
                {"error": {"message": text[:2000], "status": response.status_code}},
                status_code=response.status_code,
            )

        _persist(
            cache_row=(
                key,
                json.dumps(gateway.canonicalizer.canonical_body(body),
                           ensure_ascii=False, sort_keys=True),
                None, text, 0, candidate.name, str(body.get("model") or ""), None,
                prompt_tokens, completion_tokens,
            ) if _finish_ok(_event_finish_reasons(payload)) else None,
            llm_event=(
                "miss", key, candidate.name, body.get("model"), 0,
                prompt_tokens, completion_tokens, 0, 0, latency_ms,
                candidate.name, request.headers.get("x-client"),
            ),
            ledger_row=(
                "llm_call", None, request.headers.get("x-client"), candidate.name,
                str(body.get("model") or ""), prompt_tokens, completion_tokens, 0, 0,
                latency_ms, 0,
                json.dumps({"cache_key": key}, ensure_ascii=False),
            ),
        )
        return JSONResponse(
            payload if payload is not None else {"raw": text},
            headers={CACHE_HEADER: "miss"},
        )

    async def _proxy_stream(request: Request, candidate, target: str, headers: dict[str, str],
                            body: dict[str, Any], key: str,
                            started: float) -> StreamingResponse:
        """Diffuse la réponse SSE de l'amont et met en cache le texte assemblé."""
        client = await gateway.pool.client_for(candidate.base_url)
        req = client.build_request("POST", target, json=body, headers=headers)
        response = await client.send(req, stream=True)
        if response.status_code >= 400:
            await response.aclose()
            raise HTTPException(status_code=response.status_code,
                                detail={"error": "upstream_error",
                                        "status": response.status_code})
        chunks: list[bytes] = []
        usage_payload: dict[str, Any] = {}

        async def _iterator() -> AsyncIterator[bytes]:
            async for raw in response.aiter_bytes():
                chunks.append(raw)
                yield raw
            await response.aclose()
            assembled = b"".join(chunks).decode("utf-8", errors="replace")
            text, usage = _extract_sse_text_and_usage(assembled)
            usage_payload.update(usage)
            finish, has_tool, n_events = _sse_scan(assembled)
            cacheable = n_events > 0 and _finish_ok(finish) and (bool(text) or has_tool)
            ctype = response.headers.get("content-type", "text/event-stream")
            raw_response = (RAW_MAGIC + "\n"
                            + json.dumps({"q": request.url.query, "ct": ctype}) + "\n"
                            + assembled)
            latency_ms = int((time.perf_counter() - started) * 1000)
            prompt_tokens, completion_tokens = _usage_tokens({"usage": usage_payload})
            # Le flux est stocké BRUT (appels d'outils compris) et rejoué à l'identique.
            # Une fin de génération anormale (tronqué, filtré, mal formé) n'est pas cachée.
            _persist(
                cache_row=(
                    key,
                    json.dumps(gateway.canonicalizer.canonical_body(body),
                               ensure_ascii=False, sort_keys=True),
                    None, raw_response, 1, candidate.name, str(body.get("model") or ""), None,
                    prompt_tokens, completion_tokens,
                ) if cacheable else None,
                llm_event=(
                    "miss", key, candidate.name, body.get("model"), 1,
                    prompt_tokens, completion_tokens, 0, 0, latency_ms,
                    candidate.name, request.headers.get("x-client"),
                ),
                ledger_row=(
                    "llm_call", None, request.headers.get("x-client"), candidate.name,
                    str(body.get("model") or ""), prompt_tokens, completion_tokens, 0, 0,
                    latency_ms, 0,
                    json.dumps({"cache_key": key, "stream": True}, ensure_ascii=False),
                ),
            )

        return StreamingResponse(_iterator(), media_type="text/event-stream")

    # ------------------------------------------------------------------
    # routes HTTP
    # ------------------------------------------------------------------
    @app.post("/v1/chat/completions")
    @app.post("/v1/messages")
    @app.post("/v1/embeddings")
    async def llm_endpoint(request: Request):
        return await _handle_llm(request, request.url.path)

    @app.post("/v1beta/models/{model_action}")
    async def gemini_endpoint(model_action: str, request: Request):
        return await _handle_llm(request, request.url.path)

    @app.post("/{upstream}/v1beta/models/{model_action}")
    async def gemini_prefixed_endpoint(upstream: str, model_action: str, request: Request):
        return await _handle_llm(request, f"/v1beta/models/{model_action}", prefix=upstream)

    @app.get("/v1/models")
    async def models_endpoint(request: Request):
        return await _handle_llm(request, "/v1/models")

    @app.api_route("/{upstream}/v1/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def prefixed_endpoint(upstream: str, rest: str, request: Request):
        return await _handle_llm(request, f"/v1/{rest}", prefix=upstream)

    @app.get("/health")
    async def health():
        return {
            "status": "healthy",
            "service": "icyloom",
            "upstreams": gateway.registry.names(),
            "ports": config.ports.model_dump(),
        }

    @app.get("/api/status")
    async def status():
        return {
            "service": "icyloom",
            "cache": gateway.cache.stats(),
            "upstreams": gateway.registry.names(),
            "breakers": [b.__dict__ for b in gateway.breakers.all_status()],
            "ledger": gateway.ledger.summary(),
        }

    @app.get("/api/metrics")
    async def metrics():
        with gateway.db.cursor(commit=False) as cur:
            row = cur.execute(
                "SELECT COUNT(*) AS calls,"
                " SUM(CASE WHEN kind='hit' THEN 1 ELSE 0 END) AS hits,"
                " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
                " COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
                " COALESCE(SUM(saved_prompt),0) AS saved_prompt,"
                " COALESCE(SUM(saved_completion),0) AS saved_completion"
                " FROM llm_events"
            ).fetchone()
        calls = int(row["calls"])
        hits = int(row["hits"] or 0)
        real = int(row["prompt_tokens"]) + int(row["completion_tokens"])
        saved = int(row["saved_prompt"]) + int(row["saved_completion"])
        return {
            "calls": calls,
            "hits": hits,
            "hit_rate_pct": round(hits / calls * 100.0, 2) if calls else 0.0,
            "tokens_real": real,
            "tokens_saved": saved,
            "saved_pct": round(saved / (saved + real) * 100.0, 2) if (saved + real) else 0.0,
            "ledger": gateway.ledger.summary(),
            "note": "tokens réels (usage amont). Aucune estimation.",
        }

    @app.post("/api/cache-clear")
    async def cache_clear(request: Request):
        """Purge du cache LLM. Exige un en-tête de confirmation explicite."""
        if request.headers.get("X-IcyLoom-Confirm", "").lower() not in ("1", "true", "yes"):
            raise HTTPException(status_code=400, detail={
                "error": "confirmation_required",
                "message": "Envoyer l'en-tête X-IcyLoom-Confirm: 1 pour confirmer.",
            })
        payload = await request.json() if await request.body() else {}
        removed = gateway.cache.clear(payload.get("key") if isinstance(payload, dict) else None)
        return {"removed": removed}

    @app.get("/api/ledger")
    async def ledger_endpoint():
        return gateway.ledger.summary()

    @app.get("/api/ledger/daily")
    async def ledger_daily(days: int = 14):
        return {"series": gateway.ledger.daily(days)}

    @app.get("/api/agent-events")
    async def agent_events(limit: int = 50):
        with gateway.db.cursor(commit=False) as cur:
            rows = cur.execute(
                "SELECT id, ts, kind, server, tool, bytes, latency_ms, saved_latency_ms,"
                " detail FROM tool_events ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return {"events": [dict(r) for r in rows]}

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await gateway.aclose()

    return app


def _extract_sse_text_and_usage(assembled: str) -> tuple[str, dict[str, Any]]:
    """Extrait le texte généré et l'usage d'un flux SSE assemblé."""
    text_parts: list[str] = []
    usage: dict[str, Any] = {}
    for block in assembled.split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if isinstance(event.get("usage"), dict):
                usage.update(event["usage"])
            # Anthropic : l'usage d'entrée est dans message_start.message.usage.
            msg = event.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
                usage.update(msg["usage"])
            # Gemini : usageMetadata cumulatif à chaque chunk, le dernier fait foi.
            meta = event.get("usageMetadata")
            if isinstance(meta, dict):
                usage["prompt_tokens"] = meta.get("promptTokenCount") or 0
                usage["completion_tokens"] = meta.get("candidatesTokenCount") or 0
            for cand in event.get("candidates") or []:
                if not isinstance(cand, dict):
                    continue
                content = cand.get("content") or {}
                for part in content.get("parts") or [] if isinstance(content, dict) else []:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        text_parts.append(part["text"])
            for choice in event.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta") or {}
                if isinstance(delta.get("content"), str):
                    text_parts.append(delta["content"])
                message = choice.get("message") or {}
                if isinstance(message.get("content"), str):
                    text_parts.append(message["content"])
            content = event.get("content")
            if isinstance(content, str):
                text_parts.append(content)
            if isinstance(event.get("delta"), dict) and isinstance(event["delta"].get("text"), str):
                text_parts.append(event["delta"]["text"])
    return "".join(text_parts), usage


def run_gateway(config: Config, host: str | None = None, port: int | None = None) -> None:
    """Lance la passerelle (bloquant)."""
    import uvicorn

    app = create_app(config)
    uvicorn.run(app, host=host or config.server.host,
                port=port or config.ports.gateway_http, log_level="warning")
