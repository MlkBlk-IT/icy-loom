# Cache sémantique (niveau L2)

Cache LLM à deux niveaux pour Icy-Loom :

- **L1 (exact)** — `cache_entries` : clé = hash canonique du corps de requête. Touché à l'identique, réponse identique.
- **L2 (sémantique)** — `semantic_cache` : clé = vecteur d'embedding. Touché quand la requête a **le même sens mais d'autres mots**.

## Fonctionnement

1. Requête entrante → clé exacte L1 calculée (`canonicalizer.digest`).
2. Miss L1 → si `cache.semantic_enabled` est vrai et requête non-streamée :
   - calcul d'un embedding via LM Studio (`text-embedding-qwen3-embedding-4b`, 2560 dim, normalisé L2) ;
   - recherche linéaire dans `semantic_cache` (cosine = produit scalaire de vecteurs normalisés) ;
   - si `score >= semantic_threshold` → **hit L2**, la réponse mémorisée est servie, `access_count` incrémenté.
3. Miss L2 → requête relayée à l'upstream, puis la réponse est persistée dans `cache_entries` **et** indexée dans `semantic_cache`.

## Configuration (`icyloom.yaml`)

```yaml
cache:
  semantic_enabled: true                                    # active le L2
  semantic_threshold: 0.95                                  # seuil cosine (0..1)
  semantic_url: 'http://127.0.0.1:1234/v1'                   # LM Studio OpenAI-compatible
  semantic_provider: ''                                     # ex: lmstudio (Info)
  semantic_collection: 'icyloom'                             # etiquette
```

Champs disponibles dans `CacheConfig` (src/icyloom/config.py).

## API

`GET /api/status` renvoie désormais :

```json
{
  "cache":    { "entries": 0, "hits": 0 },
  "semantic": { "enabled": true, "entries": 0, "reuses": 0, "threshold": 0.95, "dim": 2560 },
  "upstreams": ["..."]
}
```

## Modèle de données

Table `semantic_cache` (sqlite, `var/icyloom.db`) :

- `key` (PK) — clé canonique associée
- `canon_key` — texte canonique normalisé (200 car. max)
- `provider`, `model`, `is_stream`
- `response` — réponse LLM sérialisée
- `embedding` (BLOB float32), `dim` (int)
- `prompt_tokens`, `completion_tokens`
- `last_accessed`, `access_count`

## Dépendances

**Aucune.** Implémentation stdlib pure (`array`, `math`, `urllib`). Pas de numpy, pas de Qdrant requis pour le tier L2.

## Performances

Recherche linéaire plafonnée à `max_scan` (2000) entrées, triées par `last_accessed DESC`. Suffisant pour un cache local ; à remplacer par un index ANN si la table dépasse ~50k vecteurs.

## Modules

- `src/icyloom/semantic.py` — `EmbeddingClient` + `SemanticIndex`
- `src/icyloom/db.py` — `SCHEMA` + `ensure_schema()` (migration idempotente)
- `src/icyloom/gateway.py` — hooks init / lookup / insert
