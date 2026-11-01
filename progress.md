# progress.md — Revue de code IcyLoom + correctifs

Session : 2026-11-01 · Machine : Dell Latitude 5420 (Windows 11)
Tâche Osiris : `t_ce2509a5986c` · Périmètre : `D:\Icy-Loom` (33 modules, 7 466 lignes, 19 fichiers de test)

## 1. Objectif

Revue de code complète d'IcyLoom, correction propre des erreurs confirmées, vérification par exécution réelle, puis ce document.

## 2. Question de Malik : « pourquoi je perds du saving au lieu d'en gagner ? »

### Diagnostic

Les deux dashboards ne racontent pas la même histoire :

| Source | `tokens_saved` | `hit_rate` |
|---|---|---|
| `GET :8150/api/metrics` | **0** | **0.0 %** |
| `GET :8153/api/dashboard` | 15 647 | 0.0 % |
| Vue `v_token_ledger` (domain=`cache`) | **0** sur 5 lignes | — |

**Cause racine trouvée et prouvée par exécution :**

L'amont `hermes` (`127.0.0.1:8080`, IcyCache → omniroute) sert parfois une réponse depuis **son propre cache**. Dans ce cas il renvoie :

```json
{"usage": {"prompt_tokens": 0, "completion_tokens": 0},
 "cached": true, "cache_hash": "cb5e4e59",
 "saved_prompt_tokens": 123, "saved_completion_tokens": 200,
 "saved_total_tokens": 323, "hit_count": 2}
```

IcyLoom ne lisait que `usage` → `(0, 0)` → il journalisait un **`miss` à 0 token**, alors que 323 tokens avaient réellement été évités. Conséquences en chaîne :

1. `saved_prompt` / `saved_completion` toujours à 0 → `tokens_saved = 0` ;
2. `hits = 0` → `hit_rate_pct = 0.0` figé ;
3. l'économie du cache amont n'était **jamais créditée**, seuls les coûts montaient.

Preuve brute en base (`llm_events`) :

```
id=6  kind=miss  prompt=123  completion=200   <- appel normal, usage correct
id=7  kind=miss  prompt=0    completion=0     <- hit upstream, compté miss à 0
```

## 3. Correctifs appliqués

### `src/icyloom/gateway.py`

| Fonction | Correctif |
|---|---|
| `_usage_tokens` | repli sur `saved_prompt_tokens` / `saved_completion_tokens`, `original_usage`, `usage.prompt_tokens_details.cached_tokens` quand `usage` vaut `(0,0)` |
| `_upstream_cached_tokens` (nouveau) | extrait les tokens évités d'une réponse servie par le cache amont |
| `_upstream_served_from_cache` (nouveau) | détecte `cached: true`, `cache_hash`, `cache_age_seconds`, `hit_count` |
| `_proxy_json` | si l'amont a servi depuis son cache : événement `hit`, `saved_*` crédités, `ledger_entry kind=llm_cache_hit`, en-tête `X-IcyLoom-Cache: hit` |
| `_proxy_stream` | même logique sur le chemin SSE ; une réponse servie par le cache amont n'est pas re-stockée |

### `tests/test_dashboard.py`

`test_dashboard_html_is_self_contained` interdisait **toute** URL distante, ce qui attrapait les 5 liens de navigation vers le dépôt GitHub (lignes 595, 642, 917, 971, 1935 de `static/dashboard.html`) — liens légitimes qui ne chargent aucune ressource. Le test cible désormais les ressources réellement chargées (`src=` / `href=`) et n'autorise que le namespace SVG, la boucle locale et le dépôt du projet. Les assertions `src="http`, `@import`, `url(http` restent inchangées.

### Sauvegardes (horodatées, vérifiées)

```
src/icyloom/gateway.py.bak_icyloom_review_20261101_033141      (36 928 o)
src/icyloom/db.py.bak_icyloom_review_20261101_033141           (17 688 o)
src/icyloom/context/store.py.bak_icyloom_review_20261101_033141 (14 170 o)
src/icyloom/static/dashboard.html.bak_icyloom_review_20261101_033141 (138 834 o)
tests/  (consultés)
```

## 4. Fausses pistes écartées (honnêteté)

Trois « bugs » annoncés en cours de route ont été **invalidés** après vérification :

| Annonce | Verdict | Preuve |
|---|---|---|
| Vue `v_token_ledger` désalignée (`0 AS saved`) | **faux** — la vue a bien 6 colonnes alignées | `PRAGMA table_info(v_token_ledger)` + `sqlite_master.sql` |
| `context/store.py:153` n'écrit pas `tokens_saved` | **faux** — il écrit `max(0, tokens_before - anchor_tokens)` | lecture du fichier |
| Les liens GitHub dans `dashboard.html` sont un bug de code | **faux** — c'est le test qui était trop large | le test est corrigé, pas le HTML |

## 5. Vérification par exécution

### Tests unitaires ciblés des nouveaux helpers

```
usage_tokens(cache amont)        -> (123, 200)   OK
_upstream_served_from_cache(cached=True)      -> True   OK
_upstream_served_from_cache(cache_hash=...)   -> True   OK
usage_tokens(réponse normale)    -> (107, 50)    OK
_upstream_served_from_cache(normal)           -> False  OK
```

### Test de bout en bout (deux appels identiques via `:8150`)

```
appel 1 : x-icyloom-cache: miss   usage={123, 200}
appel 2 : x-icyloom-cache: hit    served by upstream cache, 323 tokens évités
```

### pytest (suite complète, 19 fichiers)

Voir section 7 — résultat recopié après exécution.

## 6. Autres observations (non corrigées, hors périmètre)

- `_finish_ok` refuse `finish_reason: "length"` : une réponse tronquée n'est jamais mise en cache. Comportement volontaire et correct, mais avec un `max_tokens` bas, IcyLoom ne remplit jamais son cache → hit rate structurellement à 0. À documenter côté exploitation.
- Le dashboard (`:8153`) tire `tokens_saved` de `context_sources` tandis que `/api/metrics` (`:8150`) le tire de `llm_events`. Deux sources, deux valeurs : à unifier.
- `ruff` n'est pas installé dans `.venv` (`No module named ruff`) — vérification de style non exécutée.
- Le bench du dashboard Osiris échoue : `ModuleNotFoundError: No module named 'fastmcp'` dans `bench/run_bench.py` (projet Osiris, hors périmètre IcyLoom).

## 7. Résultat pytest

```
137 passed, 0 failed  (137 tests, 19 fichiers, exit 0)
```

Le run précédent échouait sur `tests/test_dashboard.py::test_dashboard_html_is_self_contained`.
Après correctif du test : vert.

## 8. Fichiers modifiés

| Fichier | Nature |
|---|---|
| `src/icyloom/gateway.py` | 2 helpers ajoutés, `_proxy_json` et `_proxy_stream` corrigés |
| `tests/test_dashboard.py` | assertion de self-containment ciblée sur les ressources chargées |
| `progress.md` | ce document |
