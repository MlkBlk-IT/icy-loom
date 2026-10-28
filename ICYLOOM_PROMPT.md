# PROMPT — IcyLoom : nouveau serveur MCP unifié (IcyCache + Loom)

> Ce document est un prompt autonome. Tu n'as aucun contexte en dehors de celui-ci.
> Les sources sont sur cette machine : `D:\IcyCache` et `D:\Loom`.
> Le produit à construire ira dans `D:\ICY-LOOM` (dossier quasi vide aujourd'hui).

---

## 1. MISSION

Construire **IcyLoom**, un **nouveau serveur MCP unifié** qui réunit la force de deux projets
existants et fonctionnels :

- **IcyCache** (`D:\IcyCache`) — cache LLM + proxy multi-upstream + proxy d'outils MCP
- **Loom** (`D:\Loom`) — compression sémantique de contexte + tiroir de chunks + recherche

Ce n'est **ni une juxtaposition** (deux serveurs dans un même paquet) **ni une fusion
mécanique** (copier-coller +renommer). C'est une **réécriture propre** qui garde **le meilleur
de chaque parent**, supprime leurs faiblesses, et les fait travailler ensemble pour un but
unique : **faire coûter moins de tokens et moins d'argent à l'utilisateur, sans rien perdre
en qualité de réponse.**

Les deux parents sont **déjà écrits, testés et documentés**. Tu as le droit — et même
l'obligation — de **réécrire** : l'objectif premier est d'éliminer les bugs et lesangles morts
des deux parents, pas de préserver leur code ligne à ligne.

**Résultat attendu : un seul produit, une seule config, une seule base, un seul serveur MCP.**

---

## 2. CONTEXTE MESURÉ (à ne pas redécouvrir)

J'ai audité les deux parents. Voici l'état **réel**, mesuré, au 2026-10-26.

### IcyCache — v0.3.0, commit `58b29a4`

- Stack : FastAPI + uvicorn + MCP 1.30 (`mcp>=1.2.0,<2` obligatoire : en `mcp` 2.x, FastMCP a
  été renommé `MCPServer` → `ModuleNotFoundError` au démarrage) + SQLite.
- 14 fichiers Python, **4485 lignes** dans `src/`.
- 18 upstreams : omniroute, lmstudio, ollama, openai, anthropic, nvidia, groq, cerebras,
  openrouter, kilo, nous, copilot, ai-gateway, huggingface, google-aistudio, llm7io, mistral,
  cloudflare. `omniroute` = défaut. `google-aistudio` a un flag `strip_v1`.
- Normalisation : moteur autonome `icy_normalize.py` (NFKD → minuscules → Aho-Corasick →
  accents → ponctuation) + **1122 règles** dans `config/normalization_rules.json`
  (règles de synonymes canoniques + conversion nombre-mot→chiffre, ex. « two plus two » → `2+2`).
  **Le hash de cache dépend de ces règles : les modifier invalide le cache existant.**
- Cache : SQLite, TTL 7 j, 50 000 entrées max, LRU. Table `cache` (PK `prompt_hash`).
- Cache sémantique : **opt-in**, embeddings + **Qdrant**, seuil 0.95, **inactif par défaut**.
- Comptage de tokens : la table `events` enregistre les tokens **réels remontés par l'upstream**,
  jamais estimés (`prompt_tokens`, `completion_tokens`, `saved_prompt`, `saved_completion`).
- Proxy d'outils MCP : `tool_proxy.py` (544 l.) — cache les résultats d'outils de **12 serveurs
  descendants** (github, fetch, omniroute, qwen-chroma, icycache, loom, wispr_flow_mcp, 4× dak-*),
  avec **TTL par serveur** (politique `github.*` → 3600 s, etc.) et **garde anti-boucle**.
- `upstream_client.py` (82 l.) : pool de clients httpx par URL de base, cycle
  **drain → recycle → retry**. C'est le fix du **BUG-001** (gateway bloquée en 502 après un 400).
- `prefix_proxy.py` : cache de préfixe anthropique (`cache_control`) / OpenAI (`prompt_cache_key`),
  **base séparée**.
- API HTTP : `/v1/chat/completions`, `/v1/messages` (Anthropic), `/v1/embeddings`, `/v1/models`,
  `/{upstream}/v1/{path}` (routage par préfixe), `/ws/events` (WebSocket live), `/health`,
  `/api/status`, `/api/dashboard-stats`, `/api/metrics`, `/api/metrics/series`,
  `/api/metrics/export`, `/api/cache-clear`, `/api/tools`, `/api/tools/stats`,
  `/api/tools/{server}`, `/api/live`, `/api/agent-events`, `/dashboard` (HTML).
- Transports MCP : stdio, SSE (8081), Streamable-HTTP (8084), + une façade gateway (8083).
- Garde anti-boucle : fenêtre 300 s, warn à 3, block à 5, cycle max 3.
- Registre d'outils : `config/tools_registry.json` = **100 ancres MCP vérifiées**, assertion
  au démarrage `total == 100`.
- CLI : `icycache serve` / `mcp --mode {proxy,mcp-stdio,mcp-sse,mcp-http,all}` / `doctor` /
  `paths` / `version`. Wheel + sdist + wrapper npm.
- **Tests : ALL PASS** via `tests/run_isolated.py` (runner obligatoire : `pytest` direct
  appelle `sys.exit()` si `CF_BASE` est absent ; le runner monte un faux upstream sur `:8191`).

### Loom — v0.1.1, commit `560ccdd`

- Stack : MCP + FastMCP + tiktoken. 19 fichiers, **3419 lignes** dans `src/loom/`.
- **17 outils MCP.**
- **Tiroir SQLite + index FTS5** (`chunks_fts`, external-content, synchronisé à la main, sans
  triggers). Plus de FAISS par défaut : FAISS est un extra optionnel (`faiss-cpu`,
  `sentence-transformers`, `numpy`) ; la branche `loom-semantic` est *stale*.
- Tables : `chunks`, `anchors`, `invariants`, `sessions`, `sources`, `chunks_fts`.
  `sources` porte **`sha256`** (adressage par contenu), `tokens_before`, `anchor_tokens`,
  `redactions`. `chunks` porte `source_sha` + `idx`.
- Compression par **ancre** : `compress.py` construit une ancre à partir d'invariants
  extraits par `anchors.py` (nombres, chemins, URLs, versions, hashes, **ports**, contraintes).
  3 profils : `strict` (zéro perte) / `balanced` / `aggressive` (lossy, opt-in explicite).
- `verify.py` : contrôle de couverture — **prouve** qu'aucun invariant n'a été perdu.
- Sessions : UUIDv7, TTL 30 j, isolation, purge. `cross_session.py` : transfert inter-sessions.
- Sécurité : `crypto.py` (SQLCipher base **ou** Fernet colonnes), `redact.py` (masque
  `Bearer`, `sk-`, `password=`, `secret=`, `token=`), `sandbox.py` (Job Object Windows /
  bwrap-firejail Linux, fail-closed), `ratelimit.py` (token bucket par session/IP),
  `audit.py` (JSONL, rotation 30 j).
- `auto_store.py` : décorateur qui range automatiquement une sortie d'outil volumineuse et
  renvoie une ancre.
- CLI admin : `loom health|stats|sessions|search|vacuum|purge|purge-expired|serve|version`.
- Dashboard `dash/` (port configurable `LOOM_DASH_PORT`, défaut 8775).
- **Tests : 83 passés / 19 échoués / 1 erreur de collection** sur 103 fonctions.
  - 4 échecs = **vraies régressions de comportement** : le kind `port` n'est plus produit
    (`PORT = 8765` → `{env_var, number}` au lieu de `{port}`), les kinds rares n'atteignent
    plus l'ancre en profil `strict`, les dates sont encore extraites comme nombres.
  - 15 échecs = refactor du code non suivi d'une mise à jour des tests (`_store_impl`,
    `MAX_NUMBERS_IN_ANCHOR` → `MAX_INVARIANTS_IN_ANCHOR`, kwarg `topup`, schémas `anyOf`,
    encodage UTF-16/cp1252, transport HTTP/SSE).
  - 1 erreur de collection : `tests/test_allowed_dirs.py` importe `_path_allowed`, supprimé
    au profit d'un contrôle en ligne de `LOOM_ALLOWED_DIRS`.

### Ce que Loom dit de lui-même (c'est capital)

`D:\Loom\UPDATE-NOTES.md` v0.1.1, section *Honest status* :

> « **Pas prêt pour la production.** Aucune économie de tokens n'a été démontrée : les chiffres
> `saved_pct` publiés (94–99 %) ne mesuraient que la taille de l'ancre et sont **invalides**.
> La mesure réelle (LM Studio, petits modèles, petits documents) montre Loom **coûting plus de
> tokens** que la baseline, à cause de la surcharge fixe de l'ancre et des schémas d'outils.
> Le test grand-contexte avec un modèle cloud n'a pas été fait.
> **Critère de décision gelé avant le benchmark :** même taux de succès avec **≥ 35 % de tokens
> en moins** (entrée + sortie, taxes de récupération et d'outils incluses). Sinon : pivot ou arrêt. »

Donc : **la moitié cache est prouvée, la moitié compression ne l'est pas.** C'est le problème
central d'IcyLoom et ta principale cible.

### Intégration existante (Hermes)

`C:\Users\Tech\AppData\Local\hermes\config.yaml` :
- `providers.icycache` → `base_url: http://127.0.0.1:8080/v1` — c'est le **trafic LLM** d'Hermes.
- `mcp_servers.icycache` → `python D:\IcyCache\src\mcp_stdio_bridge.py`
- `mcp_servers.loom` → `D:\Loom\.venv\Scripts\loom-mcp.exe`, env `LOOM_AUTO_STORE_THRESHOLD: "500"`
- `mcp_servers` contient aussi `qwen-chroma`, `wispr_flow_mcp`, `dak-*`.

---

## 3. CONTRAINTES DURES

1. **Réécriture autorisée et encouragée.** Objectif : zéro bug connu, zéro config morte, zéro
   angle mort. Ne porte pas du code mort, ne recopie pas des bugs « pour être faithful ».
2. **Ports : JAMAIS deux services sur le même port.** Et **tous les ports d'IcyLoom doivent être
   distincts de ceux déjà pris** (voir §6). Le but : pouvoir faire tourner IcyLoom **en parallèle**
   des deux parents pendant la migration.
3. **Zéro perte de fonctionnalité.** Toute capacité d'IcyCache et de Loom listée au §5 doit
   exister dans IcyLoom. Si tu fusionnes deux outils en un, ou si tu en retires un, tu
   **justifies explicitement** que la capacité couverte est intacte. **Aucune capacité
   d'IcyCache ne peut disparaître « au profit » d'un équivalent de Loom qui fait moins.**
4. **Une seule config** (un seul fichier), **une seule base** (un seul fichier SQLite), **un seul
   serveur MCP** exposant toutes les capacités.
5. **Local-first, 0 $.** Aucun service cloud payant, aucun appel réseau sortant obligatoire.
6. **Python ≥ 3.11**, cible Windows 11 (dev en PowerShell 7).
7. **Ne casse pas Hermes.** Le trafic LLM d'Hermes pointe sur `http://127.0.0.1:8080/v1`.
   IcyLoom doit pouvoir reprendre ce rôle sur un nouveau port, **et** Hermes doit pouvoir
   remplacer ses 2 entrées MCP (`icycache` + `loom`) par **une seule** entrée `icyloom`.
   L'auto-store doit continuer de lire `LOOM_AUTO_STORE_THRESHOLD`.
8. **Compatibilité descendante des variables d'environnement** : lis `ICYCACHE_CONFIG`,
   `LOOM_DB`, `LOOM_AUTO_STORE_THRESHOLD`, `LOOM_ALLOWED_DIRS` en fallback des nouveaux noms.
9. **Aucune régression de test.** Les tests des parents servent de référence : on ne doit
   jamais transformer un vert en rouge. Les 19 échecs Loom sont *connus* — les porter tels
   quels est acceptable, les corriger est un bonus (phase dédiée).
10. **Le MCP doit être economically viable** : c'est le but. Si la compression coûte plus de
    tokens qu'elle n'en économise, le système n'a pas le droit d'activer la compression par
    défaut.

---

## 4. CE QUE J'AI TROUVÉ — LES PIÈGES (lis ça avant de concevoir)

### 4.1 Collisions réelles entre les deux parents

| Piège | Détail | Correction attendue |
|---|---|---|
| **Table `events` × 2** | `icy_core.py` et `prefix_proxy.py` créent **tous deux** une table `events`, avec des **colonnes incompatibles**. `CREATE TABLE IF NOT EXISTS` rend la 2ᵉ création silencieusement inopérante → les `INSERT` échouent ensuite sur colonne absente | Tables au nom distinct (`llm_events`, `prefix_events`) dans la base unifiée |
| **Deux configs** | IcyCache JSON (`icycache.gateway.json` + `icycache.json` + `normalization_rules.json` + `semantic_cache.json` + `tools_registry.json`), Loom YAML/pyproject/env | Un seul fichier de config unifié, les règles externalisées gardées en JSON à côté |
| **Deux loci de DB** | IcyCache `db/cache.db` + `db/prefix.db` ; Loom `~/.loom/loom.db` | Un seul fichier |
| **Variable d'env** | `LOOM_DB` (Loom) vs `cache.db_path` (IcyCache) | Un seul mécanisme, chemins résolus depuis la config unique |

### 4.2 Bugs et faiblesses à ne pas reproduire

**IcyCache**
- `gateway.cache` (bloc imbriqué dans la config) **n'est jamais lu** → le cache sémantique
  annoncé dans le README est inactif à l'exécution. **Config morte : à ne pas reproduire.**
- `tests/test_gateway.py` et `tests/test_prefix_proxy.py` **ne sont jamais exécutés** par le
  runner vert → la gateway, cœur du projet, n'est pas couverte.
- Chemins absolus codés en dur (`D:\IcyCache\db\cache.db`, `D:\Loom\.venv\Scripts\loom-mcp.exe`).
- `tool_cache.default` passé à `allow` + `max_result_bytes` à 10 Mio : tout outil exposé est
  cachable, à surveiller côté mémoire.

**Loom**
- Le compression **coûte plus de tokens** que la baseline sur modèles locaux (§2).
- 4 régressions d'ancres réelles : kind `port` non produit, kinds rares absents du profil
  `strict`, dates extraites comme nombres.
- `README` annonçait 13 outils (il y en a 17) et « 72 tests pass » (il y en a 83/19) → les docs
  mentent, `bench_results.json` / `bench-report.json` / `REPORT_FINAL.md` contiennent encore des
  chiffres **invalidés** (`saved_pct` jusqu'à 99.95 avec `coverage` à 0.018).

**Le squelette existant `D:\ICY-LOOM` (à jeter, ne pas réutiliser)**
C'est une 3ᵉ implémentation, plus faible que ses parents (~1000 lignes). Bugs relevés :
- `llm_route_request` laisse les autres upstreams à `priority=999` **définitivement** (le
  `finally` ne restaure que la cible) → le routage est détruit après un seul appel.
- Un upstream en échec n'est **jamais retenté** (`healthy=False` → exclu → `failures` bloqué à 3).
- `reset_seconds` du circuit breaker : **jamais implémenté** (code mort).
- Config ignorée au démarrage (`port`/`transport` hardcodés).
- `embedding_model: "local:qwen3-embedding-0.6b"` → id HuggingFace **invalide**.
- Connexions SQLite jamais fermées (`with sqlite3.connect()` commit mais ne ferme pas) →
  **verrou de fichier sous Windows**.
- `_rebuild_bm25()` reconstruit l'index **à chaque écriture** → O(n²).
- `hit_rate = hits / total` → hits par entrée, pas un taux de succès.

---

## 5. INVENTAIRE DES FONCTIONNALITÉS À PRÉSERVER

### 5.1 Côté IcyCache (cache & réseau)

| # | Capacité | Contrainte de préservation |
|---|---|---|
| A1 | Proxy HTTP OpenAI-compatible `/v1/chat/completions` | Non négociable (Hermes) |
| A2 | Proxy HTTP Anthropic-compatible `/v1/messages` | Non négociable |
| A3 | `/v1/embeddings`, `/v1/models` | Non négociable |
| A4 | Routage par préfixe `/{upstream}/v1/...` | Non négociable |
| A5 | 18 upstreams, routage par priorité **préfixe > header `X-IcyCache-Upstream` > champ `upstream` du body > défaut**, `sticky_upstream` | Non négociable |
| A6 | Normalisation canonique (1122 règles + synonymes + nombre-mot→chiffre) + aperçu de normalisation + hash | **Le hash doit rester compatible** avec le cache existant, sinon tout le cache est perdu |
| A7 | Cache LLM (TTL, LRU, max entries) | Non négociable |
| A8 | Cache sémantique (embeddings + Qdrant), **opt-in**, silent miss si indisponible | Non négociable |
| A9 | Comptage de tokens **réels** (jamais estimés) + savings | Non négociable — c'est la base de la preuve économique |
| A10 | Cache de préfixe anthropique / OpenAI | À garder |
| A11 | Proxy d'outils MCP : `proxy_tool` (unitaire + batch), liste des serveurs/outils cibles, stats, purge | Non négociable — **c'est la capacité qui a le plus souffert de mes suppressions, ne la perd pas** |
| A12 | Cache des résultats d'outils + TTL par serveur + politique par motif (`github.*`, `loom.*`…) + 12 serveurs descendants | Non négociable |
| A13 | Garde anti-boucle d'outils (fenêtre, warn/block, cycle max) | Non négociable |
| A14 | Pool client résilient : drain → recycle → retry (fix BUG-001) | Non négociable |
| A15 | Circuit breaker avec **reset réel** (le reset est du code mort chez IcyCache) | À corriger |
| A16 | WebSocket `/ws/events` (flux live) | À garder |
| A17 | API REST `/health`, `/api/status`, `/api/dashboard-stats`, `/api/metrics(+series,+export)`, `/api/cache-clear`, `/api/tools(+stats,+{server})`, `/api/live`, `/api/agent-events` | À garder |
| A18 | Dashboard HTML | À garder |
| A19 | Transports MCP : **stdio**, **SSE**, **streamable-HTTP** | Non négociable |
| A20 | Registre d'outils : 100 ancres vérifiées, assertion `total == 100` au démarrage | Non négociable |
| A21 | 11 outils MCP IcyCache : `get_cache_stats`, `clear_cache`, `normalize_preview`, `search_cached_prompts`, `proxy_tool`, `proxy_tools`, `list_proxy_servers`, `list_proxy_tools`, `get_tool_cache_stats`, `clear_tool_cache`, `check_tool_loop` | **Capacités à ne pas perdre** — tu peux renommer/fusionner si la capacité survit, mais justifies |
| A22 | CLI (`serve`, `mcp --mode …`, `doctor`, `paths`, `version`) + wheel/sdist + wrapper npm | À garder |
| A23 | `warmup_cache.py` (amorce le cache depuis les prompts système d'Hermes) | À garder |
| A24 | Bridge stdio : stdout réservé au JSON-RPC, tout le texte/logs vers stderr, UTF-8 forcé (sinon crash `cp1252` sur les accents) | **À porter tel quel** |

### 5.2 Côté Loom (contexte)

| # | Capacité | Contrainte de préservation |
|---|---|---|
| B1 | 17 outils MCP Loom : `ctx_store`, `ctx_store_file`, `ctx_store_cmd`, `ctx_get`, `ctx_check`, `ctx_stats`, `ctx_profile`, `ctx_list_sessions`, `ctx_purge_session`, `ctx_purge_expired`, `ctx_vacuum`, `ctx_health`, `ctx_search`, `ctx_verify`, `ctx_transfer`, `ctx_rebuild_index`, `ctx_vector_stats` | Capacités à ne pas perdre |
| B2 | Compression par ancre, 3 profils (`strict` lossless / `balanced` / `aggressive` opt-in) | Non négociable |
| B3 | Extraction d'invariants (nombres, chemins, URLs, versions, hashes, **ports**, dates, contraintes) | Non négociable — c'est ce qui rend l'ancre *lossless* |
| B4 | Vérification de couverture (`ctx_verify`) — preuve qu'aucun invariant n'est perdu | **Non négociable.** C'est la garantie qui rend la compression acceptable |
| B5 | Tiroir SQLite + **FTS5** + recherche hybride (keyword + sémantique optionnel) | Non négociable |
| B6 | Sessions : UUIDv7, TTL, isolation, purge, listage, stats | Non négociable |
| B7 | Transfert inter-sessions | Non négociable |
| B8 | Chiffrement (SQLCipher base ou Fernet colonnes) | Non négociable |
| B9 | Redaction des secrets avant stockage | Non négociable |
| B10 | Sandbox d'exécution (Job Object Windows / bwrap Linux, fail-closed) pour `ctx_store_cmd` | Non négociable |
| B11 | Rate limiting (token bucket par session/IP) | Non négociable |
| B12 | Audit log JSONL + rotation 30 j | Non négociable |
| B13 | Auto-store (sortie d'outil volumineuse → ancre) | Non négociable |
| B14 | Index vectoriel optionnel (FAISS) + stats | À garder (optionnel par défaut) |
| B15 | CLI admin (`health`, `stats`, `sessions`, `search`, `vacuum`, `purge`, `purge-expired`) | À garder |
| B16 | Dashboard Loom (`dash/`) | À garder |

---

## 6. PORTS — RÈGLE ABSOLUE

**Aucun service ne partage un port. Aucun port d'IcyLoom ne chevauche un port déjà pris.**

Ports déjà occupés sur cette machine (ne pas les prendre) :
`8080, 8081, 8084, 8765, 8766, 8767, 8770, 8775, 1234, 20128, 20131, 20132, 6333, 6334, 80, 53…`
(8080/8081/8084 = IcyCache ; 8765-8775 = dashboards ; 1234 = LM Studio ; 20128 = omniroute.)

**Bloc réservé à IcyLoom (vérifié libre au 2026-10-26) :**

| Port | Service |
|---|---|
| **8150** | Gateway HTTP OpenAI/Anthropic-compatible (`/v1/*`) — le trafic LLM |
| **8151** | MCP Streamable-HTTP |
| **8152** | MCP SSE |
| **8153** | API d'admin + dashboard |
| *(stdio)* | MCP stdio — aucun port |

Tout autre service éventuel prend un port dans **8154-8159**. Si tu as besoin de plus, étends
**8154-8169**. **Interdiction absolue** d'utiliser 8080-8084, 8090, 8765-8775 sans que ça
n'ait été validé.

---

## 7. DIRECTION D'ARCHITECTURE (contraintes, pas une maquette)

Tu **réfléchis** et tu proposes. Voici ce qui est arrêté, ce qui est ouvert.

### 7.1 Arrêté

- **Un seul serveur MCP** = une seule instance FastMCP qui expose **toutes** les capacités
  (cache + contexte), servie sur **stdio + SSE (8152) + Streamable-HTTP (8151)**.
  → Un client MCP voit un seul serveur, pas deux.
- **Le gateway HTTP (8150) et le serveur MCP sont des processus séparés** (transports
  différents, domaines de panne différents) — mais le **même paquet**, la **même config**,
  la **même base**. Tu peux aussi exposer le gateway en *tool* MCP si c'est plus propre,
  mais Hermes doit pouvoir continuer à parler HTTP.
- **Une base SQLite unique** : un fichier, un schéma unifié, WAL, `busy_timeout` (2 processus
  écrivant), `CREATE TABLE IF NOT EXISTS` idempotent, colonnes ajoutées par `ALTER TABLE`
  (jamais de `DROP`/`RECREATE` qui caserait les données).
- **Une config unique** (YAML), lue par tous les processus. Les règles externalisées
  (`normalization_rules.json` 1122 règles, `tools_registry.json` 100 ancres) restent des
  fichiers à côté, en JSON, **copiés à l'identique** (le hash de cache en dépend).
- **Un ledger unique** : une seule table qui enregistre *tout* ce qui coûte et *tout* ce qui
  économise — appels LLM (tokens réels), compressions (tokens avant → tokens ancre), appels
  d'outils (tokens évités / latence évitée). Une seule vue permet de répondre à
  « combien de tokens m'ont-ils coûté, et combien m'ont-ils fait gagner ? ».
  → **C'est le cœur du produit.** Le seuil des **35 %** doit devenir un chiffre lisible,
  pas un chiffre de benchmark.
- **Le cache et la compression doivent se renforcer mutuellement**, pas coexister :
  - le cache doit pouvoir être **invalidé intelligemment** quand le contexte change
    (le contenu volumineux ne doit pas faire exploser la clé de cache) ;
  - la compression doit être **automatique** dans le chemin de la requête (l'agent ne doit
    pas avoir à y penser) mais **opt-in par profil**, avec `strict` par défaut, et
    **jamais de compression lossy activée d'office** ;
  - un résultat d'outil mis en cache **est** un chunk du tiroir (un seul mécanisme, pas deux).

### 7.2 Ouvert — à toi de trancher, avec justification

- **Surface d'outils finale** : combien d'outils, quels noms, quelles fusions. Contrainte :
  **aucune capacité perdue** (§3.3). Pense au coût en contexte de chaque schéma d'outil
  exposé — 28 outils aujourd'hui chez Hermes, c'est un vrai coût à chaque requête.
- **Granularité du découpage** : comment séparer une requête LLM en « blocs » compressibles.
- **Politique de cache** : quelles clés, quelle invalidation, quel TTL par cas.
- **Stratégie de profil** : comment basculer `strict` / `balanced` / `aggressive` et qui décide.
- **Emplacement exact du ledger** : table à part, ou colonnes ajoutées aux tables existantes.
- **Chiffrement** : SQLCipher sur la base entière, ou Fernet par colonne — arbitrer en
  tenant compte que les deux domaines partagent le fichier.

### 7.3 Interdit

- Chroma, ou tout store vectoriel **obligatoire** (L'index vectoriel est **optionnel**).
- Un nouveau service externe obligatoire (Qdrant est **opt-in**, silent miss si absent).
- Toute dashboard avant que le reste soit vert.
- Tout dashboard comme **premier** livrable.
- Réintroduire une config morte, un code mort, ou un chemin absolu codé en dur.

---

## 8. DÉPENDANCES

**Base (obligatoire)**
```
fastmcp>=2.0            # serveur MCP
mcp>=1.2.0,<2           # ATTENTION: mcp 2.x a renommé FastMCP -> MCPServer
fastapi>=0.115,<1
uvicorn>=0.30
httpx>=0.27,<1
sse-starlette>=2.1
websockets>=12.0        # requis pour /ws/events
pydantic>=2.9
pyyaml>=6.0
pyahocorasick>=2.1      # moteur de normalisation
nltk>=3.8
tiktoken>=0.7
sqlite-utils>=3.37      # helpful pour les migrations
python-dotenv>=1.0
psutil>=6.0
tqdm>=4.66
```
**Optionnel**
```
[semantic]  faiss-cpu>=1.7 , sentence-transformers>=2.2 , numpy>=1.24
[qdrant]    qdrant-client
[crypto]    sqlcipher / cryptography (Fernet)
[dev]       pytest>=8 , pytest-asyncio>=0.23 , ruff>=0.5
```
**Dépendances du squelette `D:\ICY-LOOM` à NE PAS reprendre** (inutiles ou fausses) :
`chromadb`, `sentence-transformers` (en dur), `prometheus-client` (jamais importé),
`sqlite-utils` si non utilisé, `rank-bm25` (le BM25 du squelette était cassé et en O(n²)).

---

## 9. ORDRE DE CONSTRUCTION

Construis **par couches testables**, chaque couche avec sa preuve. N advance pas à la couche
N+1 tant que la N n'est pas verte.

| Couche | Contenu | Preuve de sortie |
|---|---|---|
| **0** | Squelette du paquet `icyloom/`, `paths.py`, config unique + loader Pydantic, CLI (`icyloom serve\|mcp\|gateway\|doctor\|paths\|version\|migrate`), `pyproject.toml` (wheel + sdist + entry points), `.gitignore` | `icyloom doctor` affiche chemins, versions, ports ; `pip install -e .` OK |
| **1** | **Moteur cache** : normalisation (1122 règles, hash **compatible**), store SQLite, routage 18 upstreams, pool résilient, circuit breaker **avec reset**, proxy de préfixe | Les tests de cache d'IcyCache passent (portés ou réécrits) ; le hash de normalisation est **identique** à l'existant sur un corpus de référence ; gateway `/v1/chat/completions` répond |
| **2** | **Moteur contexte** : tiroir + FTS5, anchors + invariants, compression 3 profils, verify, sessions, cross-session, crypto, redact, sandbox, ratelimit, audit, auto-store | La suite Loom passe au moins au niveau actuel (83/19) ; `ctx_store`→`ctx_get` est **byte-exact** ; `ctx_verify` prouve la couverture |
| **3** | **Base unifiée** : schéma unique (tables `llm_events` / `prefix_events` distinctes), WAL + `busy_timeout`, `icyloom migrate` (copie additive, **les bases d'origine restent intactes**) | Comptages avant/après **égaux** pour toutes les tables ; hash des fichiers d'origine inchangé ; pas de `database is locked` avec 2 processus concurrents |
| **4** | **Serveur MCP unifié** : une instance FastMCP, toutes les capacités, transports stdio + SSE(8152) + HTTP(8151), bridge stdout/stderr intact | Handshake sur les 3 transports → **liste complète et cohérente des outils** ; stdout 100 % JSON-RPC ; aucun crash d'encodage sur les accents |
| **5** | **Config unique + outillage** : `icyloom.yaml`, `normalization_rules.json` (1122), `tools_registry.json` (100 + assertion `total == 100`), cache sémantique Qdrant **opt-in**, CLI admin | `icyloom config_get` reflète la config réelle ; l'assertion des 100 ancres passe ; le cache sémantique est **inactif par défaut** |
| **6** | **Le ledger** : une source unique pour les coûts et les gains, exposée via les outils de stats + l'API admin | Le seuil de **35 % de tokens en moins (in + out, tax d'outils incluse)** est **mesurable en continu** sur du trafic réel |
| **7** | **Outils MCP** : surface finale (noms, fusions, docstrings courtes, `ToolAnnotations` partout) + outils d'admin (destructifs isolés derrière une confirmation explicite) | Chaque capacité du §5 est couverte par au moins un outil ; **une table de correspondance capacité → outil**, revue et signée |
| **8** | **Dashboards** : fusion des deux (IcyCache HTML + Loom `dash/`) sur le ledger | — (dernier) |
| **9** | **Migration & cut-over** : `icyloom migrate` depuis `D:\IcyCache\db\*.db` et `~/.loom/loom.db` ; config Hermes : `mcp_servers.icycache` + `mcp_servers.loom` → **une seule** `mcp_servers.icyloom` ; old path conservé en shim pour rollback | Hermes : trafic LLM OK sur le nouveau port, **une seule** entrée MCP qui liste tout, `LOOM_AUTO_STORE_THRESHOLD` toujours honoré ; **rollback** possible en < 1 min (repointer la config) |

---

## 10. CRITÈRES DE VALIDATION

**Fonctionnel**
- [ ] Toutes les capacités A1-A24 et B1-B16 sont présentes, chacune couverte par un outil ou une route identifiée dans une table de correspondance
- [ ] `/v1/chat/completions` (OpenAI) et `/v1/messages` (Anthropic) répondent sur **8150**
- [ ] Routage : préfixe > header > body > défaut, sur 18 upstreams, avec `sticky_upstream`
- [ ] Streaming SSE : assemblage, cache du stream, **rejeu** depuis le cache
- [ ] Normalisation : **1122 règles**, hash **identique** à IcyCache (assertion sur corpus de référence)
- [ ] Cache sémantique : opt-in, **silent miss** si Qdrant/embedder absent
- [ ] Compression : profil `strict` **lossless**, `balanced`/`aggressive` en opt-in explicite
- [ ] `ctx_verify` : couverture 100 % des invariants, aucune perte
- [ ] `ctx_get` : **byte-exact** (jamais tronqué, jamais normalisé)
- [ ] Proxy d'outils : cache, TTL par serveur, garde anti-boucle, **batch**, liste des serveurs/outils
- [ ] Circuit breaker : **reset réel** après le délai configuré
- [ ] Pool résilient : un 400 upstream ne laisse pas la gateway en 502
- [ ] Redaction : aucun secret (`Bearer`, `sk-`, `password=`, `secret=`, `token=`) dans la base
- [ ] Sandbox : `ctx_store_cmd` échoue **closed** si la sandbox est indisponible
- [ ] Registre d'outils : assertion `total == 100` au démarrage

**Non-régression**
- [ ] Aucune régression par rapport aux suites des parents
- [ ] Cache existant : les hits survivent à la migration (hash de clé identiques)
- [ ] 2 processus écrivant la base : aucun `database is locked`

**Économique (le vrai critère)**
- [ ] Le ledger attribue chaque token : entrée, sortie, gain cache, gain compression, gain outil
- [ ] Le seuil des **35 %** est mesurable en continu, en + et en −, tax d'outils incluse
- [ ] Aucune compression lossy activée par défaut
- [ ] Le coût en tokens des **schémas d'outils exposés** est mesuré et ne dépasse pas celui d'aujourd'hui (28 outils chez Hermes aujourd'hui)

**Intégration**
- [ ] Hermes : une seule entrée `mcp_servers.icyloom`, trafic LLM OK, `LOOM_AUTO_STORE_THRESHOLD` honoré
- [ ] `ICYCACHE_CONFIG`, `LOOM_DB`, `LOOM_ALLOWED_DIRS` toujours honorés en fallback
- [ ] Rollback testé : repointer la config vers les anciens serveurs restaure l'état initial

---

## 11. CE QUE J'ATTENDS DE TOI

1. **Une architecture** — structure de modules, surface d'outils finale, schéma de base unifié,
   config unifiée. Diagramme ASCII.
2. **La table de correspondance** : chaque capacité A1-A24 / B1-B16 → l'outil ou la route
   qui la couvre dans IcyLoom. **Aucune ligne vide, aucune capacité « perdue » en route.**
3. **Le plan de construction** en phases avec, pour chaque phase, le périmètre exact des
   fichiers et le test qui valide la phase.
4. **La liste des décisions** où tu as tranché (surface d'outils, granularité du découpage,
   politique de cache, stratégie de profil, chiffrement, emplacement du ledger) **avec la
   raison**, pour que je puisse trancher différemment si besoin.
5. **Le code** de la phase 0 et de la phase 1, exécutable et testé, écrit dans
   `D:\ICY-LOOM`.

Contraintes de livraison : **zéro dépendance inutile**, **zéro code mort**, **zéro config
morte**, **toute constante de configuration lue depuis le fichier de config unique**, aucun
chemin absolu codé en dur, et **chaque décision non évidente commentée dans le code**.

Si un point te semble ambigu ou si tu penses qu'une capacité listée doit être abandonnée,
**dis-le explicitement et justifie** — ne supprime pas silencieusement.
