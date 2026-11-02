"""Serveur MCP unifié : une seule instance, toutes les capacités.

Transports : **stdio**, **streamable-HTTP** (8151), **SSE** (8152). Un client
MCP voit donc un seul serveur, pas deux.

Règles de surface :

* 19 outils, descriptions courtes (chaque schéma coûte des tokens à chaque
  requête — c'est un budget, pas un détail) ;
* `ToolAnnotations` sur chaque outil (lecture seule, destructif, idempotent,
  ouvert sur le monde) ;
* toute action destructrice exige `confirm=true` : sans confirmation,
  l'appel est refusé **sans effet de bord** ;
* stdout est réservé au JSON-RPC en transport stdio : les logs partent sur
  stderr.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from . import paths
from .cache import CacheStore
from .config import Config, ToolServerConfig, load_config
from .context.store import ContextStore
from .db import Database
from .ledger import Ledger
from .normalize import Canonicalizer
from .toolproxy import ToolProxy


def _client_of(ctx: Any) -> str:
    """Nom du client MCP : celui déclaré dans le message ``initialize``.

    Sans ce nom, ``client`` reste NULL sur tous les événements d'outils et le
    tableau « par client » du dashboard reste vide.
    """
    try:
        params = getattr(getattr(ctx, "session", None), "client_params", None)
        info = getattr(params, "clientInfo", None)
        name = getattr(info, "name", None)
        if name:
            return str(name)
    except Exception:      # pragma: no cover - SDK différent
        pass
    return "mcp"

log = logging.getLogger("icyloom.mcp")

#: Outils destructeurs : sans `confirm=true`, l'appel est refusé.
DESTRUCTIVE_CONFIRM_MESSAGE = (
    "Action destructive : rejouer l'appel avec confirm=true."
)


def _needs_confirm(confirm: bool, action: str) -> dict[str, Any] | None:
    """Renvoie un refus si l'action destructive n'est pas confirmée."""
    if confirm:
        return None
    return {
        "ok": False,
        "error": "confirmation_required",
        "action": action,
        "message": DESTRUCTIVE_CONFIRM_MESSAGE,
    }


def active_servers(servers: dict[str, ToolServerConfig]) -> list[str]:
    """Serveurs d'outils actifs : le registre doit tous les indexer.

    Un serveur désactivé est exclu (et c'est volontaire : le registre ne
    doit montrer que ce qui répond).
    """
    return [name for name, srv in servers.items() if srv.enabled]


async def refresh_catalog(tools: ToolProxy, servers: dict[str, ToolServerConfig],
                          interval: float = 300.0) -> None:
    """Indexe les outils de chaque serveur actif, puis recommence.

    Sans cette boucle, le registre ne contient que les serveurs qu'un client
    a déjà appelés : un serveur jamais sollicité reste invisible (c'était le
    cas de ``sqlite``, jamais listé).
    """
    while True:
        for name in active_servers(servers):
            try:
                await tools.list_tools(name)
            except Exception as exc:  # pragma: no cover - serveur indisponible
                log.warning("catalogue %s indisponible : %s", name, exc)
        await asyncio.sleep(interval)


def _lifespan(tools: ToolProxy, config: Config):
    """Cycle de vie du serveur MCP : le catalogue est indexé en tâche de fond.

    L'indexation ne bloque pas le démarrage : elle tourne dans la boucle
    d'événements du serveur MCP (les sessions descendants y sont liées).
    """

    @asynccontextmanager
    async def _span(_server: FastMCP | None = None):
        task = asyncio.create_task(refresh_catalog(tools, config.tool_proxy.servers))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    return _span


def build_server(config: Config) -> FastMCP:
    """Construit l'instance FastMCP et ses 13 outils."""
    db = Database(config)
    db.ensure_schema()
    cache = CacheStore(db, ttl_seconds=config.cache.ttl_seconds,
                       max_entries=config.cache.max_entries)
    canonicalizer = Canonicalizer(rules_path=paths.data_file("normalization_rules.json"))
    context = ContextStore(config)
    tools = ToolProxy(config)
    ledger = Ledger(db, enabled=config.ledger.enabled)

    mcp = FastMCP(
        name=config.server.name,
        instructions=config.server.instructions,
        host=config.server.host,
        port=config.ports.mcp_http,
        lifespan=_lifespan(tools, config),
    )

    # ------------------------------------------------------------------
    # 1. cache
    # ------------------------------------------------------------------
    @mcp.tool(
        name="cache",
        description="Cache LLM : stats | search | normalize | clear.",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False,
            openWorldHint=False,
        ),
    )
    def cache_tool(action: str, query: str = "", body: dict[str, Any] | None = None,
                   limit: int = 10, confirm: bool = False) -> dict[str, Any]:
        """Cache LLM : stats | search | normalize | clear.

        Args:
            action: stats | search | normalize | clear
            query: texte recherché (search) ou texte à normaliser (normalize)
            body: corps de requête à normaliser (normalize)
            limit: nombre maximum de résultats (search)
            confirm: requis à true pour clear
        """
        verb = (action or "").strip().lower()
        if verb == "stats":
            return {"ok": True, "action": "stats", "cache": cache.stats()}
        if verb == "search":
            return {"ok": True, "action": "search", "query": query,
                    "results": cache.search(query, limit=limit)}
        if verb == "normalize":
            payload = body if isinstance(body, dict) else {
                "messages": [{"role": "user", "content": query}]
            }
            return {
                "ok": True,
                "action": "normalize",
                "cache_key": canonicalizer.digest(payload),
                "canonical": canonicalizer.canonical_body(payload),
            }
        if verb == "clear":
            refused = _needs_confirm(confirm, "cache.clear")
            if refused:
                return refused
            return {"ok": True, "action": "clear", "removed": cache.clear()}
        return {"ok": False, "error": "unknown_action", "action": verb,
                "allowed": ["stats", "search", "normalize", "clear"]}

    # ------------------------------------------------------------------
    # 2. relais d'outils
    # ------------------------------------------------------------------
    @mcp.tool(
        name="tool_proxy",
        description="Appelle des outils MCP de serveurs descendants (lot).",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def tool_proxy_tool(calls: list[dict[str, Any]]) -> dict[str, Any]:
        """Appelle des outils de serveurs descendants, en lot.

        Args:
            calls: liste de {server, tool, args} ; exécution séquentielle.
        """
        if not isinstance(calls, list) or not calls:
            return {"ok": False, "error": "empty_calls",
                    "message": "calls doit contenir au moins un appel."}
        results = await tools.call_batch(calls)
        return {"ok": True, "count": len(results), "results": results}

    # ------------------------------------------------------------------
    # 3. introspection du relais
    # ------------------------------------------------------------------
    @mcp.tool(
        name="proxy_info",
        description="Introspection du relais d'outils : servers | tools | search | stats | loop.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def proxy_info_tool(what: str, server: str = "", query: str = "") -> dict[str, Any]:
        """Introspection du relais d'outils.

        Args:
            what: servers | tools | search | stats | loop
            server: serveur visé pour what=tools / what=search
            query: texte cherché pour what=search
        """
        topic = (what or "").strip().lower()
        if topic == "servers":
            return {"ok": True, "what": "servers", "servers": tools.server_names()}
        if topic == "tools":
            if not server:
                return {"ok": True, "what": "tools", "servers": tools.server_names(),
                        "message": "Préciser 'server' pour lister les outils d'un serveur."}
            return await tools.list_tools(server)
        if topic == "search":
            if not query:
                return {"ok": False, "error": "empty_query", "what": "search",
                        "message": "Préciser 'query' pour chercher un outil."}
            # le catalogue doit être chargé avant de chercher
            for name in ([server] if server else tools.server_names()):
                await tools.list_tools(name)
            answer = tools.search_tools(query, server)
            answer["what"] = "search"
            return answer
        if topic == "stats":
            return {"ok": True, "what": "stats", "stats": tools.stats()}
        if topic == "loop":
            return {"ok": True, "what": "loop",
                    "guard": {"window_seconds": config.tool_proxy.loop_window_seconds,
                              "warn_after": config.tool_proxy.loop_warn_after,
                              "block_after": config.tool_proxy.loop_block_after}}
        return {"ok": False, "error": "unknown_action", "action": topic,
                "allowed": ["servers", "tools", "search", "stats", "loop"]}

    # ------------------------------------------------------------------
    # 3 bis. sélection dynamique des outils (V2)
    # ------------------------------------------------------------------
    async def _forward_roots(ctx: Any) -> None:
        """Transmet au relais les racines (dossiers de travail) du client.

        Sans cela, un serveur de fichiers placé derrière IcyLoom démarre
        sans aucun dossier autorisé : ses outils répondent « racine
        manquante » alors que le client, lui, a bien déclaré les siennes.
        """
        try:
            listed = await ctx.session.list_roots()
            tools.set_client_roots(list(listed.roots))
        except Exception:
            tools.set_client_roots([])

    @mcp.tool(
        name="search_tools",
        description="Cherche les outils disponibles (index lexical, sans embeddings).",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def search_tools_tool(query: str, server: str = "",
                                top_k: int = 10,
                                ctx: Context = None) -> dict[str, Any]:
        """Cherche les outils correspondant à une intention.

        Args:
            query: intention en langage naturel ("lire un fichier du dépôt")
            server: restreint à un serveur descendant
            top_k: nombre maximal de résultats
        """
        if not (query or "").strip():
            return {"ok": False, "error": "empty_query"}
        if ctx is not None:
            await _forward_roots(ctx)
        for name in ([server] if server else tools.server_names()):
            await tools.list_tools(name)
        answer = tools.search_tools(query, server)
        if answer.get("ok") and top_k:
            answer["matches"] = answer.get("matches", [])[:max(1, int(top_k))]
            answer["count"] = len(answer["matches"])
        return answer

    @mcp.tool(
        name="get_tool_schema",
        description="Contrat d'un outil (schéma compressé) ou schéma complet.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def get_tool_schema_tool(server: str, tool: str,
                                   raw: bool = False,
                                   ctx: Context = None) -> dict[str, Any]:
        """Schéma d'un outil.

        Args:
            server: serveur descendant
            tool: nom de l'outil
            raw: vrai pour le schéma complet (par défaut : contrat compressé)
        """
        if not server or not tool:
            return {"ok": False, "error": "missing_server_or_tool"}
        if ctx is not None:
            await _forward_roots(ctx)
        await tools.list_tools(server)
        return tools.get_tool_schema(server, tool, raw=raw)

    @mcp.tool(
        name="invoke_tool",
        description="Exécute un outil (politique, cache et compression inclus).",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def invoke_tool_tool(server: str, tool: str,
                               args: dict[str, Any] | None = None,
                               role: str = "", intent: str = "",
                               confirm: bool = False,
                               ctx: Context = None) -> dict[str, Any]:
        """Appelle un outil descendant.

        Args:
            server: serveur descendant
            tool: nom de l'outil
            args: arguments de l'appel
            role: rôle de l'appelant (pilote la politique)
            intent: intention déclarée (traçabilité)
            confirm: approuve un appel destructeur ou exigeant approbation
        """
        if not server or not tool:
            return {"ok": False, "error": "missing_server_or_tool"}
        if ctx is not None:
            await _forward_roots(ctx)
        return await tools.call(server, tool, args or {}, role=role,
                                intent=intent, confirmed=confirm,
                                client=_client_of(ctx))

    @mcp.tool(
        name="run_tool_script",
        description="Enchaîne plusieurs appels d'outils dans un bac à sable.",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False,
            openWorldHint=True,
        ),
    )
    async def run_tool_script_tool(script: str, role: str = "",
                                   intent: str = "",
                                   confirm: bool = False,
                                   ctx: Context = None) -> dict[str, Any]:
        """Exécute un script d'outils.

        Le script appelle `await call(server, tool, args)` et renvoie un
        résultat. Sans `confirm`, seuls les outils sans effet de bord sont
        autorisés.

        Args:
            script: source Python du script
            role: rôle de l'appelant
            intent: intention déclarée
            confirm: autorise les outils à effet de bord
        """
        if not (script or "").strip():
            return {"ok": False, "error": "empty_script"}
        if ctx is not None:
            await _forward_roots(ctx)
        return await tools.run_script(script, role=role, intent=intent,
                                      confirmed=confirm)

    @mcp.tool(
        name="tool_policy",
        description="Politique d'exécution : rôles, portée, approbations.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def tool_policy_tool() -> dict[str, Any]:
        """Décrit la politique d'exécution en vigueur."""
        return {"ok": True, "policy": tools.policy.describe(),
                "roles": tools.policy.roles()}

    @mcp.tool(
        name="gateway_stats",
        description="Observabilité : tokens par catégorie, registre, audit.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def gateway_stats_tool(days: int = 30) -> dict[str, Any]:
        """Situation du gateway.

        Args:
            days: période d'agrégation des tokens
        """
        return {"ok": True, "tokens": tools.meter.summary(days),
                "registry": tools.registry.stats(),
                "audit": tools.audit.stats(),
                "policy": tools.policy.describe(),
                "cache": tools.stats()}

    # ------------------------------------------------------------------
    # 4. purge du cache d'outils
    # ------------------------------------------------------------------
    @mcp.tool(
        name="tool_cache_clear",
        description="Vide le cache des résultats d'outils.",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def tool_cache_clear_tool(server: str = "", confirm: bool = False) -> dict[str, Any]:
        """Vide le cache d'outils (un serveur, ou tous).

        Args:
            server: serveur à vider (vide = tous)
            confirm: requis à true
        """
        refused = _needs_confirm(confirm, "tool_cache_clear")
        if refused:
            return refused
        return {"ok": True, "server": server or "*", "result": tools.clear_cache(server or None)}

    # ------------------------------------------------------------------
    # 5. rangement de contexte
    # ------------------------------------------------------------------
    @mcp.tool(
        name="ctx_store",
        description="Range un contenu volumineux dans le tiroir et rend une ancre dense.",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False,
            openWorldHint=False,
        ),
    )
    def ctx_store_tool(source: str, content: str = "", path: str = "", cmd: str = "",
                       session_id: str = "default", label: str = "") -> dict[str, Any]:
        """Range un contenu hors du contexte et rend son ancre.

        Args:
            source: text | file | cmd
            content: texte à ranger (source=text)
            path: fichier à ranger (source=file)
            cmd: commande dont la sortie est rangée (source=cmd)
            session_id: session logique
            label: nom court du contenu
        """
        kind = (source or "").strip().lower()
        try:
            if kind == "text":
                outcome = context.store_text(content, session_id, label)
            elif kind == "file":
                outcome = context.store_file(path, session_id, label)
            elif kind == "cmd":
                outcome = context.store_cmd(cmd, session_id, label)
            else:
                return {"ok": False, "error": "unknown_action", "action": kind,
                        "allowed": ["text", "file", "cmd"]}
        except FileNotFoundError as exc:
            return {"ok": False, "error": "not_found", "message": str(exc)}
        except RuntimeError as exc:
            return {"ok": False, "error": "refused", "message": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "message": str(exc)}
        return {"ok": True, **outcome.as_dict()}

    @mcp.tool(
        name="ctx_get",
        description="Relit un contenu rangé, octet pour octet.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def ctx_get_tool(chunk_id: str) -> dict[str, Any]:
        """Relit un contenu rangé, sans aucune transformation.

        Args:
            chunk_id: identifiant renvoyé par ctx_store (chunk_ids)
        """
        return context.get_chunk(chunk_id)

    @mcp.tool(
        name="ctx_search",
        description="Recherche un contenu dans le tiroir.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def ctx_search_tool(query: str, limit: int = 5, session_id: str = "default") -> dict[str, Any]:
        """Recherche dans le tiroir de contexte.

        Args:
            query: texte cherché
            limit: nombre maximum de résultats
            session_id: session explorée (vide = toutes)
        """
        # "" doit rester "" : le tiroir traite "" et "all" comme « toutes les
        # sessions ». Ecraser "" par "default" rendait la recherche confined
        # à une seule session, alors que l'aide annonce l'inverse.
        return context.search(query, limit=limit, session_id=session_id)

    @mcp.tool(
        name="ctx_verify",
        description="Prouve qu'aucun invariant n'a été perdu par la compression.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def ctx_verify_tool(session_id: str = "") -> dict[str, Any]:
        """Vérifie la couverture des ancres (chaque invariant est-il présent ?).

        Args:
            session_id: session vérifiée (vide = toutes)
        """
        return context.verify(session_id or None)

    @mcp.tool(
        name="ctx_check",
        description="Contrôle de perte d'information sur une session.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def ctx_check_tool(session_id: str = "default") -> dict[str, Any]:
        """Contrôle de perte : invariants manquants et fidélité du contenu.

        Args:
            session_id: session contrôlée
        """
        return context.check(session_id or "default")

    @mcp.tool(
        name="ctx_profile",
        description="Profil de compression : lecture (mode vide) ou changement.",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False,
            openWorldHint=False,
        ),
    )
    def ctx_profile_tool(mode: str = "") -> dict[str, Any]:
        """Profil de compression : strict (défaut) | balanced | aggressive.

        Args:
            mode: profil à activer (vide = lire le profil courant)
        """
        verb = (mode or "").strip().lower()
        if not verb:
            profile = context.profile
            return {"ok": True, "action": "get", "mode": profile.name,
                    "lossy": profile.lossy, "max_anchor_chars": profile.max_anchor_chars}
        return context.set_profile(verb)

    @mcp.tool(
        name="ctx_session",
        description="Sessions du tiroir : list | purge | transfer.",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False,
            openWorldHint=False,
        ),
    )
    def ctx_session_tool(action: str, session_id: str = "", to_session: str = "",
                         chunk_ids: list[int] | None = None,
                         confirm: bool = False) -> dict[str, Any]:
        """Gère les sessions du tiroir.

        Args:
            action: list | purge | transfer
            session_id: session concernée
            to_session: session cible (transfer)
            chunk_ids: fragments transférés (transfer)
            confirm: requis à true pour purge
        """
        verb = (action or "").strip().lower()
        if verb == "list":
            return {"ok": True, "action": "list",
                    "sessions": [s.__dict__ for s in context.sessions.list()]}
        if verb == "purge":
            refused = _needs_confirm(confirm, "ctx_session.purge")
            if refused:
                return refused
            return {"ok": True, "action": "purge", **context.sessions.purge(session_id)}
        if verb == "transfer":
            return {"ok": True, "action": "transfer",
                    **context.transfer(session_id, to_session, list(chunk_ids or []))}
        return {"ok": False, "error": "unknown_action", "action": verb,
                "allowed": ["list", "purge", "transfer"]}

    @mcp.tool(
        name="ctx_admin",
        description="Administration du tiroir : stats | health | vacuum | purge_expired | rebuild_index.",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False,
            openWorldHint=False,
        ),
    )
    def ctx_admin_tool(action: str, session_id: str = "default",
                       confirm: bool = False) -> dict[str, Any]:
        """Administre le tiroir de contexte.

        Args:
            action: stats | health | vacuum | purge_expired | rebuild_index
            session_id: session pour stats
            confirm: requis à true pour vacuum / purge_expired / rebuild_index
        """
        verb = (action or "").strip().lower()
        if verb == "stats":
            return {"ok": True, "action": "stats", "stats": context.stats()}
        if verb == "health":
            return {"ok": True, "action": "health", "health": context.health()}
        if verb in ("vacuum", "purge_expired", "rebuild_index"):
            refused = _needs_confirm(confirm, f"ctx_admin.{verb}")
            if refused:
                return refused
            if verb == "vacuum":
                with db.transaction() as cur:
                    cur.execute("VACUUM")
                return {"ok": True, "action": "vacuum", "vacuumed": True}
            if verb == "purge_expired":
                return {"ok": True, "action": "purge_expired",
                        **context.sessions.purge_expired()}
            return {"ok": True, "action": "rebuild_index",
                    **context.drawer.rebuild_index()}
        return {"ok": False, "error": "unknown_action", "action": verb,
                "allowed": ["stats", "health", "vacuum", "purge_expired", "rebuild_index"]}

    # ------------------------------------------------------------------
    # 13. ledger
    # ------------------------------------------------------------------
    @mcp.tool(
        name="ledger",
        description="Journal économique : ce qui coûte, ce qui économise.",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True,
            openWorldHint=False,
        ),
    )
    def ledger_tool(view: str = "summary", limit: int = 50) -> dict[str, Any]:
        """Journal économique unique (tokens réels, aucun estimé côté amont).

        Args:
            view: summary | daily
            limit: nombre de lignes pour les vues détaillées
        """
        topic = (view or "summary").strip().lower()
        if topic == "summary":
            return {"ok": True, "view": "summary", "ledger": ledger.summary()}
        if topic == "daily":
            return {"ok": True, "view": "daily", "series": ledger.daily(limit)}
        return {"ok": False, "error": "unknown_action", "action": topic,
                "allowed": ["summary", "daily"]}

    return mcp


def run_mcp(config: Config, transport: str = "stdio", host: str | None = None,
            port: int | None = None) -> None:
    """Lance le serveur MCP sur le transport demandé."""
    mcp = build_server(config)
    target_host = host or config.server.host
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    if transport == "sse":
        mcp.settings.host = target_host
        mcp.settings.port = port or config.ports.mcp_sse
        mcp.run(transport="sse")
        return
    mcp.settings.host = target_host
    mcp.settings.port = port or config.ports.mcp_http
    mcp.run(transport="streamable-http")
