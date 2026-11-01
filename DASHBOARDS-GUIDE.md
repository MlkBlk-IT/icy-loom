# Guide & Quickstart — Dashboards IcyLoom (8153) & Osiris (8770)

Dernière mise à jour : 2026-11-01

---

## 1. Quickstart (Lancement & Accès)

### IcyLoom (`8150–8153`)
- **Dashboard Web (temps réel 2s)** : `http://127.0.0.1:8153/` (ou `/dashboard`)
- **Gateway LLM (OpenAI-compatible)** : `http://127.0.0.1:8150/v1`
- **MCP HTTP (stateful)** : `http://127.0.0.1:8151/mcp`
- **SSE** : `http://127.0.0.1:8152/sse`
- **Base SQLite** : `D:\Icy-Loom\var\icyloom.db`

**Démarrer IcyLoom (hors session agent / terminal dédié) :**
```bash
cd /d/Icy-Loom
.venv/Scripts/python.exe -m icyloom serve
```

### Osiris (`8770`)
- **Dashboard Web (temps réel 2s)** : `http://127.0.0.1:8770/`
- **Assets JS (cache-busted)** : `http://127.0.0.1:8770/assets/app.js?v=202611010212`
- **Base SQLite** : `C:\Users\Tech\.osiris\osiris.db`

**Démarrer le Dashboard Osiris :**
```bash
cd /d/mcp-osiris
.venv/Scripts/python.exe -m dashboard.server --port 8770
```

---

## 2. Comment lire les Dashboards

### Dashboard IcyLoom (`http://127.0.0.1:8153`)
Rafraîchissement automatique toutes les **2 secondes** (`setInterval(poll, 2000)` sur `/api/dashboard`).
- **Gateway & Cache Hits** : affiche le taux de hit exact/normalisé, les tokens économisés vs payés, et la latence par upstream (`hermes` -> `:8080`).
- **Context Store (Tiroir)** : nombre total de chunks (`context_chunks`), sessions actives (`context_sessions`), taux de compression par profil (`strict`, `lossless`).
- **Tool Cache & Ledger** : journal des appels d'outils cachés et économie quotidienne (`/api/ledger/daily`).
- **Fenêtre temporelle** : filtrable via `?hours=24&days=7`.

### Dashboard Osiris (`http://127.0.0.1:8770`)
Rafraîchissement automatique toutes les **2 secondes** (`S.every = 2`, feed à `3s`, intégrité/badges à `5s`).
- **Overview / KPIs** : tâches actives, niveau d'engagement (`bystander`, `light`, `standard`, `full`), budget tokens restant, statut d'intégrité (`SEAL`).
- **Tasks & Manifest** : détail de la tâche active (ex. `t_508935610764`), étapes du plan, contrat scellé SHA256.
- **Refs (`mem://sha256:...`)** : payloads volumineux (>120 tokens) externalisés hors du contexte LLM.
- **Ledger** : tokens déplacés (`moved`), économisés (`saved`), coût net d'Osiris par tâche.
- **LoopGuard** : détection de boucles d'outils (`fingerprints`, `failures`, `tool_usage`).
- **Invariants & Audit** : vérification de non-perte (`lossless`) et dérive (`drift: none`).

---

## 3. Référence des Endpoints API

### API IcyLoom Admin & Dashboard (`http://127.0.0.1:8153`)
| Méthode | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Santé du serveur admin |
| `GET` | `/api/dashboard?hours=24&days=7` | Payload complet consolidé pour l'UI temps réel |
| `GET` | `/api/status` | Statut global IcyLoom |
| `GET` | `/api/gateway` | Statut du proxy Gateway (`:8150`) |
| `GET` | `/api/context/stats` | Statistiques du tiroir de contexte (`context_chunks`, tokens) |
| `GET` | `/api/sessions` | Liste des sessions de contexte (`context_sessions`) |
| `POST` | `/api/sessions/purge` | Purge des sessions expirées |
| `GET` | `/api/cache/stats` | Statistiques du cache de réponses LLM |
| `POST` | `/api/cache-clear` | Vider le cache LLM |
| `GET` | `/api/tools` | Statistiques du cache d'outils MCP |
| `POST` | `/api/tools/clear` | Vider le cache d'outils |
| `GET` | `/api/ledger` | Journal comptable des tokens |
| `GET` | `/api/ledger/daily` | Agrégation quotidienne des gains |
| `GET` | `/api/metrics` | Métriques détaillées |

*(Note : pas d'endpoint HTTP `ctx_store` sur `:8153` — l'écriture se fait via MCP `mcp__icyloom__ctx_store` ou en SQLite direct via le hook `~/.hermes/agent-hooks/icyloom_auto_store.py`).*

### API IcyLoom Gateway (`http://127.0.0.1:8150`)
| Méthode | Endpoint | Description |
|---|---|---|
| `POST` | `/v1/chat/completions` | Proxy OpenAI chat completions avec cache |
| `POST` | `/v1/messages` | Proxy Anthropic messages |
| `POST` | `/v1/embeddings` | Proxy embeddings |
| `GET` | `/v1/models` | Liste des modèles disponibles |
| `GET` | `/api/agent-events` | Flux d'événements agents |

### API Osiris Dashboard (`http://127.0.0.1:8770`)
| Méthode | Endpoint | Description |
|---|---|---|
| `GET` | `/api/health` | État du serveur, de la base SQLite et des compteurs |
| `GET` | `/api/overview` | KPIs globaux (tâches, tokens, intégrité) |
| `GET` | `/api/tasks` | Liste de toutes les tâches |
| `GET` | `/api/task/<task_id>` | État complet d'une tâche + manifeste scellé |
| `GET` | `/api/refs` | Store content-addressed (`mem://sha256:...`) |
| `GET` | `/api/memory` | Mémoire typée (`scope`, `kind`) |
| `GET` | `/api/ledger` | Économie de tokens (entrées, sorties, gains par tâche) |
| `GET` | `/api/loopguard` | Empreintes d'appels + échecs (anti-boucle) |
| `GET` | `/api/events` | Journal d'événements append-only |
| `GET` | `/api/handoffs` | Contrats de délégation |
| `GET` | `/api/invariants` | Invariants anti-casse + verdicts |
| `GET` | `/api/audit?task=<id>` | Preuve d'audit et de non-perte |
| `GET` | `/api/tools` | Les outils MCP Osiris + empreinte des schémas |
| `GET` | `/api/bench` | Benchmark isolé baseline vs Osiris |
| `GET` | `/api/diagnostics` | Bugs détectés + correctifs appliqués |
| `GET` | `/api/config` | Configuration effective d'Osiris |

---

## 4. Commandes de Diagnostic Rapide (CLI)

```bash
# Vérifier le temps réel Osiris (doit afficher v=202611010212 et 1)
curl -s http://127.0.0.1:8770/ | grep -o "app.js?v=[0-9]*"
curl -s http://127.0.0.1:8770/assets/app.js | grep -c "every: 2"

# Vérifier la santé des 2 dashboards
curl -s http://127.0.0.1:8153/api/context/stats
curl -s http://127.0.0.1:8770/api/overview
```
