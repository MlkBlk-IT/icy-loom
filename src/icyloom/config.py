"""Configuration unique d'IcyLoom (YAML + validation Pydantic).

Un seul fichier, une seule hiérarchie. Rien n'est lu ailleurs : pas de JSON
de configuration, pas de valeurs par défaut « dans le code » qui ne soient pas
ici. Les seuls fichiers externes sont les **données** externalisées
(`data/normalization_rules.json`, `data/tools_registry.json`), qui sont des
ressources, pas de la configuration.

Les chemins stockés sont **relatifs** : ils sont résolus contre la racine du
projet au chargement (voir `paths.py`).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from . import paths

Profile = Literal["strict", "balanced", "aggressive"]
UpstreamKind = Literal["openai", "anthropic", "gemini"]


class ServerConfig(BaseModel):
    """Serveur MCP unifié."""

    name: str = "icyloom"
    host: str = "127.0.0.1"
    instructions: str = (
        "IcyLoom réunit un cache LLM local et un tiroir de contexte compressé. "
        "Les actions destructrices exigent confirm=true."
    )
    #: Jeton optionnel pour autoriser un accès distant (vide = local uniquement).
    remote_access_token: str | None = None

    @field_validator("host")
    @classmethod
    def _host_is_loopback_or_explicit(cls, v: str) -> str:
        return v.strip() or "127.0.0.1"

    @model_validator(mode="after")
    def _remote_host_requires_token(self) -> "ServerConfig":
        host = self.host.lower().strip("[]")
        loopback = host in {"localhost", "127.0.0.1", "::1"}
        if not loopback and not self.remote_access_token:
            raise ValueError("remote_access_token est obligatoire si server.host n'est pas local")
        return self


class PortsConfig(BaseModel):
    """Ports de service. 8150-8153 sont réservés à IcyLoom, 8154-8159 aux tests."""

    gateway_http: int = 8150
    mcp_http: int = 8151
    mcp_sse: int = 8152
    admin: int = 8153


class DatabaseConfig(BaseModel):
    """Base SQLite unique (WAL + busy_timeout)."""

    path: str = "icyloom.db"
    journal_mode: Literal["WAL", "DELETE", "TRUNCATE", "MEMORY", "OFFER"] = "WAL"
    busy_timeout_ms: int = 5000
    synchronous: Literal["OFF", "NORMAL", "FULL", "EXTRA"] = "NORMAL"


class CacheConfig(BaseModel):
    """Cache LLM : TTL, plafond d'entrées, cache sémantique optionnel."""

    ttl_seconds: int = 604800
    max_entries: int = 50000
    semantic_enabled: bool = False
    semantic_threshold: float = 0.95
    semantic_provider: str = ""
    semantic_url: str = ""
    semantic_collection: str = "icyloom"


class CompressionConfig(BaseModel):
    """Compression du contexte. Désactivée par défaut, jamais lossy par défaut."""

    enabled: bool = False
    profile: Profile = "strict"
    auto_store_threshold: int = 500
    max_anchor_chars: int = 6000
    max_invariants: int = 40


class ContextConfig(BaseModel):
    """Tiroir de contexte : sessions, rédaction, sandbox, limitation de débit."""

    session_ttl_days: int = 30
    redact_enabled: bool = True
    rate_limit_per_minute: int = 120
    allowed_dirs: list[str] = Field(default_factory=list)
    audit_enabled: bool = True
    audit_retention_days: int = 30
    sandbox_enabled: bool = True
    sandbox_timeout_seconds: float = 30.0


class UpstreamConfig(BaseModel):
    """Un fournisseur LLM accessible en HTTP."""

    name: str
    base_url: str
    priority: int = 100
    kind: UpstreamKind = "openai"
    api_key_env: str = ""
    #: Valeur de l'en-tête `anthropic-version` (kind=anthropic) si le client n'en envoie pas.
    anthropic_version: str = "2023-06-01"
    timeout_seconds: float = 120.0
    strip_v1: bool = False
    enabled: bool = True
    sticky: bool = False

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.strip().rstrip("/")


class ToolServerConfig(BaseModel):
    """Un serveur MCP descendant, appelable via le relais d'outils."""

    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cache_enabled: bool = False
    ttl_seconds: int = 3600
    #: Allowlist stricte d'outils cachables ; vide = rien n'est mis en cache.
    cache_tools: dict[str, bool] = Field(default_factory=dict)
    enabled: bool = True
    #: Description lisible (catalogue) : sert au choix offert dans le dashboard.
    description: str = ""


class ToolProxyConfig(BaseModel):
    """Relais d'outils MCP : cache, TTL par serveur, garde anti-boucle."""

    servers: list[ToolServerConfig] = Field(default_factory=list)
    #: Catalogue de serveurs disponibles mais inactifs : le dashboard propose ce choix.
    catalog: list[ToolServerConfig] = Field(default_factory=list)
    default_ttl_seconds: int = 3600
    loop_window_seconds: int = 300
    loop_warn_after: int = 3
    loop_block_after: int = 5
    loop_max_cycle: int = 3
    max_result_bytes: int = 1048576
    connect_timeout_seconds: float = 20.0
    call_timeout_seconds: float = 300.0


class BreakerConfig(BaseModel):
    """Circuit breaker : seuil d'échecs et délai de réarmement réel."""

    failure_threshold: int = 3
    reset_seconds: float = 30.0
    half_open_probes: int = 1


class LedgerConfig(BaseModel):
    """Journal économique : ce qui coûte, ce qui économise."""

    enabled: bool = True
    retention_days: int = 90


class Config(BaseModel):
    """Racine de la configuration unique."""

    server: ServerConfig = Field(default_factory=ServerConfig)
    ports: PortsConfig = Field(default_factory=PortsConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    compression: CompressionConfig = Field(default_factory=CompressionConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)
    ledger: LedgerConfig = Field(default_factory=LedgerConfig)
    tool_proxy: ToolProxyConfig = Field(default_factory=ToolProxyConfig)
    upstreams: list[UpstreamConfig] = Field(default_factory=list)

    # -- accès rapide aux upstreams ------------------------------------
    def upstream(self, name: str) -> UpstreamConfig | None:
        for u in self.upstreams:
            if u.name == name:
                return u
        return None

    def default_upstream(self) -> UpstreamConfig | None:
        """Upstream par défaut : le plus prioritaire, sinon le premier actif."""
        active = [u for u in self.upstreams if u.enabled]
        if not active:
            return None
        return sorted(active, key=lambda u: (u.priority, u.name))[0]

    def db_path(self) -> Path:
        """Chemin résolu du fichier SQLite unique."""
        return paths.db_file(self.database.path)

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Charge et valide le YAML de configuration."""
    target = Path(path) if path else paths.config_path()
    if not target.is_file():
        raise FileNotFoundError(
            f"configuration introuvable : {target} "
            "(attendu à la racine du projet ou via $ICYLOOM_CONFIG)"
        )
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"configuration invalide (racine YAML non-mapping) : {target}")
    return Config.model_validate(raw)


def write_default_config(target: Path, force: bool = False) -> Path:
    """Écrit une configuration par défaut (utilisé par `icyloom init`)."""
    target = Path(target)
    if target.exists() and not force:
        raise FileExistsError(f"déjà présent : {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(
            Config().as_dict(), sort_keys=False, allow_unicode=True, default_flow_style=False
        ),
        encoding="utf-8",
    )
    return target
