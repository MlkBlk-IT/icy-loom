# PROMPT POUR CLAUDE AI — PLAN DE MERGE ICYCACHE + LOOM → ICYLOOM

## CONTEXTE

Tu es un architecte logiciel senior. Je dois fusionner DEUX projets Python existants et fonctionnels en UN SEUL serveur MCP unifié appelé **IcyLoom**. J'ai déjà un squelette de départ. Je veux un PLAN DE MERGE intelligent et complet — pas de code, juste le plan.

## OBJECTIF

Fusionner IcyCache (cache LLM HTTP) + Loom MCP (compression de contexte sémantique) en un seul produit **IcyLoom** qui:
1. Expose 1 seul serveur MCP (FastMCP, transport streamable-http + stdio)
2. Garde TOUTES les fonctionnalités des deux projets
3. N'ajoute RIEN d'inutile — pas de dashboard pour l'instant (viendra à la fin)
4. Réutilise le code existant au maximum (port + adapt, pas rewrite from scratch)
5. Évite la duplication: SQLite déjà utilisé par les deux → 1 seule DB

## PROJET 1: ICYCACHE (D:/IcyCache/)

### Structure (4671 lignes, 10 fichiers Python)
```
D:/IcyCache/src/
├── server.py          (1133 l.) — HTTP proxy FastAPI: /v1/chat/completions, streaming SSE, cache intercept
├── mcp_server.py      (1306 l.) — MCP tools: get_cache_stats, clear_cache, normalize_preview, search_cached_prompts, proxy_tool, proxy_tools, list_proxy_servers, list_proxy_tools, get_tool_cache_stats, clear_tool_cache, check_tool_loop
├── mcp_gateway.py     (356 l.)  — Gateway MCP facade (port 8083)
├── mcp_stdio_bridge.py(83 l.)   — stdio bridge for Hermes
├── icy_core.py        (375 l.) — upstream routing, cacheable logic, cache key, SSE parsing
├── icy_cache_store.py (217 l.) — SQLite cache store (get/set/stats/clear, TTL, LRU)
├── icy_normalize.py   (185 l.) — request normalization + canonical hashing
├── prefix_proxy.py    (270 l.) — URL prefix routing (/{upstream}/v1/...)
├── tool_proxy.py      (655 l.) — tool proxy: cache tool results, anti-loop guard
├── upstream_utils.py  (91 l.)  — upstream health checks
```

### Config (config/icycache.gateway.json)
- Ports: 8080 (HTTP proxy), 8081 (MCP SSE), 8082 (gateway WS), 8083 (MCP gateway), 8084 (MCP streamable HTTP)
- Upstreams: omniroute (20128), lmstudio (1234), ollama (11434), openai, anthropic
- Cache: SQLite at D:/IcyCache/db/cache.db, max 10000 entries, TTL 86400s
- Loop guard: warn after 3, block after 5, max cycle 3
- Tool cache: enabled, default deny, max 1MB results
- Semantic cache: disabled (threshold 0.95)

### Outils MCP existants (11 tools)
1. `get_cache_stats()` — entries, hits, size, hit rate
2. `clear_cache(confirm)` — purge cache
3. `normalize_preview(text)` — show normalized form + hash
4. `search_cached_prompts(query, limit)` — search cached prompts by text
5. `proxy_tool(server, tool, args, client)` — proxy single MCP tool call with cache
6. `proxy_tools(calls, client)` — batch proxy MCP tool calls
7. `list_proxy_servers()` — list configured proxy servers
8. `list_proxy_tools(server)` — list tools for a proxied server
9. `get_tool_cache_stats()` — tool cache statistics
10. `clear_tool_cache(confirm)` — purge tool cache
11. `check_tool_loop(messages, warn_after, max_cycle)` — detect tool call loops

### Fonctionnalités uniques IcyCache
- **HTTP proxy transparent** — compatible OpenAI/Anthropic SDK (drop-in replacement)
- **Streaming SSE** — cache works with streaming responses (assemble + replay)
- **Multi-upstream routing** — par préfixe URL, header X-IcyCache-Upstream, body upstream, ou default config
- **Normalization** — canonical form avant hashing (ordre des keys, whitespace, etc.)
- **Loop guard** — détecte cycles d'outils (warn/block)
- **Tool proxy** — proxy + cache les résultats d'outils MCP externes
- **Circuit breaker** — 3 failures → unhealthy, reset après 30s

## PROJET 2: LOOM MCP (D:/Loom/)

### Structure (3941 lignes, 16 fichiers Python)
```
D:/Loom/src/loom/
├── server.py          (685 l.) — FastMCP server, 17 tools
├── store.py           (648 l.) — SQLite context store (chunks, sessions, metadata)
├── compress.py        (316 l.) — compression: build_anchor, expand_anchor, lossless
├── anchors.py         (411 l.) — invariant extraction (IPs, ports, model IDs, paths, URLs)
├── verify.py          (86 l.)  — lossless verification (all invariants present?)
├── vector_index.py   (287 l.) — FAISS vector index (semantic search)
├── session.py         (293 l.) — session management (create, expire, list)
├── cross_session.py   (216 l.) — transfer chunks between sessions
├── auto_store.py      (129 l.) — decorator: auto-store large outputs, return anchor
├── profile.py         (76 l.)  — compression profiles (strict/balanced/aggressive)
├── audit.py           (111 l.) — audit logging for tool calls
├── crypto.py          (265 l.) — encryption for sensitive chunks
�--|-- redact.py         (50 l.)  — redact secrets before storing
├── sandbox.py         (245 l.) — sandboxed code execution for verify
├── tokenizer.py       (33 l.)  — tiktoken wrapper
├── ratelimit.py       (85 l.)  — rate limiting per session
```

### Outils MCP existants (17 tools)
1. `ctx_store(content, session_id, label)` — store + chunk + embed
2. `ctx_get(chunk_id)` — retrieve full chunk (lossless)
3. `ctx_check(session_id)` — session health check
4. `ctx_stats(session_id)` — session statistics
5. `ctx_profile(mode)` — compression profile info
6. `ctx_list_sessions(include_expired)` — list all sessions
7. `ctx_purge_session(session_id)` — delete a session
8. `ctx_purge_expired()` — cleanup expired chunks
9. `ctx_vacuum()` — optimize SQLite + rebuild indexes
10. `ctx_health()` — global health check
11. `ctx_store_file(path, session_id, label)` — store file contents
12. `ctx_store_cmd(cmd, session_id, label)` — store command output
13. `ctx_search(query, limit, session_id, semantic)` — hybrid search (BM25 + FAISS)
14. `ctx_verify(session_id)` — verify lossless compression
15. `ctx_transfer(from_session, to_session, chunk_ids)` — cross-session transfer
16. `ctx_rebuild_index()` — rebuild FAISS vector index
17. `ctx_vector_stats()` — vector index statistics

### Fonctionnalités uniques Loom
- **Anchor-based compression** — large content → small anchor (chunk IDs + invariants)
- **Invariant extraction** — IPs, ports, model IDs, paths, URLs préservés à travers compression
- **Lossless verification** — vérifie que tous les invariants sont présents après décompression
- **FAISS vector index** — recherche sémantique locale (sentence-transformers)
- **Hybrid search** — BM25 (keyword) + FAISS (semantic), fusion des résultats
- **Auto-store decorator** — `@auto_store(threshold_tokens=500)` sur n'importe quelle fonction
- **Cross-session transfer** — copier chunks entre sessions
- **Rate limiting** — par session, configurable
- **Redaction** — secrets redacted avant stockage
- **Encryption** — chunks chiffrables (crypto.py)
- **Sandbox** — exécution code isolée pour verify
- **Compression profiles** — strict (100% lossless), balanced, aggressive

### Config (D:/Loom/pyproject.toml)
- deps: mcp>=1.2.0, fastmcp>=2.0.0, tiktoken>=0.7.0
- optional: faiss-cpu, sentence-transformers, numpy (semantic search)
- entry: `loom-mcp = "loom.server:main"`

## PROJET 3: ICYLOOM SQUELETTE (D:/icyloom/) — DÉJÀ CRÉÉ

### Structure (843 lignes, 9 fichiers)
```
D:/icyloom/
├── pyproject.toml
├── icyloom.yaml
├── README.md
├── icyloom/
│   ├── __init__.py
│   ├── __main__.py
│   ├── config.py       (79 l.)  — Pydantic config loader (YAML)
│   ├── server.py       (218 l.) — FastMCP + 14 tools (stub implementations)
│   ├── cache/
│   │   ├── store.py   (107 l.) — SQLite cache (set/get/TTL/LRU/stats/clear)
│   │   └── proxy.py   (136 l.) — httpx proxy + upstream routing + circuit breaker
│   └── context/
│       └── store.py   (295 l.) — Chroma + BM25 hybrid + chunker + dedup
└── tests/
    ├── test_cache.py
    └── test_config.py
```

### Tools définis dans le squelette (14)
- Cache (5): llm_chat_completion, llm_cache_stats, llm_cache_clear, llm_upstream_health, llm_route_request
- Context (7): ctx_store, ctx_search, ctx_get, ctx_list_sessions, ctx_purge_expired, ctx_transfer, ctx_profile
- Meta (2): icyloom_config_get, icyloom_health

## ISSUES RENCONTRÉES

1. **IcyCache normalize.py** — la normalisation avant hashing est critique pour le hit rate. Si on change la normalisation, le cache existant devient invalide. Il faut porter `icy_normalize.py` tel quel.

2. **IcyCache streaming SSE** — le cache streaming assemble les chunks SSE puis rejoue. Logique complexe dans `server.py` (lignes 725+). Ne pas réécrire, porter.

3. **Loom FAISS vs Chroma** — le squelette IcyLoom utilise Chroma, mais Loom utilise FAISS. Chroma est plus lourd mais plus simple. FAISS est plus rapide mais plus bas niveau. **Question: garder FAISS (port direct) ou migrer vers Chroma (squelette)?**

4. **Loom SQLite vs IcyCache SQLite** — les deux utilisent SQLite mais avec des schémas différents. IcyCache: cache table (hash, response, model, temperature). Loom: chunks table (chunk_id, session_id, content, tokens, embedding). **Question: 1 DB avec 2 tables, ou 2 DBs séparées?**

5. **Tool proxy IcyCache** — le tool_proxy.py (655 lignes) proxy+cache les outils MCP externes. C'est une feature unique qui n'a pas d'équivalent dans Loom. Il faut la garder mais elle ajoute beaucoup de complexité.

6. **Loop guard IcyCache** — détection de cycles d'outils. Loom a un rate limiter mais pas de loop guard. Les deux sont complémentaires.

7. **Loom auto_store decorator** — pattern élégant mais spécifique à Loom. Peut être gardé comme utility.

8. **Loom encryption/redaction** — features de sécurité qui n'existent pas dans IcyCache. À garder.

9. **Dashboards** — IcyCache a un dashboard HTML (static/dashboard.html). Loom a un dashboard (dash/). **On ne merge PAS les dashboards maintenant — ils viendront à la fin.**

10. **Hermes integration** — Hermes config.yaml pointe vers IcyCache (http://127.0.0.1:8080/v1). Après le merge, il faudra repointer vers IcyLoom. Loom est configuré comme MCP server stdio dans Hermes.

## CE QUE JE VEUX DE TOI (CLAUDE AI)

Prépare un **PLAN DE MERGE** complet et intelligent qui répond à:

### 1. Architecture cible
- Un seul paquet Python `icyloom` avec quelle structure de modules?
- Quels modules de IcyCache porter tels quels?
- Quels modules de Loom porter tels quels?
- Quels modules du squelette IcyLoom garder/modifier/jeter?
- Comment éviter la duplication (SQLite, HTTP client, config loader)?

### 2. Fusion des outils MCP
- Liste finale de TOUS les outils (IcyCache 11 + Loom 17 + IcyLoom squelette 14 = potentiellement 42, mais beaucoup de doublons)
- Quels outils fusionner? (ex: get_cache_stats + llm_cache_stats → 1 outil)
- Quels outils garder séparés?
- Quels outils nouveau créer? (ex: icyloom_health unifie ctx_health + get_cache_stats)
- Nommage final et catégories

### 3. Fusion des données
- 1 SQLite DB ou 2? Si 1, quel schéma?
- Que faire du cache existant (D:/IcyCache/db/cache.db)?
- Que faire des chunks Loom existants (D:/Loom/data/)?
- Migration strategy

### 4. Fusion des transports
- IcyCache: HTTP (8080) + MCP SSE (8081) + MCP HTTP (8084) + gateway (8082/8083)
- Loom: stdio
- IcyLoom cible: quels ports? quels transports?
- Comment Hermes se connecte (HTTP pour LLM, stdio pour MCP)?

### 5. Fusion des configs
- 1 fichier YAML unifié? Format?
- Quels paramètres de IcyCache garder?
- Quels paramètres de Loom garder?
- Variables d'environnement

### 6. Ordre d'implémentation (phases)
- Quelle phase en premier? (quoi merger en priorité)
- Dépendances entre phases
- Estimation effort par phase
- Points de validation à chaque phase

### 7. Risques et mitigations
- Quelles features risquent de casser?
- Comment tester que le merge ne perd aucune fonctionnalité
- Rollback strategy

### 8. Ce qu'on NE fait PAS
- Pas de dashboard (pour l'instant)
- Pas de nouveau feature
- Pas de refactor inutile
- Pas de changement d'API externe (Hermes doit pouvoir switcher sans modifier sa config)

## CONTRAINTES

- Python >=3.11
- Windows 11 (MSYS bash pour le dev)
- Budget 0$ — pas de service cloud payant
- Local-first — tout tourne sur la machine
- Dépendances existantes à réutiliser: fastmcp, httpx, pydantic, sqlite, tiktoken
- Dépendances Loom optionnelles: faiss-cpu, sentence-transformers (si on garde FAISS)
- Le code existant est TESTÉ et FONCTIONNEL — on porte, on ne réécrit pas

## FORMAT DE RÉPONSE ATTENDU

1. **Architecture cible** — diagramme ASCII de la structure de modules
2. **Table de fusion des outils** — tableau: outil source → outil cible → action (keep/merge/drop/new)
3. **Schéma DB unifié** — DDL SQLite
4. **Config YAML unifiée** — template complet
5. **Plan de phases** — numéroté avec dépendances + estimation
6. **Matrice de risques** — risque × probabilité × mitigation
7. **Checklist de validation** — comment vérifier que rien n'est perdu

Sois exhaustif. Demande-moi des clarifications si quelque chose n'est pas clair.
