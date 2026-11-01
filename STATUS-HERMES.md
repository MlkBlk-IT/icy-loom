# STATUS — Intégration Hermes × IcyLoom

Dernière mise à jour : 2026-11-01

## État serveur

| Composant | Port | Statut |
|---|---|---|
| Gateway LLM (OpenAI-compatible) | 8150 | OK |
| MCP HTTP | 8151 | OK (stateful, session ID requis) |
| SSE | 8152 | OK (pas de compression) |
| Dashboard / Admin | 8153 | OK |
| Base SQLite | `var/icyloom.db` | OK |

Lancement :
```bash
cd /d/Icy-Loom
.venv/Scripts/python.exe -m icyloom serve
```

> Le serveur meurt si le process parent est coupé → le lancer depuis un terminal ou une tâche planifiée, pas depuis une session agent.

## Configuration (icyloom.yaml)

- `compression.enabled: true`
- `allowed_dirs`: `D:\Icy-Loom`, cache scratch Hermes, `~/.hermes`
- `upstreams.hermes` → `http://127.0.0.1:8080/v1` (IcyCache, priority 10, clé HERMES_CUSTOM_OMNIROUTE_API_KEY)

Backup : `icyloom.yaml.bak_20261101_013707`

## Hook auto-store Hermes

- Fichier : `~/.hermes/agent-hooks/icyloom_auto_store.py`
- Événement : `post_tool_call` (matcher `.*`, timeout 30)
- Seuil : 500 tokens (`ICYLOOM_HOOK_THRESHOLD`)
- Écriture directe SQLite (`context_chunks` + `context_sessions`) — fail-open (exit 0)
- Log : `~/.hermes/agent-hooks/icyloom_hook.log`
- Déclaré dans `config.yaml` (hooks.post_tool_call, hooks_auto_accept: true)

## Gain mesuré

Appels réels via gateway : **86.37% de tokens économisés** (16177 sauvés / 2553 payés).

## Pièges connus

- `pydantic_core` cassé dans le venv → `ContextStore` impossible → écriture SQLite directe
- MCP stdio conflictuel (déjà 2 process lancés par Hermes)
- MCP HTTP 8151 exige un session ID (stateful)

## Documentation Dashboards

- Guide & Quickstart : `D:\Icy-Loom\DASHBOARDS-GUIDE.md` (et `D:\mcp-osiris\DASHBOARDS-GUIDE.md`)
- Dashboard IcyLoom : `http://127.0.0.1:8153/` (auto-refresh 2s)
- Dashboard Osiris : `http://127.0.0.1:8770/` (auto-refresh 2s, `app.js?v=202611010212`)
