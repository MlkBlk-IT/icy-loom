"""Relais d'outils MCP vers des serveurs descendants.

Trois responsabilités :

1. **Appeler** un outil d'un serveur descendant (unitaire ou en lot).
2. **Mettre en cache** le résultat, avec un TTL par serveur et une
   allowlist d'outils cachables. Un outil d'écriture n'est jamais rejoué
   depuis le cache : le rejouer sauterait l'effet de bord.
3. **Empêcher les boucles** : appeler sans cesse le même outil avec les
   mêmes arguments et obtenir le même résultat est bloqué au bout de N
   appels.

Le cache des résultats est **un seul mécanisme** : un résultat d'outil mis en
cache reste dans la base unifiée, journalisé dans le ledger comme tout le
reste.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from collections import deque
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import Config, ToolServerConfig
from .db import Database
from .ledger import Ledger, LedgerEntry

log = logging.getLogger("icyloom.toolproxy")

#: Indices de nom d'outil considérés comme « d'écriture » : leur résultat ne
#: doit jamais être rejoué depuis le cache.
WRITE_HINTS = (
    "create", "write", "update", "delete", "remove", "destroy", "drop", "purge",
    "push", "commit", "merge", "send", "post", "put", "patch", "edit", "save",
    "upload", "download", "install", "uninstall", "publish", "apply", "move",
    "rename", "rebuild", "reset", "clear", "flush", "exec", "execute", "run_",
    "add_", "set_", "new_", "insert", "deploy", "build", "start_", "stop_", "kill",
)


def is_write_tool(name: str) -> bool:
    """Vrai si l'outil ressemble à une écriture (résultat non rejouable)."""
    lowered = (name or "").lower()
    return any(hint in lowered for hint in WRITE_HINTS)


def cache_key(server: str, tool: str, args: Any, epoch: str = "") -> str:
    """Clé de cache d'un résultat d'outil.

    ``epoch`` est l'époque d'état du serveur : elle change après tout appel
    non rejouable (écriture possible). Une lecture mise en cache avant cette
    écriture n'est donc plus jamais servie.
    """
    payload = json.dumps({"server": server, "tool": tool, "args": args, "epoch": epoch},
                         sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


#: Noms de paramètres désignant un chemin. Un chemin n'est normalisé que si le
#: schéma de l'outil déclare aussi ``type: string`` pour ce paramètre.
_PATH_PARAMS = frozenset({"path", "file", "file_path", "filepath", "filename",
                          "dir", "directory", "cwd", "root"})


def _schema_type(schema: Any) -> str | None:
    kind = schema.get("type") if isinstance(schema, dict) else None
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    return kind if isinstance(kind, str) else None


def canonical_args(value: Any, schema: Any = None, name: str | None = None) -> Any:
    """Normalise des arguments **uniquement** d'après l'``inputSchema`` de l'outil.

    Sans schéma (catalogue jamais chargé) ou pour un paramètre non déclaré,
    les arguments ne sont **pas touchés** : seul l'appel strictement identique
    est reconnu. Avec schéma, on ne fusionne que ce que le type déclaré rend
    équivalent :

    - ``integer`` / ``number`` : ``"8150"`` et ``8150.0`` deviennent ``8150`` ;
    - ``boolean`` : ``"true"`` devient ``true`` ;
    - chemin (paramètre nommé path/file/dir… de type ``string``) : antislash converti
      en ``/`` et séparateur final retiré.

    Jamais : changement de casse, réduction d'espaces dans une chaîne,
    réordonnancement de listes. Deux valeurs réellement différentes (deux
    motifs de recherche, deux ports) ne sont donc jamais fusionnées.
    """
    if not isinstance(schema, dict) or not schema:
        return value
    kind = _schema_type(schema)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, dict):
        props = schema.get("properties")
        props = props if isinstance(props, dict) else {}
        return {k: canonical_args(v, props.get(k), str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [canonical_args(v, schema.get("items"), name) for v in value]
    if isinstance(value, float):
        if kind in ("integer", "number") and value.is_integer():
            return int(value)
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if kind == "integer" and re.fullmatch(r"-?\d+", stripped):
            return int(stripped)
        if kind == "number" and re.fullmatch(r"-?\d+(\.\d+)?", stripped):
            number = float(stripped)
            return int(number) if number.is_integer() else number
        if kind == "boolean" and stripped.lower() in ("true", "false"):
            return stripped.lower() == "true"
        if (kind == "string" and (name or "").lower() in _PATH_PARAMS
                and ("/" in value or "\\" in value)):
            text = value.replace("\\", "/")
            if not re.fullmatch(r"[A-Za-z]:/", text):
                # "src//x" et "src/x" désignent le même chemin
                text = re.sub(r"/{2,}", "/", text)
                if len(text) > 1:
                    text = text.rstrip("/")
            return text
    return value


def canon_key(server: str, tool: str, args: Any, epoch: str = "",
              schema: Any = None) -> str:
    """Clé canonique d'un appel d'outil (reconnaît les quasi-appels déclarés)."""
    payload = json.dumps({"server": server, "tool": tool,
                          "args": canonical_args(args, schema), "epoch": epoch},
                         sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_failure(outcome: dict[str, Any]) -> bool:
    """Vrai si un appel a échoué (erreur du relais ou résultat ``is_error``)."""
    if not outcome.get("ok"):
        return True
    result = outcome.get("result")
    return isinstance(result, dict) and bool(result.get("is_error"))


def args_hash(args: Any) -> str:
    return hashlib.sha256(
        json.dumps(args, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:16]


@dataclass
class LoopGuard:
    """Détecte la répétition d'un même appel d'outil avec le même résultat."""

    window_seconds: int = 300
    warn_after: int = 3
    block_after: int = 5
    max_cycle: int = 3
    _history: deque = field(default_factory=lambda: deque(maxlen=256))

    def record(self, server: str, tool: str, args: Any, result: Any) -> dict[str, Any]:
        """Enregistre un appel et juge s'il est bloquant."""
        signature = (server, tool, args_hash(args), hashlib.sha256(
            json.dumps(result, sort_keys=True, ensure_ascii=False, default=str).encode()
        ).hexdigest()[:16])
        now = time.time()
        self._history.append((now, signature))
        while self._history and now - self._history[0][0] > self.window_seconds:
            self._history.popleft()
        repeats = sum(1 for _ts, sig in self._history if sig == signature)
        if repeats >= self.block_after:
            return {"ok": False, "blocked": True,
                    "reason": "boucle d'agent détectée", "repeats": repeats}
        if repeats >= self.warn_after:
            return {"ok": True, "blocked": False, "warning": "appel répété",
                    "repeats": repeats}
        return {"ok": True, "blocked": False, "repeats": repeats}


class ToolProxy:
    """Relais d'outils descendants."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = Database(config)
        self.db.ensure_schema()
        self.ledger = Ledger(self.db, enabled=config.ledger.enabled)
        self.guard = LoopGuard(
            window_seconds=config.tool_proxy.loop_window_seconds,
            warn_after=config.tool_proxy.loop_warn_after,
            block_after=config.tool_proxy.loop_block_after,
            max_cycle=config.tool_proxy.loop_max_cycle,
        )
        self._servers: dict[str, ToolServerConfig] = {
            s.name: s for s in config.tool_proxy.servers if s.enabled
        }
        #: sessions MCP **persistantes** par serveur descendant : on ne
        #: relance plus un processus à chaque appel (c'était ~800 ms).
        self._live: dict[str, _LiveSession] = {}
        #: catalogue d'outils mis en cache (évite de redemander la liste).
        self._catalog: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        #: nombre de sessions ouvertes (observabilité).
        self.sessions_opened = 0
        #: durée de vie du catalogue d'outils en mémoire (secondes).
        self._catalog_ttl = 300.0
        #: inputSchema par serveur puis par outil (canonisation guidée).
        self._schemas: dict[str, dict[str, dict[str, Any]]] = {}
        #: époque d'état par serveur : elle avance après tout appel non
        #: rejouable (écriture possible) et fait partie de la clé de cache.
        #: Le préfixe de démarrage écarte aussi ce qui date d'avant le
        #: lancement : le serveur a pu changer pendant que le relais dormait.
        self._boot = str(int(time.time() * 1000))
        self._epoch: dict[str, int] = {}
        for srv in self._servers.values():
            for tool_name, enabled in srv.cache_tools.items():
                if enabled and is_write_tool(tool_name):
                    log.warning("cache_tools: %s/%s ressemble à une écriture", srv.name,
                                tool_name)

    # -- catalogue --------------------------------------------------------
    def server_names(self) -> list[str]:
        return sorted(self._servers)

    def server_config(self, name: str) -> ToolServerConfig | None:
        return self._servers.get(name)

    def _cache_allowed(self, server: ToolServerConfig, tool: str) -> bool:
        """Le résultat de cet outil peut-il être rejoué depuis le cache ?

        **Refus par défaut** : seul un outil déclaré ``true`` dans
        ``cache_tools`` est rejouable. Aucune heuristique sur le nom.
        """
        if not server.cache_enabled:
            return False
        return bool(server.cache_tools.get(tool, False))

    def _epoch_of(self, server: str) -> str:
        return f"{self._boot}.{self._epoch.get(server, 0)}"

    def _bump_epoch(self, server: str) -> None:
        self._epoch[server] = self._epoch.get(server, 0) + 1

    def _schema_of(self, server: str, tool: str) -> dict[str, Any] | None:
        return self._schemas.get(server, {}).get(tool)

    async def _ensure_schema(self, server: str, tool: str) -> None:
        """Charge le catalogue du serveur si le schéma de l'outil est inconnu.

        Sans schéma, la reconnaissance d'un appel « presque identique » est
        impossible (le repli est guidé par le schéma de l'outil). Le
        catalogue est mis en cache : ce n'est payé qu'une fois par serveur.
        """
        if tool in self._schemas.get(server, {}):
            return
        if server not in self._servers:
            return
        try:
            await self.list_tools(server)
        except Exception:      # pragma: no cover - catalogue indisponible
            pass

    def _ttl(self, server: ToolServerConfig) -> int:
        return int(server.ttl_seconds or self.config.tool_proxy.default_ttl_seconds)

    # -- cache ------------------------------------------------------------
    def cache_get(self, key: str, ttl: int) -> dict[str, Any] | None:
        # transaction explicite : l'incrément de `hits` doit être persisté
        # (une lecture seule ne commite pas -> le compteur restait à 0).
        with self.db.transaction() as cur:
            row = cur.execute(
                "SELECT result_json, bytes, latency_ms, created_at FROM tool_cache"
                " WHERE key=?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            age = time.time() - _parse_ts(str(row["created_at"]))
            if age > ttl:
                return None
            cur.execute(
                "UPDATE tool_cache SET hits = hits + 1, last_accessed = datetime('now')"
                " WHERE key = ?",
                (key,),
            )
        return {
            "result": json.loads(row["result_json"]),
            "bytes": int(row["bytes"]),
            "latency_ms": int(row["latency_ms"]),
            "cached": True,
        }

    def cache_get_canonical(self, server: str, tool: str, args: Any,
                            ttl: int, epoch: str = "") -> dict[str, Any] | None:
        """Relecture d'un appel **presque identique**.

        La clé exacte a échoué : on tente la clé canonique (espaces, casse des
        clés, « 8150 » vs 8150, séparateurs de chemin). Le repli est
        conservateur : deux valeurs réellement différentes ne se confondent
        jamais.
        """
        ckey = canon_key(server, tool, args, epoch, self._schema_of(server, tool))
        with self.db.transaction() as cur:
            row = cur.execute(
                "SELECT key, result_json, bytes, latency_ms, created_at"
                " FROM tool_cache WHERE canon_key=? AND server=? AND tool=?",
                (ckey, server, tool),
            ).fetchone()
            if row is None:
                return None
            age = time.time() - _parse_ts(str(row["created_at"]))
            if age > ttl:
                return None
            cur.execute(
                "UPDATE tool_cache SET hits = hits + 1, last_accessed = datetime('now')"
                " WHERE key = ?",
                (row["key"],),
            )
        return {
            "result": json.loads(row["result_json"]),
            "bytes": int(row["bytes"]),
            "latency_ms": int(row["latency_ms"]),
            "cached": True,
            "matched": "canonical",
            "matched_key": str(row["key"]),
        }

    def cache_set(self, key: str, server: str, tool: str, result: Any,
                  latency_ms: int, canon: str | None = None) -> int:
        payload = json.dumps(result, ensure_ascii=False, default=str)
        if len(payload.encode("utf-8")) > self.config.tool_proxy.max_result_bytes:
            return 0
        ckey = canon if canon is not None else canon_key(server, tool, {})
        with self.db.transaction() as cur:
            cur.execute(
                "INSERT INTO tool_cache"
                " (key, server, tool, result_json, bytes, latency_ms, ttl, created_at,"
                "  last_accessed, hits, canon_key) VALUES (?,?,?,?,?,?,?,datetime('now'),"
                "  datetime('now'), 0,?)"
                " ON CONFLICT(key) DO UPDATE SET result_json=excluded.result_json,"
                "  bytes=excluded.bytes, latency_ms=excluded.latency_ms,"
                "  created_at=datetime('now'), last_accessed=datetime('now'),"
                "  hits=tool_cache.hits + 1, canon_key=excluded.canon_key",
                (key, server, tool, payload, len(payload.encode("utf-8")), latency_ms,
                 self.config.tool_proxy.default_ttl_seconds, ckey),
            )
        return len(payload.encode("utf-8"))

    def clear_cache(self, server: str | None = None) -> dict[str, Any]:
        """Vide le cache d'outils (tout ou un serveur)."""
        with self.db.transaction() as cur:
            if server:
                cur.execute("DELETE FROM tool_cache WHERE server=?", (server,))
            else:
                cur.execute("DELETE FROM tool_cache")
            removed = int(cur.rowcount or 0)
        return {"removed": removed, "server": server or "*"}

    # -- journal ----------------------------------------------------------
    def _log_event(self, kind: str, server: str, tool: str, args: Any,
                   latency_ms: int, saved_latency_ms: int = 0, detail: str = "") -> None:
        with self.db.transaction() as cur:
            cur.execute(
                "INSERT INTO tool_events"
                " (kind, server, tool, args_hash, bytes, latency_ms, saved_latency_ms, detail)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (kind, server, tool, args_hash(args), 0, latency_ms, saved_latency_ms, detail),
            )

    # -- appels -----------------------------------------------------------
    async def list_tools(self, server: str) -> dict[str, Any]:
        """Liste les outils d'un serveur descendant (catalogue mis en cache)."""
        cfg = self._servers.get(server)
        if cfg is None:
            return {"ok": False, "error": "unknown_server", "server": server,
                    "servers": self.server_names()}
        cached = self._catalog.get(server)
        if cached is not None and (time.time() - cached[0]) < self._catalog_ttl:
            return {"ok": True, "server": server, "count": len(cached[1]),
                    "tools": cached[1], "cached": True}
        started = time.perf_counter()
        try:
            session = await self._live_session(cfg)
            listed = await session.list_tools()
        except Exception as exc:
            await self._drop_session(server)
            self._log_event("tool_error", server, "list_tools", {},
                            int((time.perf_counter() - started) * 1000),
                            detail=f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": type(exc).__name__, "message": str(exc),
                    "server": server}
        tools = [{"name": t.name, "description": (t.description or "")[:300]}
                 for t in listed.tools]
        self._catalog[server] = (time.time(), tools)
        self._schemas[server] = {
            t.name: (getattr(t, "inputSchema", None) or {}) for t in listed.tools
        }
        return {"ok": True, "server": server, "count": len(tools), "tools": tools,
                "cached": False}

    def search_tools(self, query: str, server: str = "") -> dict[str, Any]:
        """Cherche un outil dans le catalogue (par nom ou description)."""
        needle = (query or "").strip().lower()
        if not needle:
            return {"ok": False, "error": "empty_query"}
        matches: list[dict[str, Any]] = []
        for name, (_ts, tools) in self._catalog.items():
            if server and name != server:
                continue
            for tool in tools:
                label = tool["name"].lower()
                description = tool.get("description", "").lower()
                if needle in label:
                    score = 3
                elif needle in description:
                    score = 2
                else:
                    words = [w for w in needle.split() if len(w) > 2]
                    hits = sum(1 for w in words if w in label or w in description)
                    score = 1 if hits else 0
                if score:
                    matches.append({"server": name, "tool": tool["name"],
                                    "description": tool.get("description", ""),
                                    "score": score})
        matches.sort(key=lambda m: (-m["score"], m["server"], m["tool"]))
        return {"ok": True, "query": query, "count": len(matches),
                "matches": matches[:20],
                "catalogued_servers": sorted(self._catalog)}

    async def call(self, server: str, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Appelle un outil descendant, via le cache si possible."""
        cfg = self._servers.get(server)
        if cfg is None:
            return {"ok": False, "error": "unknown_server", "server": server,
                    "servers": self.server_names()}
        args = args or {}
        await self._ensure_schema(server, tool)
        epoch = self._epoch_of(server)
        key = cache_key(server, tool, args, epoch)
        ttl = self._ttl(cfg)
        cacheable = self._cache_allowed(cfg, tool)
        if cacheable:
            cached = self.cache_get(key, ttl)
            matched = "exact"
            if cached is None:
                # appel « presque identique » (selon l'inputSchema) : on tente
                # la clé canonique avant de payer l'amont.
                cached = self.cache_get_canonical(server, tool, args, ttl, epoch)
                matched = "canonical"
            if cached is not None:
                # un HIT compte pour le LoopGuard : une boucle servie par le
                # cache reste une boucle.
                verdict = self.guard.record(server, tool, args, cached["result"])
                if verdict.get("blocked"):
                    self._log_event("tool_blocked", server, tool, args, 0,
                                    detail=str(verdict.get("reason", "")))
                    return {"ok": False, "error": "loop_blocked",
                            "message": verdict.get("reason"), "server": server,
                            "tool": tool, "repeats": verdict.get("repeats")}
                self._log_event("tool_hit", server, tool, args, int(cached["latency_ms"]),
                                saved_latency_ms=int(cached["latency_ms"]),
                                detail=matched)
                self.ledger.record(LedgerEntry(
                    kind="tool_cache_hit", upstream=server, tool_tokens_saved=0,
                    saved_latency_ms=int(cached["latency_ms"]),
                    meta={"tool": tool, "cache_key": key, "matched": matched},
                ))
                answer = {"ok": True, "server": server, "tool": tool,
                          "result": cached["result"], "cached": True}
                if matched == "canonical":
                    answer["matched"] = "canonical"
                if verdict.get("warning"):
                    answer["warning"] = verdict["warning"]
                return answer

        started = time.perf_counter()
        try:
            payload = await self._invoke(cfg, tool, args)
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            if not cacheable:
                # l'effet de bord a pu avoir lieu malgré l'échec
                self._bump_epoch(server)
            self._log_event("tool_error", server, tool, args, latency,
                            detail=f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": type(exc).__name__, "message": str(exc),
                    "server": server, "tool": tool}

        latency = int((time.perf_counter() - started) * 1000)
        if not cacheable:
            # appel non rejouable = écriture possible : les lectures déjà en
            # cache pour ce serveur ne sont plus fiables.
            self._bump_epoch(server)
        verdict = self.guard.record(server, tool, args, payload)
        if verdict.get("blocked"):
            self._log_event("tool_blocked", server, tool, args, latency,
                            detail=str(verdict.get("reason", "")))
            return {"ok": False, "error": "loop_blocked",
                    "message": verdict.get("reason"), "server": server, "tool": tool,
                    "repeats": verdict.get("repeats")}

        self._log_event("tool_call", server, tool, args, latency)
        self.ledger.record(LedgerEntry(
            kind="tool_call", upstream=server, latency_ms=latency,
            meta={"tool": tool},
        ))
        response = {"ok": True, "server": server, "tool": tool, "result": payload,
                    "cached": False, "latency_ms": latency}
        if verdict.get("warning"):
            response["warning"] = verdict["warning"]
        failed = isinstance(payload, dict) and bool(payload.get("is_error"))
        if cacheable and not failed:
            self.cache_set(key, server, tool, payload, latency,
                           canon=canon_key(server, tool, args, epoch,
                                           self._schema_of(server, tool)))
        return response

    async def call_batch(self, calls: Iterable[dict[str, Any]],
                         stop_on_error: bool = True) -> list[dict[str, Any]]:
        """Exécute une série d'appels, dans l'ordre.

        Par défaut le lot **s'arrête à la première erreur** (appel invalide,
        échec amont, résultat ``is_error``, boucle bloquée) : la suite d'un lot
        suppose en général que l'étape précédente a réussi. Les appels non
        exécutés sont rapportés ``skipped_after_error``.
        """
        results: list[dict[str, Any]] = []
        failed_at: int | None = None
        for index, call in enumerate(calls):
            if failed_at is not None and stop_on_error:
                results.append({"ok": False, "index": index,
                                "error": "skipped_after_error",
                                "failed_index": failed_at})
                continue
            if not isinstance(call, dict):
                outcome = {"ok": False, "index": index, "error": "invalid_call"}
            else:
                server = str(call.get("server") or "")
                tool = str(call.get("tool") or "")
                if not server or not tool:
                    outcome = {"ok": False, "index": index,
                               "error": "missing_server_or_tool"}
                else:
                    args = call.get("args") if isinstance(call.get("args"), dict) else {}
                    outcome = await self.call(server, tool, args)
                    outcome["index"] = index
            results.append(outcome)
            if failed_at is None and _is_failure(outcome):
                failed_at = index
        return results

    async def _invoke(self, cfg: ToolServerConfig, tool: str, args: dict[str, Any]) -> Any:
        """Exécute l'appel sur le serveur descendant et normalise le résultat."""
        try:
            session = await self._live_session(cfg)
            result = await session.call_tool(tool, args)
        except Exception:
            # la session persistante est morte : on la jette, la prochaine
            # appel en créera une nouvelle.
            await self._drop_session(cfg.name)
            raise
        content = []
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                content.append(text)
                continue
            data = getattr(block, "data", None)
            if data is not None:
                content.append(str(data))
        payload = {
            "content": content,
            "is_error": bool(getattr(result, "isError", False)),
        }
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            payload["structured"] = structured
        return payload

    def _session(self, cfg: ToolServerConfig):
        """Contexte asynchrone d'une session MCP stdio vers le descendant."""
        params = StdioServerParameters(
            command=cfg.command,
            args=list(cfg.args),
            env={**{k: str(v) for k, v in cfg.env.items()}},
        )
        return _SessionContext(params, self.config.tool_proxy.connect_timeout_seconds,
                               self.config.tool_proxy.call_timeout_seconds)

    # -- sessions persistantes ---------------------------------------------
    async def _live_session(self, cfg: ToolServerConfig) -> "_LiveSession":
        """Session MCP réutilisée (une seule par serveur descendant).

        Relancer un processus descendant à chaque appel coûtait ~800 ms.
        La session reste ouverte et sert tous les appels suivants.
        """
        live = self._live.get(cfg.name)
        if live is not None and not live.broken:
            return live
        if live is not None:
            await live.close()
        params = StdioServerParameters(
            command=cfg.command,
            args=list(cfg.args),
            env={**{k: str(v) for k, v in cfg.env.items()}},
        )
        live = _LiveSession(params, self.config.tool_proxy.connect_timeout_seconds,
                            self.config.tool_proxy.call_timeout_seconds)
        self._live[cfg.name] = live
        self.sessions_opened += 1
        return live

    async def _drop_session(self, server: str) -> None:
        """Ferme et oublie la session d'un serveur (après une erreur)."""
        live = self._live.pop(server, None)
        if live is not None:
            await live.close()

    async def close(self) -> None:
        """Referme toutes les sessions descendants."""
        for server in list(self._live):
            await self._drop_session(server)
        self._catalog.clear()

    # -- statistiques -----------------------------------------------------
    def stats(self) -> dict[str, Any]:
        with self.db.cursor(commit=False) as cur:
            row = cur.execute(
                "SELECT COUNT(*) AS entries, COALESCE(SUM(hits),0) AS hits,"
                " COALESCE(SUM(bytes),0) AS bytes FROM tool_cache"
            ).fetchone()
            by_server = cur.execute(
                "SELECT server, COUNT(*) AS entries, COALESCE(SUM(hits),0) AS hits"
                " FROM tool_cache GROUP BY server"
            ).fetchall()
            events = cur.execute(
                "SELECT kind, COUNT(*) AS n FROM tool_events GROUP BY kind"
            ).fetchall()
        return {
            "cache": dict(row),
            "by_server": [dict(r) for r in by_server],
            "events": [dict(r) for r in events],
            "servers": self.server_names(),
        }


class _SessionContext:
    """Ouvre une session MCP stdio vers un serveur descendant."""

    def __init__(self, params: StdioServerParameters, connect_timeout: float,
                 call_timeout: float) -> None:
        self._params = params
        self._connect_timeout = connect_timeout
        self._call_timeout = call_timeout

    async def __aenter__(self) -> ClientSession:
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        read, write = await self._stack.enter_async_context(stdio_client(self._params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.__aexit__(*exc)


class _LiveSession:
    """Session MCP stdio **réutilisée**, isolée dans un thread dédié.

    Pourquoi un thread : le SDK MCP ouvre la session stdio dans un groupe de
    tâches anyio, qui doit être **entré et quitté dans la même tâche**. Une
    session ouverte depuis une requête MCP puis réutilisée plus tard fait
    exploser le cancel scope (« Attempted to exit a cancel scope that isn't
    the current task's current cancel scope »).

    On loge donc la session descendante dans sa propre boucle d'événements,
    dans un thread daemon. Les appels sont soumis à cette boucle par
    ``run_coroutine_threadsafe`` : le serveur MCP n'attend qu'une future, et
    le processus descendant reste vivant entre les appels (~800 ms gagnés).
    """

    def __init__(self, params: StdioServerParameters, connect_timeout: float,
                 call_timeout: float) -> None:
        self._params = params
        self._connect_timeout = connect_timeout
        self._call_timeout = call_timeout
        self.broken = False
        self._loop = asyncio.new_event_loop()
        self._ready: threading.Event = threading.Event()
        self._session: Any = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="icyloom-mcp-live")
        self._thread.start()
        self._ready.wait(self._connect_timeout)
        if self._error is not None:
            self.broken = True
            raise self._error

    # -- boucle dédiée ----------------------------------------------------
    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            try:
                self._loop.close()
            except Exception:      # pragma: no cover - fermeture best effort
                pass

    async def _serve(self) -> None:
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(self._params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except BaseException as exc:      # pragma: no cover - échec d'ouverture
            self._error = exc
            self._ready.set()
            try:
                await stack.__aexit__(type(exc), exc, exc.__traceback__)
            except Exception:
                pass
            return
        self._session = session
        self._ready.set()
        # la session reste ouverte jusqu'à close()
        stop = asyncio.Event()
        self._stop = stop
        await stop.wait()
        try:
            await stack.__aexit__(None, None, None)
        except Exception:      # pragma: no cover - fermeture best effort
            pass

    # -- usage ------------------------------------------------------------
    async def call_tool(self, tool: str, args: dict[str, Any]) -> Any:
        """Appelle un outil sur la session persistante (sans bloquer la boucle)."""
        loop = self._loop

        async def _do() -> Any:
            assert self._session is not None
            return await self._session.call_tool(tool, args)

        future = asyncio.run_coroutine_threadsafe(_do(), loop)
        return await asyncio.wait_for(asyncio.wrap_future(future),
                                      timeout=self._call_timeout)

    async def list_tools(self) -> Any:
        loop = self._loop

        async def _do() -> Any:
            assert self._session is not None
            return await self._session.list_tools()

        future = asyncio.run_coroutine_threadsafe(_do(), loop)
        return await asyncio.wait_for(asyncio.wrap_future(future),
                                      timeout=self._call_timeout)

    async def close(self) -> None:
        self.broken = True
        self._session = None
        stop = getattr(self, "_stop", None)
        if stop is not None:
            try:
                self._loop.call_soon_threadsafe(stop.set)
            except RuntimeError:      # pragma: no cover - boucle déjà fermée
                pass
        self._thread.join(timeout=5)


def _parse_ts(value: str) -> float:
    """Convertit un horodatage SQLite en timestamp epoch."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(
            tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return time.time()

