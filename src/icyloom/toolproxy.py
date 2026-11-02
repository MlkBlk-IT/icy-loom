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
from mcp.types import ListRootsResult, Root

import yaml

from . import paths
from .audit import AuditTrail
from .config import Config, ToolServerConfig
from .db import Database
from .ledger import Ledger, LedgerEntry
from .policy import PolicyEngine
from .registry import ToolRegistry
from .tokenizer import count_tokens, counter_name
from .tokens import TokenMeter
from .toolcompress import compress_result

log = logging.getLogger("icyloom.toolproxy")

#: Backoff du coupe-circuit : un serveur mort n'est pas relancé (et journalisé)
#: à chaque appel. 30 s, 60 s, 120 s… plafonné à 10 min.
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 600


def _stamp() -> str:
    """Horodatage compact pour nommer une sauvegarde de configuration."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _root_uri(path: str) -> str:
    """Chemin Windows -> URI de racine MCP (``D:\\`` -> ``file:///D:/``)."""
    raw = str(path or "").strip()
    if not raw:
        return ""
    if raw.startswith("file://"):
        return raw
    text = raw.replace("\\", "/")
    if not text.endswith("/"):
        text += "/"
    if re.match(r"^[A-Za-z]:/$", text):
        return "file:///" + text
    return "file://" + text if text.startswith("/") else "file:///" + text

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


def _std_env(cfg: ToolServerConfig) -> dict[str, str] | None:
    """Environnement du serveur descendant.

    ``None`` = héritage par défaut du SDK MCP (PATH, APPDATA, LOCALAPPDATA,
    USERPROFILE, TEMP...). Sans lui, un serveur lancé par ``uvx`` ou ``npx``
    meurt au démarrage : « Connection closed ». Les variables déclarées dans
    le YAML écrasent l'héritage, elles ne le remplacent pas.
    """
    if not cfg.env:
        return None
    from mcp.client.stdio import get_default_environment

    return {**get_default_environment(),
            **{k: str(v) for k, v in cfg.env.items()}}


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
        #: registre centralisé des outils (catalogue, index de sélection).
        self.registry = ToolRegistry(self.db)
        #: politique d'exécution : rôle, portée projet, approbations.
        self.policy = PolicyEngine(config.policy)
        #: trace d'audit des invocations.
        self.audit = AuditTrail(self.db)
        #: répartition des tokens par catégorie.
        self.meter = TokenMeter(self.db, self.ledger)
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
        #: racines déclarées par le client (ses dossiers de travail). Les
        #: serveurs descendants les demandent : c'est IcyLoom qui est leur
        #: client, donc c'est IcyLoom qui doit les leur transmettre.
        self._client_roots: list[Any] = []
        #: Coupe-circuit par serveur : un serveur mort ne doit pas être
        #: relancé à chaque appel (c'était 37 échecs par serveur en 3 h).
        self._failures: dict[str, dict[str, Any]] = {}

        # Le catalogue d'outils est rechargé périodiquement (toutes les
        # minutes) : sans cette mémoire, chaque rechargement réécrit la
        # même mesure dans le compteur « tool_schemas » — 243 lignes
        # pour 188 outils, soit un coût de catalogue 3 fois trop grand.
        # Le coût réel est mesuré UNE seule fois par serveur, puis la
        # mesure n'est réécrite que si le catalogue change vraiment.
        self._schema_tokens_seen: dict[str, int] = {}
        for srv in self._servers.values():
            for tool_name, enabled in srv.cache_tools.items():
                if enabled and is_write_tool(tool_name):
                    log.warning("cache_tools: %s/%s ressemble à une écriture", srv.name,
                                tool_name)

    # -- catalogue --------------------------------------------------------
    def server_names(self) -> list[str]:
        return sorted(self._servers)

    def server_configs(self) -> list[dict[str, Any]]:
        """Serveurs actifs, sous forme serialisable (pour l'API/dashboard)."""
        return [
            {
                "name": s.name,
                "command": s.command,
                "args": list(s.args),
                "cache_enabled": s.cache_enabled,
                "ttl_seconds": s.ttl_seconds,
            }
            for s in self._servers.values()
        ]

    def set_server_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        """Active/desactive un serveur du catalogue : ecrit le YAML et recharge.

        Le serveur est cherche d'abord parmi les actifs, puis dans le
        catalogue. L'activation ajoute le serveur a ``tool_proxy.servers``
        (en le retirant du catalogue) ; la desactivation fait l'inverse.
        Une sauvegarde horodatee du YAML est faite avant toute ecriture.
        """
        from .config import load_config  # import local : evite un cycle

        path = paths.config_path()
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError("configuration YAML invalide")
        proxy = raw.setdefault("tool_proxy", {})
        servers = list(proxy.get("servers") or [])
        catalog = list(proxy.get("catalog") or [])
        entry = next((s for s in servers if s.get("name") == name), None)
        origin = "servers"
        if entry is None:
            entry = next((s for s in catalog if s.get("name") == name), None)
            origin = "catalog"
        if entry is None:
            raise KeyError(f"serveur inconnu : {name}")

        backup = path.with_suffix(path.suffix + f".bak-{_stamp()}")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

        servers = [s for s in servers if s.get("name") != name]
        catalog = [s for s in catalog if s.get("name") != name]
        record = dict(entry)
        record["enabled"] = bool(enabled)
        if enabled:
            servers.append(record)
        else:
            catalog.append(record)
        proxy["servers"] = servers
        proxy["catalog"] = catalog
        path.write_text(
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )

        fresh = load_config(path)
        self.config = fresh
        self._servers = {s.name: s for s in fresh.tool_proxy.servers if s.enabled}
        self._catalog.clear()
        self._schemas.clear()
        return {"backup": str(backup), "from": origin, "active": self.server_names()}

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

    def set_client_roots(self, roots: list[Any]) -> None:
        """Mémorise les racines (dossiers de travail) déclarées par le client.

        Un serveur de fichiers MCP demande ses racines au client. Derrière
        IcyLoom, c'est IcyLoom qui est le client : sans transmission, le
        serveur démarre sans aucun dossier autorisé et ses outils ne servent
        à rien.
        """
        self._client_roots = list(roots or [])

    async def _roots_callback(self, *args: Any) -> ListRootsResult:
        """Racines transmises aux serveurs descendants (callback du SDK).

        Le SDK appelle ce callback avec le contexte de requête et attend un
        ``ListRootsResult`` : on l'accepte sans le nommer, pour rester
        compatible d'une version à l'autre.

        Le client (Claude Desktop, MCode...) ne déclare que ses propres
        dossiers de travail. Un serveur de fichiers MCP limite alors son
        accès à ces seuls dossiers : il refuse ``D:\\`` si le client n'a rien
        déclaré. Les racines **accordées par la configuration**
        (``policy.project_roots``) sont donc transmises **en plus** des
        racines du client — sans quoi la policy du gateway autorise une
        opération que le serveur descendant, plus strict, refuse.
        """
        seen: set[str] = set()
        roots: list[Any] = []
        for root in list(self._client_roots) + list(self._granted_roots()):
            uri = str(getattr(root, "uri", "") or "")
            if not uri or uri in seen:
                continue
            seen.add(uri)
            roots.append(root)
        return ListRootsResult(roots=roots)

    def _granted_roots(self) -> list[Any]:
        """Racines accordées par la configuration (``policy.project_roots``)."""
        roots: list[Any] = []
        for raw in list(getattr(self.config.policy, "project_roots", []) or []):
            uri = _root_uri(str(raw))
            if uri:
                roots.append(Root(uri=uri, name=str(raw)))
        return roots

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

    @staticmethod
    def _payload_bytes(payload: Any) -> int:
        """Taille UTF-8 de la charge utile telle qu'un outil la renvoie."""
        return len(json.dumps(payload, ensure_ascii=False, default=str)
                   .encode("utf-8"))

    def cache_set(self, key: str, server: str, tool: str, result: Any,
                  latency_ms: int, canon: str | None = None,
                  ttl: int | None = None) -> int:
        payload = json.dumps(result, ensure_ascii=False, default=str)
        if len(payload.encode("utf-8")) > self.config.tool_proxy.max_result_bytes:
            return 0
        ckey = canon if canon is not None else canon_key(server, tool, {})
        # la TTL portée par la ligne est celle **réellement** appliquée à la
        # relecture (`_ttl(cfg)`), pas le défaut global : la valeur stockée
        # doit décrire l'entrée, sinon toute maintenance fondée dessus ment.
        if ttl is None:
            cfg = self._servers.get(server)
            ttl = self._ttl(cfg) if cfg is not None else self.config.tool_proxy.default_ttl_seconds
        with self.db.transaction() as cur:
            cur.execute(
                "INSERT INTO tool_cache"
                " (key, server, tool, result_json, bytes, latency_ms, ttl, created_at,"
                "  last_accessed, hits, canon_key) VALUES (?,?,?,?,?,?,?,datetime('now'),"
                "  datetime('now'), 0,?)"
                " ON CONFLICT(key) DO UPDATE SET result_json=excluded.result_json,"
                "  bytes=excluded.bytes, latency_ms=excluded.latency_ms,"
                "  ttl=excluded.ttl,"
                "  created_at=datetime('now'), last_accessed=datetime('now'),"
                "  hits=tool_cache.hits + 1, canon_key=excluded.canon_key",
                (key, server, tool, payload, len(payload.encode("utf-8")), latency_ms,
                 int(ttl), ckey),
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
                   latency_ms: int, saved_latency_ms: int = 0, detail: str = "",
                   client: str = "") -> None:
        with self.db.transaction() as cur:
            cur.execute(
                "INSERT INTO tool_events"
                " (kind, server, tool, args_hash, bytes, latency_ms, saved_latency_ms, detail,"
                "  client)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (kind, server, tool, args_hash(args), 0, latency_ms, saved_latency_ms, detail,
                 client or None),
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
        # Coupe-circuit : un serveur mort n'est pas relancé à chaque appel.
        fail = self._failures.get(server)
        now = time.time()
        if fail and now < fail["next_at"]:
            return {"ok": False, "error": fail["error"], "message": fail["message"],
                    "server": server, "backoff_seconds": round(fail["next_at"] - now, 1),
                    "failures": fail["count"]}
        started = time.perf_counter()
        try:
            session = await self._live_session(cfg)
            listed = await session.list_tools()
        except Exception as exc:
            await self._drop_session(server)
            count = (fail["count"] + 1) if fail else 1
            delay = min(BACKOFF_MAX_SECONDS,
                        BACKOFF_BASE_SECONDS * (2 ** (count - 1)))
            self._failures[server] = {"count": count, "next_at": now + delay,
                                      "error": type(exc).__name__, "message": str(exc)}
            # Le journal ne retient que la PREMIERE erreur : un serveur mort
            # ne doit pas remplir le journal a chaque appel.
            if count == 1:
                self._log_event("tool_error", server, "list_tools", {},
                                int((time.perf_counter() - started) * 1000),
                                detail=f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": type(exc).__name__, "message": str(exc),
                    "server": server, "backoff_seconds": delay, "failures": count}
        self._failures.pop(server, None)
        tools = [{"name": t.name, "description": (t.description or "")[:300]}
                 for t in listed.tools]
        self._catalog[server] = (time.time(), tools)
        self._schemas[server] = {
            t.name: (getattr(t, "inputSchema", None) or {}) for t in listed.tools
        }
        self._register(server, tools, self._schemas[server])
        return {"ok": True, "server": server, "count": len(tools), "tools": tools,
                "cached": False}

    def _register(self, server: str, tools: list[dict[str, Any]],
                  schemas: dict[str, Any]) -> None:
        """Alimente le registre centralisé et mesure le coût des schémas."""
        try:
            self.registry.register(
                server, tools, schemas,
                domain=self.config.tool_registry.domains.get(server, server),
                owners=self.config.tool_registry.owners,
                environment=self.config.policy.environment,
            )
        except Exception as exc:      # pragma: no cover - registre indisponible
            log.warning("registre: catalogue de %s non enregistré (%s)", server, exc)
            return
        tokens = self.registry.schema_tokens(server).get(server, 0)
        # Le coût des schemas est mesuré UNE FOIS par serveur : le catalogue
        # est rechargé périodiquement, il ne doit pas être recompté à chaque
        # réindexation (243 lignes de mesure pour 188 outils).
        if self._schema_tokens_seen.get(server) != tokens:
            self._schema_tokens_seen[server] = tokens
            self.meter.record("tool_schemas", tokens, server=server)

    def search_tools(self, query: str, server: str = "") -> dict[str, Any]:
        """Cherche un outil dans le registre (index lexical, score par intention).

        Le registre fournit le classement ; le repli en mémoire couvre les
        serveurs dont le catalogue n'a pas encore été chargé.
        """
        needle = (query or "").strip().lower()
        if not needle:
            return {"ok": False, "error": "empty_query"}
        matches = self.registry.search(
            query, server=server,
            top_k=self.config.tool_registry.search_top_k,
            max_per_server=self.config.tool_registry.max_per_server,
        )
        if matches:
            return {"ok": True, "query": query, "count": len(matches),
                    "matches": matches, "catalogued_servers": self.server_names()}
        matches = []
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

    def get_tool_schema(self, server: str, tool: str,
                        raw: bool = False) -> dict[str, Any]:
        """Contrat d'un outil : schéma compressé (défaut) ou schéma complet.

        Le contrat est ce que l'agent voit : description courte, arguments
        allégés, effets de bord et niveau de sécurité. Le schéma complet
        reste dans le registre et n'est réinjecté qu'au moment de l'appel.
        """
        if raw:
            schema = self._schema_of(server, tool)
            if schema is None:
                return {"ok": False, "error": "unknown_tool", "server": server,
                        "tool": tool, "servers": self.server_names()}
            return {"ok": True, "server": server, "tool": tool, "schema": schema}
        contract = self.registry.contract(
            server, tool,
            max_description_chars=self.config.tool_proxy.schema_max_description_chars)
        if contract is None:
            return {"ok": False, "error": "unknown_tool", "server": server,
                    "tool": tool, "servers": self.server_names()}
        return {"ok": True, "server": server, "tool": tool, "contract": contract}

    def _present(self, server: str, tool: str, args: dict[str, Any],
                 payload: Any) -> tuple[Any, dict[str, Any]]:
        """Comprime le résultat d'un outil ; l'original reste consultable."""
        if not self.config.tool_proxy.compress_results:
            return payload, {}
        text, meta = compress_result(payload, self.config.tool_proxy.max_result_chars)
        if not meta.get("truncated"):
            return payload, meta
        ref = self._store_result(server, tool, args, payload)
        shown = dict(payload) if isinstance(payload, dict) else {}
        shown["content"] = [text]
        shown["truncated"] = True
        shown["compressed"] = meta.get("kind", "raw")
        shown["original_chars"] = meta.get("original_chars", 0)
        shown["result_ref"] = ref
        # Le contenu « structured » dupliqué ne doit pas remonter : il porte
        # souvent le fichier ENTIER, ce qui annule la compression. La clé reste
        # présente (le contrat de l'outil ne bouge pas), son contenu part
        # derrière result_ref.
        for key in ("structured", "structuredContent"):
            if key in shown:
                shown[key] = {"omitted": True, "reason": "compressed",
                              "result_ref": ref}
        saved = max(0, int(meta.get("original_chars", 0)) - int(meta.get("compressed_chars", 0)))
        meta["result_ref"] = ref
        meta["saved_chars"] = saved
        self.meter.record("tool_results", count_tokens(text), server=server, tool=tool,
                          saved_chars=saved)
        return shown, meta

    def _store_result(self, server: str, tool: str, args: dict[str, Any],
                      payload: Any) -> int:
        """Conserve le résultat brut : il reste lisible par son identifiant."""
        try:
            rendered = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            rendered = str(payload)
        with self.db.transaction() as cur:
            cur.execute(
                "INSERT INTO tool_results (server,tool,args_hash,payload,chars)"
                " VALUES (?,?,?,?,?)",
                (server, tool, args_hash(args), rendered, len(rendered)),
            )
            return int(cur.lastrowid or 0)

    async def call(self, server: str, tool: str, args: dict[str, Any] | None = None,
                   *, role: str = "", intent: str = "", confirmed: bool = False,
                   actor: str = "", client: str = "") -> dict[str, Any]:
        """Appelle un outil descendant, via le cache si possible."""
        cfg = self._servers.get(server)
        if cfg is None:
            return {"ok": False, "error": "unknown_server", "server": server,
                    "servers": self.server_names()}
        args = args or {}
        await self._ensure_schema(server, tool)
        row = self.registry.get(server, tool)
        side_effects = str((row or {}).get("side_effects") or "none")
        decision = self.policy.decide(
            role=role, server=server, tool=tool, side_effects=side_effects,
            security_level=str((row or {}).get("security_level") or "low"),
            args=args, confirmed=confirmed, intent=intent,
        )
        if decision.effect != "allow":
            self.audit.record(actor=actor, role=decision.role, intent=intent,
                              server=server, tool=tool, args_hash=args_hash(args),
                              decision=decision.effect, reason=decision.reason,
                              detail=decision.rule)
            denied = decision.effect == "deny"
            return {"ok": False,
                    "error": "policy_denied" if denied else "approval_required",
                    "message": ("Appel refusé par la politique." if denied else
                                "Appel soumis à approbation : rejouer avec confirm=true."),
                    "server": server, "tool": tool,
                    "decision": decision.as_dict()}
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
                                    detail=str(verdict.get("reason", "")), client=client)
                    return {"ok": False, "error": "loop_blocked",
                            "message": verdict.get("reason"), "server": server,
                            "tool": tool, "repeats": verdict.get("repeats")}
                # un hit évite réellement d'encoder le résultat côté modèle :
                # on mesure les tokens que le cache vient d'épargner au lieu
                # de publier un 0 en dur.
                tokens_saved = count_tokens(
                    json.dumps(cached["result"], ensure_ascii=False, default=str))
                self._log_event("tool_hit", server, tool, args, int(cached["latency_ms"]),
                                saved_latency_ms=int(cached["latency_ms"]),
                                detail=matched, client=client)
                self.ledger.record(LedgerEntry(
                    kind="tool_cache_hit", upstream=server, client=client,
                    tool_tokens_saved=tokens_saved,
                    saved_latency_ms=int(cached["latency_ms"]),
                    meta={"tool": tool, "cache_key": key, "matched": matched,
                          "counter": counter_name(), "bytes": int(cached["bytes"])},
                ))
                shown, meta = self._present(server, tool, args, cached["result"])
                answer = {"ok": True, "server": server, "tool": tool,
                          "result": shown, "cached": True}
                if meta.get("truncated"):
                    answer["compressed"] = meta.get("kind")
                    answer["result_ref"] = meta.get("result_ref")
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
                            detail=f"{type(exc).__name__}: {exc}", client=client)
            self.audit.record(actor=actor, role=decision.role, intent=intent,
                              server=server, tool=tool, args_hash=args_hash(args),
                              decision="error", reason=f"{type(exc).__name__}: {exc}",
                              latency_ms=latency)
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
                            detail=str(verdict.get("reason", "")), client=client)
            return {"ok": False, "error": "loop_blocked",
                    "message": verdict.get("reason"), "server": server, "tool": tool,
                    "repeats": verdict.get("repeats")}

        self._log_event("tool_call", server, tool, args, latency, client=client)
        self.ledger.record(LedgerEntry(
            kind="tool_call", upstream=server, client=client, latency_ms=latency,
            meta={"tool": tool},
        ))
        shown, meta = self._present(server, tool, args, payload)
        response = {"ok": True, "server": server, "tool": tool, "result": shown,
                    "cached": False, "latency_ms": latency}
        if meta.get("truncated"):
            response["compressed"] = meta.get("kind")
            response["result_ref"] = meta.get("result_ref")
        if verdict.get("warning"):
            response["warning"] = verdict["warning"]
        failed = isinstance(payload, dict) and bool(payload.get("is_error"))
        if cacheable and not failed:
            self.cache_set(key, server, tool, payload, latency, ttl=ttl,
                           canon=canon_key(server, tool, args, epoch,
                                           self._schema_of(server, tool)))
        self.registry.record_call(server, tool, latency)
        self.audit.record(actor=actor, role=decision.role, intent=intent,
                          server=server, tool=tool, args_hash=args_hash(args),
                          decision="allow", reason=decision.reason,
                          compacted=str(meta.get("kind", "")),
                          tokens_in=count_tokens(json.dumps(shown, ensure_ascii=False,
                                                           default=str)),
                          tokens_saved=int(meta.get("saved_tokens", 0)),
                          saved_chars=int(meta.get("saved_chars", 0)),
                          latency_ms=latency)
        return response

    async def call_batch(self, calls: Iterable[dict[str, Any]],
                         stop_on_error: bool = True, *, role: str = "",
                         intent: str = "", confirmed: bool = False,
                         actor: str = "") -> list[dict[str, Any]]:
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
                    outcome = await self.call(server, tool, args, role=role,
                                             intent=intent, confirmed=confirmed,
                                             actor=actor)
                    outcome["index"] = index
            results.append(outcome)
            if failed_at is None and _is_failure(outcome):
                failed_at = index
        return results

    async def run_script(self, source: str, *, role: str = "", intent: str = "",
                         confirmed: bool = False,
                         actor: str = "") -> dict[str, Any]:
        """Exécute un script d'outils dans le bac à sable.

        Règle du bac à sable : **lecture seule par défaut**. Sans
        ``confirmed``, le script ne peut appeler que des outils sans effet de
        bord ; un outil qui écrit est refusé, et un appel qui échoue est
        remonté tel quel au script.
        """
        from .script import run_script as _run

        async def _call(server: str, tool: str,
                        args: dict[str, Any]) -> dict[str, Any]:
            row = self.registry.get(server, tool)
            effects = str((row or {}).get("side_effects") or "none")
            if not confirmed and effects != "none":
                return {"ok": False, "error": "read_only_script",
                        "message": "Le bac à sable n'autorise que les outils "
                                   "sans effet de bord.",
                        "server": server, "tool": tool}
            return await self.call(server, tool, args, role=role, intent=intent,
                                   confirmed=confirmed, actor=actor)

        outcome = await _run(
            source, _call,
            timeout=self.config.tool_proxy.script_timeout_seconds,
            max_steps=self.config.tool_proxy.script_max_steps,
            max_instructions=self.config.tool_proxy.script_max_instructions,
        )
        self.audit.record(actor=actor, role=role, intent=intent, server="*",
                          tool="run_tool_script",
                          args_hash=args_hash({"script": source}),
                          decision="allow" if outcome.get("ok") else "error",
                          reason=str(outcome.get("error", "")),
                          detail=str(outcome.get("steps", 0)))
        return outcome

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
            env=_std_env(cfg),
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
            env=_std_env(cfg),
        )
        live = _LiveSession(params, self.config.tool_proxy.connect_timeout_seconds,
                            self.config.tool_proxy.call_timeout_seconds,
                            list_roots_callback=self._roots_callback)
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
            "registry": self.registry.stats(),
            "policy": self.policy.describe(),
            "audit": self.audit.stats(),
            "tokens": self.meter.summary(),
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
                 call_timeout: float, list_roots_callback: Any = None) -> None:
        self._params = params
        self._connect_timeout = connect_timeout
        self._call_timeout = call_timeout
        self._list_roots = list_roots_callback
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
            session = await stack.enter_async_context(
                ClientSession(read, write, list_roots_callback=self._list_roots))
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

