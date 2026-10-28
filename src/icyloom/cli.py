"""CLI IcyLoom.

    icyloom doctor            état de l'installation (chemins, versions, ports, schéma)
    icyloom paths             chemins résolus
    icyloom version           version
    icyloom config get <clé>  valeur de configuration
    icyloom init              écrit une configuration par défaut
    icyloom gateway           passerelle HTTP (OpenAI/Anthropic) sur 8150
    icyloom mcp --transport … serveur MCP (stdio | http | sse)
    icyloom admin             API d'administration sur 8153
    icyloom serve             gateway + MCP + admin
"""

from __future__ import annotations

import argparse
import json
import platform
import socket
import sys
from pathlib import Path
from typing import Any

from . import __version__, paths
from .config import Config, load_config, write_default_config
from .logsetup import configure


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) != 0


def _cmd_version(args: argparse.Namespace) -> int:
    print(f"icyloom {__version__} (python {platform.python_version()})")
    return 0


def _cmd_paths(args: argparse.Namespace) -> int:
    info = {
        "project_root": str(paths.project_root()),
        "config_file": str(paths.config_path()),
        "data_dir": str(paths.data_dir()),
        "runtime_dir": str(paths.runtime_dir()),
        "normalization_rules": str(paths.data_file("normalization_rules.json")),
        "tools_registry": str(paths.data_file("tools_registry.json")),
    }
    try:
        info["database"] = str(load_config(args.config).db_path())
    except Exception as exc:
        info["database"] = f"<non résolu: {exc}>"
    if args.json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
    else:
        for key, value in info.items():
            print(f"{key:20} {value}")
    return 0


def _cmd_config_get(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    node: Any = cfg.as_dict()
    for part in args.key.split("."):
        if not isinstance(node, dict) or part not in node:
            print(f"clé inconnue : {args.key}", file=sys.stderr)
            return 2
        node = node[part]
    print(json.dumps(node, indent=2, ensure_ascii=False) if isinstance(node, (dict, list))
          else str(node))
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path) if args.path else paths.project_root() / paths.CONFIG_FILENAME
    try:
        write_default_config(target, force=args.force)
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"configuration écrite : {target}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    problems: list[str] = []
    print("== IcyLoom doctor ==")
    print(f"version               : {__version__}")
    print(f"python                : {platform.python_version()} ({sys.executable})")

    cfg_path = paths.config_path()
    print(f"racine projet         : {paths.project_root()}")
    print(f"configuration         : {cfg_path} existe={cfg_path.is_file()}")
    cfg: Config | None = None
    if not cfg_path.is_file():
        problems.append("fichier de configuration absent")
    else:
        try:
            cfg = load_config(args.config)
            print("configuration         : VALIDE")
        except Exception as exc:
            problems.append(f"configuration invalide : {exc}")
            print(f"configuration         : INVALIDE ({exc})")

    for name in paths.DATA_FILENAMES:
        target = paths.data_file(name)
        ok = target.is_file()
        size = target.stat().st_size if ok else 0
        print(f"données {name:<26}: {'présent' if ok else 'ABSENT'} ({size} octets)")
        if not ok:
            problems.append(f"fichier de données absent : {name}")

    # Registre d'outils : l'assertion d'intégrité (100 ancres) fait partie
    # du contrôle de démarrage.
    registry = paths.data_file("tools_registry.json")
    if registry.is_file():
        try:
            import json

            data = json.loads(registry.read_text(encoding="utf-8"))
            declared = int(data.get("total", 0))
            groups = data.get("groups") or {}
            if isinstance(groups, dict):
                counted = sum(int(v) for v in groups.values() if isinstance(v, int))
            else:
                counted = sum(int(g.get("total", 0)) for g in groups
                              if isinstance(g, dict))
            print(f"registre d'outils          : total déclaré={declared} "
                  f"compte={counted}")
            if declared != 100 or counted != declared:
                problems.append(
                    f"registre d'outils : total={declared} compte={counted} (attendu 100)")
        except Exception as exc:
            problems.append(f"registre d'outils illisible : {exc}")

    if cfg is not None:
        db = cfg.db_path()
        print(f"base sqlite           : {db} existe={db.is_file()}")
        if db.is_file():
            try:
                from .db import Database

                with Database(cfg) as database:
                    tables = database.table_names()
                print(f"schéma                : {len(tables)} tables ({', '.join(tables[:8])}…)")
            except Exception as exc:
                problems.append(f"base illisible : {exc}")
                print(f"schéma                : ERREUR ({exc})")
        print(f"compression           : enabled={cfg.compression.enabled} "
              f"profil={cfg.compression.profile}")
        print(f"cache sémantique       : {'actif' if cfg.cache.semantic_enabled else 'inactif (opt-in)'}")
        print(f"upstreams configurés  : {len(cfg.upstreams)}")
        print(f"serveurs d'outils      : {len(cfg.tool_proxy.servers)}")
        for label, port in (("gateway", cfg.ports.gateway_http), ("mcp-http", cfg.ports.mcp_http),
                            ("mcp-sse", cfg.ports.mcp_sse), ("admin", cfg.ports.admin)):
            free = _port_free(cfg.server.host, port)
            print(f"port {label:<17}: {port} {'libre' if free else 'OCCUPÉ'}")
            if not free:
                problems.append(f"port occupé : {port} ({label})")

    print()
    if problems:
        print("PROBLÈMES :")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("OK — aucune anomalie détectée.")
    return 0


def _cmd_gateway(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    configure("INFO", paths.log_file("gateway.log"))
    from .gateway import run_gateway

    host = args.host or cfg.server.host
    port = args.port or cfg.ports.gateway_http
    run_gateway(cfg, host=host, port=port)
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    configure("INFO", paths.log_file("mcp.log"))
    from .mcpserver import run_mcp

    run_mcp(
        cfg,
        transport=args.transport,
        host=args.host or cfg.server.host,
        port=args.port,
    )
    return 0


def _cmd_admin(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    configure("INFO", paths.log_file("admin.log"))
    from .admin import run_admin

    run_admin(cfg, host=args.host or cfg.server.host, port=args.port or cfg.ports.admin)
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    """Console web : affiche l'URL (et les métriques courantes) sans rien lancer."""
    cfg = load_config(args.config)
    url = f"http://{cfg.server.host}:{cfg.ports.admin}/dashboard"
    if args.serve:
        configure("INFO", paths.log_file("admin.log"))
        from .admin import run_admin

        print(f"Console IcyLoom : {url}")
        run_admin(cfg, host=cfg.server.host, port=cfg.ports.admin)
        return 0

    from .dashboard import snapshot

    data = snapshot(cfg)
    k = data["kpi"]
    print(f"Console IcyLoom : {url}")
    print(f"  (lancer 'icyloom dashboard --serve' ou 'icyloom serve' pour l'ouvrir)")
    print()
    print(f"appels LLM      : {k['calls']}  (cache {k['hit_rate_pct']} %)")
    print(f"tokens payés    : {k['tokens_paid']}")
    print(f"tokens économisés: {k['tokens_saved']}  (gain {k['gain_pct']} %)")
    print(f"seuil {k['threshold_pct']} %       : {'atteint' if k['threshold_met'] else 'NON ATTEINT'}")
    print(f"latence p95     : {k['latency_p95']} ms")
    print(f"entrées cache   : {k['cache_entries']}")
    print(f"base            : {data['db']['path']} ({data['db']['size_bytes']} octets)")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    configure("INFO", paths.log_file("serve.log"))
    from .runner import run_all

    run_all(cfg)
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    configure("INFO", paths.log_file("migrate.log"))
    from .migrate import run_migration

    ok = run_migration(cfg, source=Path(args.source) if args.source else None, dry_run=args.dry_run)
    return 0 if ok else 1


def _cmd_client(args: argparse.Namespace) -> int:
    """Configuration MCP pour un client : à copier ou à installer."""
    from .clientcheck import client_config, install_client_config

    if args.target == "json" or not args.install:
        block = client_config(args.config, args.transport)
        print(json.dumps(block, indent=2, ensure_ascii=False))
        if not args.install and args.target != "json":
            print(f"\n# pour installer automatiquement :\n"
                  f"# icyloom client --target {args.target} --install\n")
        return 0

    path = install_client_config(args.target, args.config, args.transport)
    print(f"configuration écrite : {path}")
    print("redémarre ton client MCP pour qu'il prenne IcyLoom en compte.")
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    """Auto-test : un vrai client se connecte et appelle les outils."""
    from .clientcheck import TRANSPORTS, run_checks

    transports = TRANSPORTS if args.transport == "all" else (args.transport,)
    print("== IcyLoom : test comme un vrai client MCP ==")
    ok, checks = run_checks(args.config, transports)
    failed = [label for flag, label in checks if not flag]
    for flag, label in checks:
        print(f"  {'✔' if flag else '✘'} {label}")
    print(f"\n{len(checks) - len(failed)}/{len(checks)} vérifications réussies")
    if failed:
        print("Si un transport HTTP/SSE échoue, la pile n'est probablement pas "
              "démarrée (icyloom serve) ; le mode stdio, lui, est autonome.")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="icyloom", description="IcyLoom — cache LLM + tiroir de contexte")
    parser.add_argument("--config", default=None, help="chemin du YAML de configuration")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("doctor", help="diagnostic de l'installation")
    p.set_defaults(func=_cmd_doctor)

    p = sub.add_parser("paths", help="chemins résolus")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_paths)

    p = sub.add_parser("version", help="version")
    p.set_defaults(func=_cmd_version)

    p = sub.add_parser("config", help="lecture de configuration")
    csub = p.add_subparsers(dest="config_command", required=True)
    cg = csub.add_parser("get", help="valeur d'une clé (ex: ports.gateway_http)")
    cg.add_argument("key")
    cg.set_defaults(func=_cmd_config_get)

    p = sub.add_parser("init", help="écrit une configuration par défaut")
    p.add_argument("--path", default=None)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("gateway", help="passerelle HTTP OpenAI/Anthropic")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.set_defaults(func=_cmd_gateway)

    p = sub.add_parser("mcp", help="serveur MCP unifié")
    p.add_argument("--transport", choices=["stdio", "http", "sse"], default="stdio")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.set_defaults(func=_cmd_mcp)

    p = sub.add_parser("admin", help="API d'administration")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.set_defaults(func=_cmd_admin)

    p = sub.add_parser("serve", help="gateway + MCP + admin")
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser("dashboard", help="console web des métriques")
    p.add_argument("--serve", action="store_true", help="lance l'API admin qui sert la console")
    p.set_defaults(func=_cmd_dashboard)

    p = sub.add_parser("migrate", help="migration additive vers la base unifiée")
    p.add_argument("--source", default=None, help="dossier des bases d'origine (lecture seule)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_cmd_migrate)

    p = sub.add_parser("client", help="configuration MCP pour un client (copier / installer)")
    p.add_argument("--target", default="cursor",
                   choices=["cursor", "claude", "claude-code", "windsurf", "codex", "json"],
                   help="client visé (json = bloc brut à copier)")
    p.add_argument("--transport", default="stdio", choices=["stdio", "http", "sse"])
    p.add_argument("--install", action="store_true",
                   help="écrit la configuration dans le client choisi")
    p.set_defaults(func=_cmd_client)

    p = sub.add_parser("check", help="auto-test : un vrai client se connecte et appelle les outils")
    p.add_argument("--transport", default="all", choices=["all", "stdio", "http", "sse"])
    p.set_defaults(func=_cmd_check)
    return parser


def main(argv: list[str] | None = None) -> int:
    # stdout est réservé au JSON-RPC en transport stdio : on force l'UTF-8
    # avant toute impression pour éviter le crash cp1252 sur les accents.
    from .logsetup import force_utf8

    force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
