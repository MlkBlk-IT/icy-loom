"""API d'administration et métriques (8153).

Séparée de la passerelle LLM : elle ne relaie aucun trafic de modèle, elle
expose l'état du système (cache, tiroir, ledger, relais d'outils).

Accès distant : si `server.remote_access_token` est défini, les routes
protégées exigent l'en-tête `Authorization: Bearer <token>`. Sinon l'API
reste ouverte sur la boucle locale.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .cache import CacheStore
from .config import Config
from .context.store import ContextStore
from .dashboard import SnapshotCache, dashboard_html, snapshot
from .db import Database
from .ledger import Ledger
from .toolproxy import ToolProxy

log = logging.getLogger("icyloom.admin")

#: Routes qui exigent un jeton quand un jeton est configuré.
def create_app(config: Config) -> FastAPI:
    """Construit l'API d'administration."""
    db = Database(config)
    db.ensure_schema()
    cache = CacheStore(db, ttl_seconds=config.cache.ttl_seconds,
                       max_entries=config.cache.max_entries)
    context = ContextStore(config)
    tools = ToolProxy(config)
    ledger = Ledger(db, enabled=config.ledger.enabled)

    # L'instantané est recalculé en tâche de fond : l'API répond en
    # millisecondes même quand la base est très chargée.
    snapshots = SnapshotCache(config, hours=24, days=14, interval=2.0)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        snapshots.start()
        try:
            yield
        finally:
            snapshots.stop()

    app = FastAPI(title="IcyLoom Admin", version="1.0.0", lifespan=lifespan)
    app.state.config = config
    app.state.snapshots = snapshots

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        token = config.server.remote_access_token
        if token and request.url.path != "/health":
            header = request.headers.get("authorization", "")
            if header != f"Bearer {token}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"status": "healthy", "service": "icyloom-admin"}

    # -- dashboard --------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard():
        """Console HTML : toutes les métriques et les graphiques."""
        return HTMLResponse(dashboard_html())

    @app.get("/assets/logo.svg")
    async def logo():
        """Logo officiel, servi tel quel depuis le paquet.

        Le même fichier est versionné dans le dépôt : la console, le README et
        GitHub partagent donc une seule et même marque.
        """
        path = Path(__file__).resolve().parent / "static" / "logo.svg"
        if not path.exists():
            raise HTTPException(status_code=404, detail="logo absent")
        return FileResponse(path, media_type="image/svg+xml")

    @app.get("/api/dashboard")
    async def dashboard_data(hours: int = 24, days: int = 14):
        """Instantané unique consommé par la console (lecture seule).

        La fenêtre par défaut vient du cache partagé (recalculé en continu
        en tâche de fond) : la réponse est instantanée. Les autres fenêtres
        sont calculées à la demande.
        """
        if (hours, days) == (snapshots.hours, snapshots.days):
            return snapshots.get()
        return snapshot(config, hours=hours, days=days)

    @app.get("/api/gateway")
    async def gateway_state():
        """État living de la passerelle (circuits, amonts) : relais vers 8150.

        Le dashboard reste sur une seule origine : la page ne parle jamais
        directement à un autre port.
        """
        url = f"http://{config.server.host}:{config.ports.gateway_http}/api/status"
        try:
            import httpx

            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(url)
                return {"ok": response.status_code == 200,
                        "status_code": response.status_code,
                        "state": response.json() if response.status_code == 200 else None}
        except Exception as exc:  # pragma: no cover - passerelle absente
            return {"ok": False, "error": str(exc), "state": None}

    @app.get("/api/status")
    async def status():
        return {
            "service": "icyloom",
            "ports": config.ports.model_dump(),
            "cache": cache.stats(),
            "context": context.stats(),
            "tools": tools.stats(),
            "ledger": ledger.summary(),
        }

    @app.get("/api/ledger")
    async def ledger_endpoint():
        return ledger.summary()

    @app.get("/api/ledger/daily")
    async def ledger_daily(days: int = 14):
        return {"series": ledger.daily(days)}

    @app.get("/api/metrics")
    async def metrics():
        with db.cursor(commit=False) as cur:
            row = cur.execute(
                "SELECT COUNT(*) AS calls,"
                " SUM(CASE WHEN kind='hit' THEN 1 ELSE 0 END) AS hits,"
                " COALESCE(SUM(prompt_tokens + completion_tokens),0) AS real_tokens,"
                " COALESCE(SUM(saved_prompt + saved_completion),0) AS saved_tokens"
                " FROM llm_events"
            ).fetchone()
        calls = int(row["calls"])
        hits = int(row["hits"] or 0)
        real = int(row["real_tokens"])
        saved = int(row["saved_tokens"])
        return {
            "calls": calls,
            "hits": hits,
            "hit_rate_pct": round(hits / calls * 100.0, 2) if calls else 0.0,
            "tokens_real": real,
            "tokens_saved": saved,
            "saved_pct": round(saved / (saved + real) * 100.0, 2) if (saved + real) else 0.0,
            "note": "tokens réels (usage amont). Aucune estimation.",
        }

    @app.get("/api/cache/stats")
    async def cache_stats():
        return cache.stats()

    @app.post("/api/cache-clear")
    async def cache_clear(request: Request):
        """Purge du cache LLM. Exige un en-tête de confirmation explicite."""
        if request.headers.get("X-IcyLoom-Confirm", "").lower() not in ("1", "true", "yes"):
            raise HTTPException(status_code=400, detail={
                "error": "confirmation_required",
                "message": "Envoyer X-IcyLoom-Confirm: 1 pour confirmer.",
            })
        payload = await request.json() if await request.body() else {}
        removed = cache.clear(payload.get("key") if isinstance(payload, dict) else None)
        return {"removed": removed}

    @app.get("/api/context/stats")
    async def context_stats():
        return context.stats()

    @app.get("/api/sessions")
    async def sessions():
        return {"sessions": [s.__dict__ for s in context.sessions.list()]}

    @app.post("/api/sessions/purge")
    async def sessions_purge(request: Request):
        """Purge d'une session. Exige un en-tête de confirmation explicite."""
        if request.headers.get("X-IcyLoom-Confirm", "").lower() not in ("1", "true", "yes"):
            raise HTTPException(status_code=400, detail={
                "error": "confirmation_required",
                "message": "Envoyer X-IcyLoom-Confirm: 1 pour confirmer.",
            })
        payload = await request.json() if await request.body() else {}
        session_id = payload.get("session_id") if isinstance(payload, dict) else None
        if not session_id:
            raise HTTPException(status_code=400, detail={"error": "session_id_required"})
        return context.sessions.purge(str(session_id))

    @app.get("/api/tools")
    async def tools_list():
        return {"servers": tools.server_names(), "stats": tools.stats()}

    @app.post("/api/tools/clear")
    async def tools_clear(request: Request):
        if request.headers.get("X-IcyLoom-Confirm", "").lower() not in ("1", "true", "yes"):
            raise HTTPException(status_code=400, detail={
                "error": "confirmation_required",
                "message": "Envoyer X-IcyLoom-Confirm: 1 pour confirmer.",
            })
        payload = await request.json() if await request.body() else {}
        server = payload.get("server") if isinstance(payload, dict) else None
        return tools.clear_cache(server)

    return app


def run_admin(config: Config, host: str | None = None, port: int | None = None) -> None:
    """Lance l'API d'administration (bloquant)."""
    import uvicorn

    uvicorn.run(create_app(config), host=host or config.server.host,
                port=port or config.ports.admin, log_level="warning")
