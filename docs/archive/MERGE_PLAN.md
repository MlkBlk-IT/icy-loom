# IcyLoom — Plan de fusion IcyCache + Loom

> Document de planification. Aucun code n'est écrit à ce stade.
> Sources : lecture directe de `D:\IcyCache` (v0.3.0, commit `58b29a4`) et `D:\Loom`
> (v0.1.1), de leurs `UPDATE-NOTES.md` / `PROGRESS.md` mesurés, du code source,
> et de la configuration Hermes active.

---

## 0. Ce qui a changé depuis `PROMPT_CLAUDE_MERGE.md`

Le cahier des charges initial datait du 2026-10-24. **Trois de ses hypothèses structurantes
sont fausses aujourd'hui.** Toute la planification en découle.

| Le prompt disait | La réalité au 2026-10-26 | Conséquence |
|---|---|---|
| Loom utilise **FAISS** (`vector_index.py`) | Loom a migré sur **SQLite + FTS5**. FAISS n'est plus qu'un extra optionnel (`loom-semantic` est *stale*, contenu dans `main`) | La question « FAISS ou Chroma ? » est **caduque**. Ni l'un ni l'autre par défaut |
| Le squelette utilise Chroma, et il faut décider | Chroma est le **troisième larron** : ni IcyCache ni Loom ne l'utilisent. Il coûte ~500 Mo de deps et exige un modèle HF valide (celui du squelette est invalide) | **Chroma est abandonné.** Le squelette Chroma-BM25 part à la poubelle |
| « IcyCache : 10 fichiers / 4671 lignes » | **14 fichiers / 4485 lignes**, + `upstream_client.py`, `icy_semantic.py`, `icy_paths.py` (absents de l'inventaire) | L'inventaire du prompt est périmé sur les deux parents |

Deux faits mesurés supplémentaires, décisifs pour le plan :

- **IcyCache : suite de tests ALL PASS.** Le moteur de cache est sain et prouvé.
- **Loom : 83 passés / 19 échoués / 1 erreur de collection.** Et surtout, son propre
  `UPDATE-NOTES.md` (v0.1.1, aujourd'hui) déclare : *« Aucune économie de tokens n'est
  démontrée »*, *« Loom coûte plus de tokens que la baseline »*, critère gelé à
  **≥ 35 % de tokens en moins (in + out)** sinon pivot ou arrêt.

**Conclusion stratégique :** la moitié *cache* est prouvée, la moitié *compression* ne l'est pas.
Le merge n'a donc pas le droit d'activer la compression par défaut — il a le droit — et le
devoir — de fournir enfin le **tiers manquant** qui permet de mesurer les deux dans un seul
registre. C'est là que se trouve le vrai `1 + 1 > 2`.

---

## 1. Verdict sur le squelette IcyLoom existant

Le squelette (`D:\ICY-LOOM\icyloom\`, ~1000 lignes) **n'est pas une base de merge**. C'est une
troisième implémentation, plus faible que ses deux parents, sur les deux domaines.

| Outil du squelette | Verdict | Raison |
|---|---|---|
| `llm_chat_completion` | **SUPPRIMER** | IcyCache ne l'a jamais exposé en MCP. La gateway HTTP OpenAI-compatible (SSE, routage, drop-in) est strictement supérieure. Un outil MCP ne peut pas la remplacer sans régression |
| `llm_cache_stats` | **SUPPRIMER** | Doublon de `get_cache_stats` (IcyCache) |
| `llm_cache_clear` | **SUPPRIMER** | Doublon de `clear_cache` (IcyCache) |
| `llm_upstream_health` | **DIFFÉRER** | N'existe dans aucun parent. Utile, mais « pas de nouveau feature » → phase tardive |
| `llm_route_request` | **SUPPRIMER** | Le routage est interne à la gateway ; l'outil du squelette est de surcroît **buggé** (voir §8) |
| `ctx_store`, `ctx_search`, `ctx_get`, `ctx_list_sessions`, `ctx_purge_expired`, `ctx_transfer` | **SUPPRIMER** | Versions dégradées des outils Loom réels (Chroma au lieu de FTS5, pas d'ancres, pas d'invariants, pas de vérification lossless) |
| `ctx_profile` | **SUPPRIMER** | **Collision de sémantique** : chez Loom, `ctx_profile(mode)` *change* le profil ; chez le squelette, il *retourne des stats*. Loom a déjà `ctx_stats` pour ça. Le squelette a le mauvais contrat |
| `icyloom_config_get` | **GARDER** | Neuf, utile, ~10 lignes |
| `icyloom_health` | **GARDER** | Neuf, unifie cache + drawer + upstreams |

**Ce que le squelette garde :** le modèle de config Pydantic sur YAML, l'idée d'une config unifiée,
le dataclass `Upstream`, et 2 outils meta. **Ce qu'il perd :** ses 14 outils et ses 1000 lignes
d'implémentation.

---

## 2. Architecture cible

```
                    ┌──────────────────────────────────────────────────┐
                    │            icyloom  (1 paquet, 1 config)         │
                    │                                                  │
  Hermes ──LLM──▶  │  ┌────────────────────────┐   ┌───────────────┐  │
  (provider        │  │  PROCESS 1 : GATEWAY   │   │  PROCESS 2    │  │
   "icycache")     │  │  FastAPI  :8080        │   │  : MCP        │  │
        │          │  │  /v1/chat/completions  │   │  FastMCP      │  │
        └──────────┼─▶│  /v1/messages (Anthropic)│ │  31 outils    │  │
                   │  │  /v1/embeddings        │   │  stdio        │  │
  Hermes ──MCP──▶  │  │  /{upstream}/v1/*     │   │  SSE    :8081 │  │
  (mcp_servers)    │  │  /ws/events            │   │  StreamHTTP:8084│ │
        │          │  │  /health /api/*        │   └───────┬───────┘  │
        └──────────┼──┴──────────┬─────────────┘           │          │
                   │             │                         │          │
                   │      ┌──────▼──────────────────────────▼──────┐   │
                   │      │        icyloom.db  (1 fichier SQLite)   │   │
                   │      │  WAL · busy_timeout · 1 writer/1 writer │   │
                   │      └──────────────────┬──────────────────────┘   │
                   │                         │                          │
                   │   ┌─────────────────────┴────────────────────┐     │
                   │   │  DOMAINE CACHE (IcyCache)                │     │
                   │   │   normalize.py  ← 1122 règles, VERBATIM   │     │
                   │   │   cache_store   · llm_events             │     │
                   │   │   upstream_client ← FIX BUG-001/002       │     │
                   │   │   tool_proxy    · agent_events           │     │
                   │   │   prefix_proxy  · prefix_events          │     │
                   │   │   loop_guard · circuit_breaker            │     │
                   │   └──────────────────────────────────────────┘     │
                   │   ┌──────────────────────────────────────────┐     │
                   │   │  DOMAINE CONTEXTE (Loom)                 │     │
                   │   │   store.py  · chunks_fts (FTS5)          │     │
                   │   │   anchors.py · compress.py · verify.py   │     │
                   │   │   session.py · cross_session.py          │     │
                   │   │   crypto · redact · sandbox · ratelimit │     │
                   │   │   audit · auto_store · tokenizer         │     │
                   │   │   vector_index (FAISS, extra optionnel) │     │
                   │   └──────────────────────────────────────────┘     │
                   └──────────────────────────────────────────────────┘
```

### 2.1 Principes non-négociables

1. **Deux processus, un paquet.** La gateway HTTP et le serveur MCP restent dans des processus
   séparés — transports différents, domaines de panne différents. C'est **la topologie actuelle
   d'IcyCache** : on la reproduit à l'identique, donc Hermes ne voit aucune différence.
2. **Port, pas réécriture.** Chaque module parent est copié, ses imports ajustés, et le diff
   est explicite : « copié verbatim » ou « adapté, raison N ». Un module qui exige une réécriture
   est un module dont on a mal compris l parent's.
3. **Le merge n'aggrave aucun test.** Critère de sortie de chaque phase : *aucun* test parent ne
   passe de vert à rouge. Les 19 échecs Loom restent 19 échecs — ils sont corrigés dans une phase
   dédiée, pas pendant le merge (sinon on ne sait plus ce qui casse quoi).
4. **Une seule source de vérité par donnée.** Config dans `icyloom.yaml`, données dans
   `icyloom.db`, règles dans `config/normalization_rules.json`. Plus de config morte.

### 2.2 Ce qui n'est **pas** dans le périmètre

Dashboards (IcyCache `static/dashboard.html` + Loom `dash/`) — reportés en phase 8, comme demandé.
`warmup_cache.py`, `tray.py`, `install_autostart.ps1`, `start_*.ps1`, wrappers npm — hors périmètre
du MCP lui-même, à rediscuter en phase 8.

---

## 3. Fusion des outils — 28 parents + 3 meta = **31**

**Règle de nommage : verbatim.** Aucun renommage en v1. Les familles se distinguent déjà par leur
préfixe (`ctx_*` pour le contexte) ; la cohérence vient du substrat (config, DB, CLI, annotations),
pas du renommage. Renommer casserait l'habitude, les docs, et les politiques `icycache.*` /
`loom.*` du registre `tool_cache` — pour un gain cosmétique.

### 3.1 Cache (11 — IcyCache, noms conservés)

| # | Outil | Statut | Remarque |
|---|---|---|---|
| 1 | `get_cache_stats` | keep | |
| 2 | `clear_cache` | keep | garde `confirm='FORCE_DELETE'` |
| 3 | `normalize_preview` | keep | aperçu de normalisation + hash |
| 4 | `search_cached_prompts` | keep | |
| 5 | `proxy_tool` | keep | cache d'outil + garde anti-boucle |
| 6 | `proxy_tools` | keep | |
| 7 | `list_proxy_servers` | keep | 12 serveurs descendants |
| 8 | `list_proxy_tools` | keep | |
| 9 | `get_tool_cache_stats` | keep | |
| 10 | `clear_tool_cache` | keep | |
| 11 | `check_tool_loop` | keep | |

### 3.2 Contexte (17 — Loom, noms conservés)

`ctx_store` · `ctx_get` · `ctx_check` · `ctx_stats` · `ctx_profile` · `ctx_list_sessions` ·
`ctx_purge_session` · `ctx_purge_expired` · `ctx_vacuum` · `ctx_health` · `ctx_store_file` ·
`ctx_store_cmd` · `ctx_search` · `ctx_verify` · `ctx_transfer` · `ctx_rebuild_index` ·
`ctx_vector_stats`

> `ctx_profile(mode)` **change** le profil (strict | balanced | aggressive). `ctx_stats` donne les
> ratios. Les deux contrats Loom sont conservés tels quels — c'est le squelette qui avait inversé
> les deux.

### 3.3 Meta (3 — nouveaux)

| # | Outil | Rôle |
|---|---|---|
| 29 | `icyloom_config_get` | Config effective (une seule source) |
| 30 | `icyloom_health` | Cache + drawer + upstreams + version + chemins |
| 31 | `icyloom_stats` | **Le registre unifié** : tokens saved (cache) + tokens before/after (compression) + coût du tax d'outils. La mesure du critère des 35 % |

### 3.4 Tax de schémas — non-régression par construction

Hermes charge aujourd'hui `icycache` (11) + `loom` (17) = **28 schémas**. Le serveur unifié en
expose **31** (+3 meta). Le coût de contexte ne **augmente pas** par rapport à l'existant — le
merge est neutre de ce point de vue.

Amélioration offerte : `icyloom --toolsets cache,context` pour ne charger qu'un domaine. Un
client qui n'utilise que le cache ne paie que 11+3 schémas.

Tous les outils reçoivent les `ToolAnnotations` de Loom (`read_only_hint`, `destructive_hint`,
`idempotent_hint`, `open_world_hint`) — IcyCache n'en a pas aujourd'hui. C'est gratuit et ça
améliore le routage côté client.

---

## 4. Schéma DB unifié

**Un seul fichier.** Friction quasi nulle : Loom lit `LOOM_DB` (env) **au moment de l'appel** et
IcyCache a `cache.db_path` dans sa config — les deux pointent déjà vers un chemin paramétrable.
Unifier = un changement de **config**, pas de code.

| Domaine | Origine | Tables |
|---|---|---|
| Cache LLM | IcyCache | `cache`, `llm_events`, `agent_events`, `tool_cache` |
| Préfixe | IcyCache | `prefix_events` |
| Contexte | Loom | `chunks`, `anchors`, `invariants`, `sessions`, `sources`, `chunks_fts` (FTS5) |

### 4.1 ⚠️ La seule vraie collision : `events` / `events`

`icy_core.py` et `prefix_proxy.py` créent **toutes deux une table `events`**, avec des colonnes
**incompatibles**. `CREATE TABLE IF NOT EXISTS` fait que la seconde création est silencieusement
ignorée → `INSERT` échoue ensuite sur colonne inexistante. **C'est le piège principal de la
migration.** Résolu par renommage : `llm_events` (cache) et `prefix_events` (préfixe).

Aucune autre collision : `cache`/`events`/`agent_events`/`tool_cache` d'un côté,
`chunks`/`anchors`/`invariants`/`sessions`/`sources`/`chunks_fts` de l'autre — **zéro recouvrement**.

### 4.2 DDL unifié

```sql
PRAGMA journal_mode=WAL;          -- IcyCache le fait déjà ; à étendre à tout le monde
PRAGMA busy_timeout=5000;         -- NOUVEAU : 2 processus écrivant le même fichier

-- ═══════════ DOMAINE CACHE ═══════════
CREATE TABLE IF NOT EXISTS cache (
    prompt_hash      TEXT PRIMARY KEY,
    normalized_prompt TEXT,
    original_prompt  TEXT,
    response         TEXT,
    is_stream        INTEGER,
    provider         TEXT,
    model            TEXT,
    created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    last_accessed    TEXT DEFAULT CURRENT_TIMESTAMP,
    access_count     INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_cache_created  ON cache(created_at);
CREATE INDEX IF NOT EXISTS idx_cache_accessed ON cache(last_accessed);

CREATE TABLE IF NOT EXISTS llm_events (          -- RENOMMÉ depuis 'events'
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT DEFAULT CURRENT_TIMESTAMP,
    kind                TEXT NOT NULL,
    hash                TEXT,
    model_req           TEXT,
    model_real          TEXT,
    stream              INTEGER,
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    saved_prompt        INTEGER DEFAULT 0,
    saved_completion    INTEGER DEFAULT 0,
    latency_ms          INTEGER,
    ttft_ms             INTEGER,
    upstream            TEXT,
    client              TEXT,
    cached_tokens       INTEGER,
    cache_write_tokens  INTEGER,
    req_id              TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON llm_events(ts);
CREATE INDEX IF NOT EXISTS idx_events_hash ON llm_events(hash, kind);

CREATE TABLE IF NOT EXISTS agent_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT DEFAULT CURRENT_TIMESTAMP,
    kind              TEXT NOT NULL,
    server            TEXT,
    tool              TEXT,
    client            TEXT,
    args_hash         TEXT,
    bytes             INTEGER,
    latency_ms        INTEGER,
    saved_latency_ms  INTEGER,
    detail            TEXT
);

CREATE TABLE IF NOT EXISTS tool_cache (
    key            TEXT PRIMARY KEY,
    server         TEXT,
    tool           TEXT,
    result_json    TEXT,
    bytes          INTEGER,
    latency_ms     INTEGER,
    ttl            INTEGER,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    last_accessed  TEXT DEFAULT CURRENT_TIMESTAMP,
    hits           INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tool_cache_server ON tool_cache(server);

-- ═══════════ DOMAINE PRÉFIXE ═══════════
CREATE TABLE IF NOT EXISTS prefix_events (       -- RENOMMÉ depuis 'events'
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT DEFAULT CURRENT_TIMESTAMP,
    provider    TEXT,
    model       TEXT,
    stream      INTEGER,
    status      INTEGER,
    injected    TEXT,
    total_in    INTEGER,
    cache_read  INTEGER,
    cache_write INTEGER,
    out_tokens  INTEGER,
    latency_ms  INTEGER,
    measured    INTEGER
);

-- ═══════════ DOMAINE CONTEXTE ═══════════
CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    content    TEXT NOT NULL,
    tokens     INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    hits       INTEGER NOT NULL DEFAULT 0,
    source_sha TEXT,            -- colonnes additives Loom
    idx        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chunk_session ON chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_chunk_source  ON chunks(source_sha, idx);

CREATE TABLE IF NOT EXISTS anchors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    label      TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anchor_session ON anchors(session_id);

CREATE TABLE IF NOT EXISTS invariants (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    value      TEXT NOT NULL,
    priority   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_inv_session ON invariants(session_id);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    label      TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    ttl_days   INTEGER NOT NULL DEFAULT 30
);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at);

CREATE TABLE IF NOT EXISTS sources (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL,
    label          TEXT NOT NULL DEFAULT '',
    sha256         TEXT NOT NULL,
    bytes          INTEGER NOT NULL DEFAULT 0,
    n_chunks       INTEGER NOT NULL DEFAULT 0,
    first_chunk_id INTEGER,
    last_chunk_id  INTEGER,
    tokens_before  INTEGER NOT NULL DEFAULT 0,
    anchor_tokens  INTEGER NOT NULL DEFAULT 0,
    redactions     INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_session ON sources(session_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    label, content, content='chunks', content_rowid='id'
);
-- NOTE : Loom synchronise chunks_fts à la main (INSERT explicite + 'rebuild'),
-- sans triggers. Pas de triggers à créer ici : on garde sa mécanique.
```

### 4.3 Le registre unifié (la vue, pas une table)

Aucune nouvelle table : une **vue SQL** qui croise les deux domaines. C'est le livrable `1+1>2`.

```sql
CREATE VIEW IF NOT EXISTS v_token_ledger AS
-- Tokens économisés par le CACHE (mesures upstream réelles, jamais estimées)
SELECT 'cache'      AS domain, ts AS ts,
       prompt_tokens AS tokens_in, completion_tokens AS tokens_out,
       saved_prompt + saved_completion AS tokens_saved
FROM llm_events WHERE kind = 'hit'
UNION ALL
-- Tokens économisés par la COMPRESSION (avant → ancre)
SELECT 'compression', created_at,
       tokens_before, 0, tokens_before - anchor_tokens
FROM sources
UNION ALL
-- Tax d'outils : latence et octets économisés par le tool cache
SELECT 'tools', ts, 0, 0, saved_latency_ms
FROM agent_events;
```

---

## 5. Config unifiée — `icyloom.yaml`

Une seule config. Sections renommées **sans casser** les noms de clés que les parents lisaient :
les clés existantes sont conservées telles quelles, les nouvelles s'ajoutent.

```yaml
server:
  name: "icyloom"
  version: "1.0.0"
  # Transports : stdio (Hermes) + HTTP + SSE, comme IcyCache aujourd'hui
  transports:
    stdio: true
    streamable_http: { host: "127.0.0.1", port: 8084 }
    sse:          { host: "127.0.0.1", port: 8081 }

gateway:                        # ← IcyCache
  host: "127.0.0.1"
  port: 8080                    # INCHANGÉ : Hermes pointe ici
  default_upstream: "omniroute"
  ws_delta_interval_s: 0.25
  sticky_upstream: true

upstreams:                      # ← IcyCache, 18 entrées conservées telles quelles
  omniroute: { base_url: "http://localhost:20128", kind: "openai" }
  lmstudio:  { base_url: "http://localhost:1234",  kind: "openai" }
  ollama:    { base_url: "http://localhost:11434", kind: "openai" }
  openai:    { base_url: "https://api.openai.com", kind: "openai" }
  anthropic: { base_url: "https://api.anthropic.com", kind: "anthropic" }
  # … 13 autres, copiés à l'identique
  google-aistudio:
    base_url: "https://generativelanguage.googleapis.com/v1beta/openai"
    kind: "openai"
    strip_v1: true

cache:                          # ← IcyCache
  db_path: "db/icyloom.db"      # UNE seule base pour les deux domaines
  max_entries: 50000
  enable_semantic: false        # opt-in (Qdrant)
  semantic_threshold: 0.95
  ttl_seconds: 604800
  cache_when_temperature_unset: true
  embeddings_ttl_seconds: 604800

context:                        # ← Loom
  db_path: "db/icyloom.db"      # MÊME fichier
  profile: "strict"             # strict = lossless. balanced/aggressive = opt-in explicite
  session_ttl_days: 30
  chunk_tokens: 512
  auto_store_threshold: 500
  encryption: "auto"            # auto | none | fernet | sqlcipher
  semantic:
    enabled: false              # FAISS, extra optionnel
    backend: "faiss"

normalization:                  # ← IcyCache
  remove_politeness: true
  canonical_synonyms:
    config: configuration
    setup: configuration
    stp: ""
    "s'il te plaît": ""
    please: ""
    "can you": ""
    "peux-tu": ""
  rules_file: "config/normalization_rules.json"   # 1122 règles, VERBATIM

loop_guard:                     # ← IcyCache
  enabled: true
  window_seconds: 300
  tools: { warn_after: 3, block_after: 5, max_cycle_len: 3 }
  llm:   { warn_after: 3, action: warn, block_after: 6 }

tool_cache:                     # ← IcyCache
  enabled: true
  default: "allow"
  max_result_bytes: 10485760
  downstream:
    github:   { transport: "stdio", command: "npx", args: ["-y", "@modelcontextprotocol/server-github"] }
    fetch:    { transport: "stdio", command: "uvx", args: ["mcp-server-fetch@2026.10.10"] }
    omniroute:{ transport: "sse",   url: "http://127.0.0.1:20128/mcp" }
    # 'icycache' et 'loom' pointeurs vers icyloom lui-même → auto-référence,
    # voir §9 phase 6 (le registre se referme sur lui-même)
  policy:
    github.*: { ttl_seconds: 3600 }
    loom.*:   { ttl_seconds: 3600 }

routing:                        # ← squelette (l'unique apport conservé)
  strategy: "cost_latency"
  fallback: true
  circuit_breaker:
    failures: 3
    reset_seconds: 30           # ← DOIT être câblé (dans le squelette c'est du code mort)

paths:                          # ← IcyCache icy_paths.py
  data_dir: "~/.icyloom"        # installé : %LOCALAPPDATA%\IcyLoom
  config_dir: "~/.icyloom"
  logs: "logs/app.log"

logging:
  level: "INFO"
  file: "logs/app.log"
  max_size_mb: 10
  audit_jsonl: true              # ← Loom audit.py
```

### 5.1 Variables d'environnement (compatibilité descendante)

| Actuelle | Nouvelle | Note |
|---|---|---|
| `ICYCACHE_CONFIG` | `ICYLOOM_CONFIG` | **l'ancienne reste lue en fallback** |
| `LOOM_DB` | `ICYLOOM_DB` | idem — `LOOM_DB` reste lu en fallback |
| `LOOM_AUTO_STORE_THRESHOLD` | `ICYLOOM_AUTO_STORE_THRESHOLD` | idem — Hermes définit actuellement `LOOM_AUTO_STORE_THRESHOLD: "500"` |
| `LOOM_ALLOWED_DIRS` | `ICYLOOM_ALLOWED_DIRS` | idem |
| `LOOM_DASH_PORT` | — | dashboard reporté |

> **Hermes définit `LOOM_AUTO_STORE_THRESHOLD`** dans sa section `mcp_servers.loom`. Sans fallback,
> le merge ferait **perdre silencieusement** l'auto-store. C'est le genre de détail qui casse
> « sans rien casser ».

---

## 6. Plan de phases

Chaque phase a un **gate de validation** bloquant. Aucune phase ne commence avant que la
précédente soit verte.

| # | Phase | Contenu | Gate de validation | Est. |
|---|---|---|---|---|
| **0** | **Socle** | Paquet `icyloom/`, `paths.py`, `cli.py` (`serve`/`mcp`/`gateway`/`doctor`/`paths`/`version`/`migrate`), loader config Pydantic | `import icyloom` OK ; `icyloom doctor` affiche chemins + versions ; **les 2 parents tournent toujours en place** | 0,5 j |
| **1** | **Domaine cache** | Port verbatim de `icy_normalize.py` + les **1122 règles**, `icy_core.py`, `icy_cache_store.py`, `upstream_client.py` (fix BUG-001/002), `upstream_utils.py`, `icy_paths.py`, `tool_proxy.py`, `prefix_proxy.py` | Suite isolée IcyCache **ALL PASS** à l'identique (`tests/run_isolated.py`) ; **normalisation : 1122 règles, hash identique** à l'existant → le cache survit au move | 2 j |
| **2** | **Domaine contexte** | Port des 17 modules Loom (`store`, `anchors`, `compress`, `verify`, `session`, `cross_session`, `profile`, `tokenizer`, `crypto`, `redact`, `sandbox`, `ratelimit`, `audit`, `auto_store`, `vector_index`, `cli`, `server`) | Suite Loom : **83 passés / 19 échoués / 1 erreur de collection** — exactement l'état actuel. Aucun test parent ne devient rouge | 1,5 j |
| **3** | **DB unifiée** | Init unique (`icyloom.db`), renoms `events`→`llm_events`/`prefix_events`, `WAL` + `busy_timeout`, `icyloom migrate` (ATTACH + copie additive), **les anciennes bases restent intactes** | `icyloom migrate` : comptages avant/après **égaux** pour les 6 tables Loom et les 4 tables IcyCache ; les 2 fichiers d'origine sont inchangés (hash) | 1 j |
| **4** | **MCP unifié** | **Une seule** instance FastMCP, 31 outils, 3 transports (stdio / SSE 8081 / Streamable-HTTP 8084). Port du shim `mcp_stdio_bridge.py` **tel quel** (isolation stdout + cp1252) | Handshake stdio → **31 outils listés** ; idem sur SSE et HTTP ; **stdout reste 100 % JSON-RPC** (test d'encodage accents) | 2 j |
| **5** | **Config + outillage** | `icyloom.yaml` unifié, `normalization_rules.json` (1122) + `tools_registry.json` (100) portés, assertion startup `total == 100`, cache sémantique Qdrant **opt-in** | `icyloom config_get` reflète la config réelle ; l'assertion des 100 ancres passe au démarrage ; le cache sémantique reste **inactif par défaut** | 1 j |
| **6** | **Cut-over Hermes** | Config Hermes : `mcp_servers.icycache` + `mcp_servers.loom` → `mcp_servers.icyloom`. **Shims** laissés aux anciens chemins (`D:\IcyCache\src\mcp_stdio_bridge.py`, `loom-mcp.exe`) pour rollback instantané | Hermes : trafic LLM toujours sur `http://127.0.0.1:8080/v1` ; **le provider `icycache` fonctionne sans changer sa config** ; les 31 outils apparaissent une seule fois | 0,5 j |
| **7** | **Registre unifié** | Vue `v_token_ledger`, outil `icyloom_stats`, métriques croisées cache + compression + tax d'outils | Le critère gelé (**≥ 35 % de tokens en moins, in+out, tax d'outils inclus**) est **mesurable** et reproductible | 1,5 j |
| **8** | **Dashboard** *(différé, comme demandé)* | Fusion `static/dashboard.html` (IcyCache) + `dash/` (Loom), sur `v_token_ledger` | — | 1,5 j |

**Total ≈ 11,5 jours.**

### 6.1 Phases hors merge (à ne pas mélanger)

Ces corrections sont **réelles** mais **apparentent** à IcyLoom. Les traiter pendant le merge rend
la revue impossible (« ça casse » = merge ou bug d'origine ?). **Phase dédiée, après le cut-over.**

1. **Les 4 régressions d'ancres Loom** (`port` non produit, kinds rares absents du profil `strict`,
   dates extraites comme nombres) — ce sont les seules qui touchent au comportement.
2. **Les 15 échecs dus à un refactor non suivi** (signatures `_store_impl`, `MAX_NUMBERS_IN_ANCHOR`,
   `topup`, schémas `anyOf`, encodage UTF-16/cp1252, transport HTTP/SSE).
3. **L'erreur de collection** `test_allowed_dirs.py` (`_path_allowed` supprimé).
4. **`gateway.cache` mort** dans IcyCache (le bloc imbriqué n'est jamais lu → le cache sémantique
   annoncé est inactif). Ne pas rejouer l'erreur dans la config unifiée : une seule clé `cache`.
5. **Le `hit_rate` mal calculé** du squelette (`hits / total` au lieu de `hits / (hits+misses)`).

---

## 7. Matrice de risques

| # | Risque | Prob. | Impact | Signal précoce | Mitigation |
|---|---|---|---|---|---|
| R1 | **Collision `events`/`events`** entre `icy_core` et `prefix_proxy` → `INSERT` échoue en prod | **Élevée** | Élevé | `INSERT INTO events` échoue sur colonne `provider` absente | Renommage explicite `llm_events` / `prefix_events` au gate phase 3 ; test qui crée les 2 domaines dans le même fichier |
| R2 | **Compat Hermes cassée** (provider `icycache` sur :8080 + 2 serveurs MCP) | Moyenne | **Critique** | Le provider `icycache` ne résout plus | Topologie identique (2 processus, mêmes ports) ; shims conservés ; phase 6 = cut-over réversible ; test Hermes en dry-run |
| R3 | **Le bridge stdio stdout/cp1252 perdu** → crash au démarrage sur un accent | Moyenne | Élevé | Crash `UnicodeEncodeError` au handshake | Porter `mcp_stdio_bridge.py` **verbatim** ; test dédié d'encodage dans le gate phase 4 |
| R4 | **Le fix BUG-001 (502 bloqué après un 400) n'est pas porté** | Moyenne | Élevé | La gateway reste bloquée en 502 | `upstream_client.py` (82 lignes) porté en phase 1 + test de non-régression dédié |
| R5 | **Changer la normalisation invalide le cache existant** | Moyenne | Moyen | Hit rate qui s'effondre | `icy_normalize.py` + les 1122 règles **verbatim**, avec une assertion `total == 1122` ; test d'égalité de hash sur un corpus |
| R6 | **La compression coûte plus de tokens** (constat Loom v0.1.1) | **Élevée** | Moyen | `saved_pct` négatif | Profil `strict` par défaut, `balanced`/`aggressive` en opt-in explicite ; le merge **n'active rien** ; mesure honnête via `v_token_ledger` (gate phase 7) |
| R7 | **Contention SQLite** : 2 processus écrivant le même fichier | Moyenne | Moyen | `database is locked` dans les logs | `WAL` (déjà là) + `busy_timeout=5000` + timeout de retry ; test de 2 processus concurrents |
| R8 | **31 schémas d'outils** = tax de contexte | Moyenne | Moyen | Tokens d'outils en hausse dans Hermes | Non-régression (28 → 31 schémas) ; `--toolsets` pour ne charger qu'un domaine ; annotations Loom sur tous les outils |
| R9 | **Chiffrement Loom (SQLCipher/Fernet) perdu** dans la base unifiée | Moyenne | Élevé | Des chunks chiffrés deviennent illisibles | `crypto.py` porté + phase 3 explicite sur la stratégie de chiffrement de la base unifiée ; chiffrement de colonne plutôt que de fichier si les deux domains partagent le fichier |
| R10 | **Les 19 tests Loom qui échouent** peuvent masquer une régression du merge | **Quasi certaine** | Moyen | Impossible de distinguer merge-bug et bug-Loom | Critère de sortie strict : **le compte de tests ne doit pas diminuer** ; les échecs sontcompareds à la liste nominale (fichier × cause) |
| R11 | **Oubli d'un module du parent** (14 + 19 = 33 fichiers) | Moyenne | Moyen | Un outil « manque » au handshake | Inventaire de port explicite en annexe ; **compte de lignes par module** avant/après (4485 + 3419 = **7904** lignes) |
| R12 | **Les chiffres de bench invalidés** (`saved_pct` 94–99 %) repartent dans la doc | Moyenne | Moyen | README qui promet des économies | Le registre unifié remplace les chiffres ; la doc IcyLoom ne publie **que** des mesures reproductibles |
| R13 | **Le cache sémantique Qdrant** exige un service externe | Faible | Moyen | `ConnectionError` au démarrage | **Opt-in, silent miss** (déjà le comportement d'IcyCache) ; jamais activé par défaut |
| R14 | **Le registre `tool_cache` s'auto-référence** (`icycache`/`loom` pointant vers icyloom) | Moyenne | Faible | Boucle de proxy | Le registre pointe vers les servers **externes** ; icyloom n'y figure pas |

---

## 8. Bugs du squelette à ne pas porter

Relevés pendant l'audit de `D:\ICY-LOOM` — listés pour être **écrasés** par le port, pas hérités.

| Bug | Emplacement | Sort dans IcyLoom |
|---|---|---|
| `llm_route_request` laisse les autres upstreams à `priority=999` **définitivement** (le `finally` ne restaure que la cible) | `server.py:102-110` | Outil supprimé ; le routage de la gateway IcyCache le remplace |
| Un upstream en échec n'est **jamais retenté** (`healthy=False` → exclu de `_select_upstream` → `failures` bloqué à 3) | `cache/proxy.py:63` | Corrigé par le port de `upstream_client.py` (pool drain→recycle→retry) |
| `reset_seconds` du circuit breaker : **jamais implémenté** | `icyloom.yaml` vs `proxy.py` | Implémenté (config câblée) |
| Config ignorée au démarrage : `port=8090` et `transport` hardcodés | `server.py:214` | `main()` lit `_config` |
| `embedding_model: "local:qwen3-embedding-0.6b"` → id HuggingFace invalide | `icyloom.yaml:19` | **Chroma abandonné** → pas de modèle d'embedding par défaut |
| Connexions SQLite jamais fermées (`with sqlite3.connect()` commit mais ne ferme pas) → **verrou de fichier Windows**, 4 handles ouverts | `cache/store.py` | Fermeture explicite partout |
| `_rebuild_bm25()` reconstruit l'index complet **à chaque `store()`** → O(n²) | `context/store.py:151` | **BM25 abandonné** → FTS5 de Loom |
| `_evict_old()` fait `DELETE` + `COUNT(*)` complet **à chaque écriture** → O(n) | `cache/store.py:81-91` | Le store IcyCache (indexé) le remplace |
| `hit_rate = hits / total` — hits par entrée, pas un taux de succès | `cache/store.py:103` | `icyloom_stats` expose le vrai ratio |
| Dedup BM25 vs seuil 0.95 → **ne se déclenche jamais** | `context/store.py:91-100` | Supprimé avec le BM25 |
| `chunk_size` en caractères alors que les tokens sont comptés à part | `context/store.py:69` | `chunk_tokens` explicite |
| `config.py` promet « env var overrides » → seul `ICYLOOM_CONFIG` est lu | `config.py:60` | Overrides réels implémentés |
| `_ensure_init()` sans lock → double init en concurrence ; `close()` jamais appelé | `server.py:19` | Verrou + fermeture au shutdown |
| `prometheus-client`, `sqlite-utils` déclarés mais jamais importés | `pyproject.toml` | Retirés des deps |

---

## 9. Annexe — inventaire de port (aucune ligne perdue)

**IcyCache — 14 fichiers, 4485 lignes**

| Fichier | Lignes | Destination IcyLoom | Traitement |
|---|---|---|---|
| `icy_normalize.py` | 167 | `icyloom/cache/normalize.py` | **verbatim** (+ règles JSON 1122) |
| `icy_cache_store.py` | 198 | `icyloom/cache/store.py` | verbatim |
| `icy_core.py` | 321 | `icyloom/cache/core.py` | verbatim, table `events` → `llm_events` |
| `upstream_client.py` | 82 | `icyloom/cache/upstream_client.py` | **verbatim** — porte le fix BUG-001 |
| `upstream_utils.py` | 115 | `icyloom/cache/upstream_utils.py` | verbatim |
| `icy_paths.py` | 56 | `icyloom/paths.py` | verbatim |
| `icy_semantic.py` | 166 | `icyloom/cache/semantic.py` | verbatim, opt-in Qdrant |
| `tool_proxy.py` | 544 | `icyloom/cache/tool_proxy.py` | verbatim |
| `prefix_proxy.py` | 286 | `icyloom/cache/prefix_proxy.py` | verbatim, table `events` → `prefix_events` |
| `server.py` | 985 | `icyloom/gateway/app.py` | adapté (config + chemins) |
| `mcp_server.py` | 1192 | `icyloom/mcp/server.py` | **fusionné** avec les outils Loom → 1 instance FastMCP |
| `mcp_gateway.py` | 294 | — | **redondant** : la gateway HTTP expose déjà `/api/tools*` |
| `mcp_stdio_bridge.py` | 61 | `icyloom/mcp/stdio_bridge.py` | **verbatim** (isolation stdout + cp1252) |
| `mcp_stdio_entry.py` | 18 | `icyloom/mcp/stdio_entry.py` | verbatim |

**Loom — 19 fichiers, 3419 lignes**

| Fichier | Lignes | Destination IcyLoom | Traitement |
|---|---|---|---|
| `server.py` | 579 | `icyloom/mcp/tools_context.py` | adapté (17 outils → même instance FastMCP) |
| `store.py` | 567 | `icyloom/context/store.py` | adapté (`DB_PATH` → paramètre `db_path`) |
| `anchors.py` | 346 | `icyloom/context/anchors.py` | verbatim |
| `compress.py` | 273 | `icyloom/context/compress.py` | verbatim |
| `crypto.py` | 216 | `icyloom/context/crypto.py` | verbatim |
| `sandbox.py` | 213 | `icyloom/context/sandbox.py` | verbatim |
| `cross_session.py` | 180 | `icyloom/context/cross_session.py` | verbatim |
| `session.py` | 249 | `icyloom/context/session.py` | verbatim |
| `vector_index.py` | 240 | `icyloom/context/vector_index.py` | verbatim, extra optionnel |
| `auto_store.py` | 101 | `icyloom/context/auto_store.py` | verbatim |
| `audit.py` | 94 | `icyloom/context/audit.py` | verbatim |
| `ratelimit.py` | 68 | `icyloom/context/ratelimit.py` | verbatim |
| `profile.py` | 63 | `icyloom/context/profile.py` | verbatim |
| `redact.py` | 42 | `icyloom/context/redact.py` | verbatim |
| `tokenizer.py` | 23 | `icyloom/context/tokenizer.py` | verbatim |
| `verify.py` | 72 | `icyloom/context/verify.py` | verbatim |
| `cli.py` | 84 | `icyloom/cli.py` | fusionné avec la CLI IcyCache |
| `__main__.py` | 6 | `icyloom/__main__.py` | verbatim |
| `__init__.py` | 3 | `icyloom/context/__init__.py` | verbatim |

**Config à porter :** `config/normalization_rules.json` (1122 règles), `config/tools_registry.json`
(100 ancres), `config/semantic_cache.json`, `config/icycache.gateway.json` → `icyloom.yaml`.

---

## 10. Checklist de validation finale

**Fonctionnel**
- [ ] Handshake MCP stdio → **31 outils** listés
- [ ] Handshake SSE + Streamable-HTTP → **31 outils** listés
- [ ] `http://127.0.0.1:8080/v1/chat/completions` répond (drop-in OpenAI)
- [ ] `http://127.0.0.1:8080/v1/messages` répond (drop-in Anthropic)
- [ ] `/{upstream}/v1/...` route par préfixe (18 upstreams)
- [ ] Streaming SSE : assemblage + **rejeu depuis le cache**
- [ ] `normalize_preview` → hash **identique** à IcyCache actuel
- [ ] `ctx_store` → ancre + IDs + rapport d'économie
- [ ] `ctx_search` → FTS5 + semantique (FAISS) optionnelle
- [ ] `ctx_verify` → couverture 100 % des invariants
- [ ] `ctx_profile("strict")` → **lossless** (jamais `aggressive` par défaut)
- [ ] `proxy_tool` → cache d'outil + garde anti-boucle active
- [ ] `check_tool_loop` → détection de cycle

**Non-régression**
- [ ] Suite IcyCache isolée : **ALL PASS** (identique)
- [ ] Suite Loom : **83 passés / 19 échoués / 1 erreur** (pas moins)
- [ ] Normalisation : `total_rules == 1122`
- [ ] Registre d'outils : `total == 100` (assertion startup)
- [ ] Cache existant : les hits survivent au move (hash identiques)

**Intégration**
- [ ] Hermes : provider `icycache` → `http://127.0.0.1:8080/v1` inchangé
- [ ] Hermes : les 31 outils MCP listés **une seule fois**
- [ ] `LOOM_AUTO_STORE_THRESHOLD` toujours honoré (auto-store actif)
- [ ] `LOOM_ALLOWED_DIRS` / `ICYCACHE_CONFIG` toujours honorés (fallback)

**Données**
- [ ] `icyloom migrate` : comptages avant/après **égaux** (11 tables)
- [ ] Les 2 bases d'origine **inchangées** (hash) → rollback = repointer la config
- [ ] `busy_timeout` actif ; pas de `database is locked` sur 2 processus concurrents

**Mesure (le vrai livrable)**
- [ ] `v_token_ledger` peuplé : cache + compression + tax d'outils
- [ ] Le critère **≥ 35 % de tokens en moins (in+out)** est mesurable et reproductible
- [ ] Aucun `saved_pct` non mesuré dans la documentation

---

## 11. Décisions prises (réponses aux 10 questions du cahier des charges initial)

| # | Question | Décision |
|---|---|---|
| 1 | Architecture cible | Un paquet `icyloom`, **2 processus** (gateway :8080 + MCP), un module par fichier parent, import ajusté |
| 2 | Quels modules porter | **Les 33** (14 IcyCache + 19 Loom), dont 2 redondants (`mcp_gateway.py`) ou dead-code (`mcp_stdio_entry.py`) à arbitrer |
| 3 | Chroma / FAISS | **Ni l'un ni l'autre par défaut** : SQLite **FTS5** (Loom, prouvé) ; FAISS en extra optionnel ; Chroma **abandonné** |
| 4 | 1 DB ou 2 | **1 fichier**, 2 domaines, aucun recouvrement de tables ; `events`/`events` est la seule collision → renommé |
| 5 | Cache existant / chunks Loom | Migration **additive et réversible** (`icyloom migrate`, ATTACH), les originaux **intacts** |
| 6 | Ports / transports | **Inchangés** : 8080 (gateway), 8081 (SSE), 8084 (HTTP), stdio (Hermes) ; MCP unifié sur les trois |
| 7 | Config | **Un** `icyloom.yaml`, clés des parents **conservées**, sections Loom ajoutées, fallback des env vars historiques |
| 8 | Ordre d'implémentation | Socle → cache → contexte → DB → MCP → config → cut-over → registre → dashboard |
| 9 | Risques | 14 risques cartographiés (§7), R1 (collision `events`) et R2 (compat Hermes) treated en priorité |
| 10 | Ce qu'on ne fait pas | Pas de dashboard avant la phase 8, pas de nouveau feature, pas de refactor, **pas de changement d'API externe** |

---

*Plan établi le 2026-10-26 sur l'état mesuré de `D:\IcyCache` @ `58b29a4` (v0.3.0) et
`D:\Loom` @ `560ccdd` (v0.1.1). Aucune modification de code n'a été effectuée.*
