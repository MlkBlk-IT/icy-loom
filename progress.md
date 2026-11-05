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

## 9. Second bug, plus grave : l'ancre coûte plus cher que le contenu

### Mesure runtime

```
before  anchor  delta   decision
     1      26    -25   RAW (perte evitee)
    15      40    -25   RAW (perte evitee)
    81     105    -24   RAW (perte evitee)
```

L'ancre est un squelette structurel à taille quasi fixe (~25 tokens).
Sous ~150 tokens de contenu, elle coûte **plus cher** que ce qu'elle résume :
chaque compression **perdait** des tokens.

### En base (42 sources)

```
GAIN  :  2 sources  -> +16 968 tokens
PERTE : 40 sources  ->  -1 803 tokens
  dont 32 compressions label='check' : before=224 / anchor=992 -> -768
```

Le `tokens_saved: 15165` du dashboard n'est qu'un **cumul historique** porté
par 2 gros tests. Tout le reste perdait.

### Pourquoi c'était invisible

`store.py` écrivait `max(0, tokens_before - anchor_tokens)` : une perte de
-24 était enregistrée à **0**. Les metrics ne pouvaient pas bouger.

### Correctif

Dans `store.py` : si `anchor_tokens >= tokens_before`, on sert le **contenu
brut** (gain nul au pire, jamais négatif) en **conservant les invariants**
(ce sont eux qui rendent `verify()`/`coverage` valides). Le ledger enregistre
désormais la valeur **signée**, la perte devient visible.

### Vérification

```
small  before=   1  anchor=   1  delta=    0
medium before=  81  anchor=  81  delta=    0
facts  before=  20  anchor=  20  delta=    0
big    before=2000  anchor= 193  delta=+1807
coverage 1.0  ok True

pytest: 137 passed, 0 failed
```

### Incident de parcours

Première version du correctif : `invariants = []` → `coverage` tombait à 0.0,
`test_verify_reports_full_coverage` échouait. Corrigé en conservant les
invariants.

## 10. Commits

| Commit | Contenu |
|---|---|
| `4cd5617` | fix(gateway): credit upstream cache hits |
| `f25e6f2` | fix(store): no anchor below break-even, signed ledger value |

Les deux sont sur `origin/main`.

## 11. Fichiers modifies

| Fichier | Nature |
|---|---|
| `src/icyloom/gateway.py` | 2 helpers ajoutes, `_proxy_json` et `_proxy_stream` corriges |
| `src/icyloom/context/store.py` | seuil de rentabilite + ledger signe |
| `tests/test_dashboard.py` | assertion self-containment ciblee |
| `progress.md` | ce document |

---

# Session 2026-11-02 — IcyLoom V2 (passerelle d'outils MCP)

Machine : PC portable i7 vPro, 32 Go, Intel Iris Xe (aucun GPU) · Périmètre : `D:\Icy-Loom`
Chaîne : V1 (cache + tiroir + relais) → **V2 (registre, sélection, politique, compression, audit)**

## 1. Objectif

Transformer IcyLoom en **passerelle d'outils MCP** conforme au brief « MCP Tool Gateway » :
registre centralisé des outils, index de sélection, sélection dynamique via meta-tools,
compression des schémas et des résultats, permissions/scoping, sandbox d'exécution,
observabilité et traçabilité. Le tout doit rester installable depuis GitHub **sans dépendance
externe** : pas d'embeddings, pas de GPU, pas de LM Studio.

## 2. Ce qui a été ajouté

| Module | Rôle |
|---|---|
| `src/icyloom/registry.py` | registre `tool_registry` + index FTS5/BM25, inférence intention/effets de bord/sécurité, lexique **12 langues** + bigrammes CJK, score nom > tags > description, plafond par serveur |
| `src/icyloom/policy.py` | rôles `reader`/`writer`/`operator`/`admin`, règles `allow`/`deny`/`approval` (la plus spécifique l'emporte), destructif = approbation obligatoire, portée projet, refus des écritures hors racines |
| `src/icyloom/toolcompress.py` | compression des schémas (**ordre des propriétés conservé**) + des résultats (JSON/log/diff/HTML) + détection de secrets |
| `src/icyloom/audit.py` | table `tool_audit` (acteur, rôle, intention, décision, motif, compression, tokens) |
| `src/icyloom/tokens.py` | compteur par catégorie (`system_prompt`, `tool_schemas`, `tool_results`, `cache_read`, `cache_write`, `llm_calls`) |
| `src/icyloom/script.py` | bac à sable Python (allowlist AST, pas d'import, budget d'instructions via `sys.settrace`, timeout, `max_steps`) |

Modifiés : `db.py` (nouvelles tables + FTS5 optionnel), `config.py` (`PolicyConfig`,
`ToolRegistryConfig`, options `tool_proxy`), `toolproxy.py` (registre/policy/compression/audit/
tokens + transmission des racines), `mcpserver.py` (6 meta-tools), `clientcheck.py` (19 tools,
27 vérifications).

**Meta-tools exposés** : `search_tools`, `get_tool_schema`, `invoke_tool`, `run_tool_script`,
`tool_policy`, `gateway_stats` → **19 tools** au total (13 avant).

## 3. Console — vue « Gateway V2 » refaite

La vue était vide et sans la moindre mesure. Refaite avec :

- **5 cartes KPI** et un badge de réussite par carte (`✓ indexed`, `✓ attached`, `✓ harmless`,
  `✓ 6 stopped`, `✓ compressed`) ;
- **3 graphiques** : barres « tools per server », donut « side effects », barres « where the
  tokens go » ;
- **registre** et **policy in force** en lignes clé/valeur + tables serveurs et rôles ;
- **audit decisions** (qui · quoi · décision · pourquoi · tokens gagnés) ;
- libellés, intentions, décisions et motifs **traduits en anglais** (le reste de la console est
  en anglais : la version française avait été retirée sur demande explicite).

## 4. Trois bugs trouvés par le test, pas par la lecture

| Bug | Symptôme | Cause | Correctif |
|---|---|---|---|
| `.row-line` sans style | les lignes du registre s'affichaient en texte brut | la règle CSS n'existait que sous `.hero-card` | règle `.row-line` globale ajoutée |
| Console vide en API | `/api/dashboard` ne renvoyait **aucun** bloc `gateway` | le serveur tournait depuis le **1er nov.** avec l'ancien code Python | processus `serve` redémarré, bloc `gateway` vérifié à froid |
| **Classement de la recherche** | `supprime un fichier` → `basic_memory_diagnostics` ; `cree un ticket` → `add_issue_comment` | les intentions du registre sont en anglais (`read`, `delete`) et la requête française n'était **jamais traduite** → le bonus d'intention ne se déclenchait pas, score au hasard | `expand_concepts()` + `STOPWORDS` + `FR_FORMS` (formes non infinitives) + pénalité objet dans `registry.py` |

## 5. Test live (serveur MCP stdio, appels réels, aucun mock)

```
TOOLS=19  META_OK=True

lis un fichier du depot  -> github.get_file_contents | desktop-commander.read_file | filesystem.read_file
supprime un fichier      -> github.delete_file | local-rag.delete_file | basic-memory.delete_note
cree un ticket github    -> github.issue_write | memory.create_entities | github.add_issue_comment
liste les fichiers       -> desktop-commander.list_directory | filesystem.list_directory
ecris un fichier         -> desktop-commander.write_file | filesystem.write_file

read_file README.md            -> ok=True, contenu réel retourné
write_file                     -> ok=True, fichier réellement écrit
github.delete_file sans confirm-> error=approval_required, appel jamais arrivé au serveur
run_tool_script                -> ok=True, bac à sable, 1 étape
tool_policy                    -> role=reader env=dev roots=[] (racines declarees par le client)
gateway_stats                  -> 427 289 tokens (422 373 schémas / 4 916 résultats)
```

Un faux positif a été vérifié puis écarté : la lecture de `D:\Icy-Loom\README.md` échouait avec
« path outside allowed directories » — c'était une **coquille dans le test** (racine
`D:/IcyLoom` au lieu de `D:/Icy-Loom`). Corrigée, la lecture passe. L'incident prouve au
passage que le périmètre des racines est réellement appliqué par le serveur descendant.

## 6. Benchmarks mesurés (instance de développement, pile réelle de 13 serveurs)

| Mesure | Valeur |
|---|---|
| Outils indexés | **194** sur **14** serveurs |
| Lecture seule / écriture / destructif | **134** (69,1 %) / **45** (23,2 %) / **15** (7,7 %) |
| Décisions tracées | **58** — 40 autorisées, 17 approbations, 1 refus (hors périmètre projet) |
| Tokens gagnés par la compression d'outils | **118 654** — tokens réels (tiktoken) |
| Tokens mesurés (fenêtre) | **837 431** — 812 407 schémas, 25 024 résultats |
| Tests | **20 tests de mission** (un par constat) + suite complète verte, exit 0 |

### Passage en charge (mesuré)

Charge réelle sur le gateway, même instance, aucun mock — 40 appels, 10 prompts distincts :

| Mesure | Valeur |
|---|---|
| Appels traversant le gateway | **40** — 0 erreur |
| Servis par le cache local IcyLoom | **30** — ~215 ms chacun, 0 token dépensé |
| Générations réelles (miss amont) | **10** — p50 6,2 s, p95 10,4 s |
| Taux de hit (fenêtre console) | **78.5 %** — 102 hits / 28 misses (13 erreurs exclues) |
| Tokens évités | **373 190** — cache 20 250 + compression 234 286 + outils 118 654 |
| Gain sur la fenêtre | **93.7 %** — 373 190 évités / 398 367 payés |
| Cache local après le passage | **8 entrées**, 4 réusages chacune |
| Lignes du ledger | **623** |

Deux faits trouvés par ce passage :

- l'amont (IcyCache) sert depuis **son** cache → IcyLoom ne stockait rien en local et la vue Cache restait vide ; le gateway met maintenant en cache local ce que l'amont a servi ;
- une réponse **tronquée** (`finish_reason: length`) n'est pas mise en cache : le budget `max_tokens` doit laisser la génération se terminer, sinon le cache reste vide et chaque appel repart en amont.

### Console interactive (nouveauté)

Chaque graphique est maintenant interrogeable : **survol** → tooltip avec les valeurs, **clic** → tooltip épinglé (il survit au rafraîchissement de 2 s), **clic sur une légende** → la série s'affiche ou se masque.

## 7. Commits

| Commit | Contenu |
|---|---|
| `f5343af` | V2 gateway : registre, policy, compression, audit, meta-tools |
| `81a3653` | transmission des racines client |
| `b765edf` | versions épinglées des serveurs descendants |
| `62f26e4` | console : vue Gateway V2 |
| `4684a07` | console : refonte KPI + graphiques + badges de réussite |
| `e341606` | console : traduction du motif de refus hors périmètre |
| `2e6595f` | registre : traduction des requêtes en concepts canoniques |

## 8. Reste à faire

- **Release cette semaine** : version taguée + notes de publication (`ANNOUNCEMENT.md` prêt).
- Test manuel dans les clients (Claude Desktop, MCode, Hermes) avant toute bascule :
  « liste les outils que tu as » → « lis-moi D:\Icy-Loom\README.md » → « supprime le fichier X »
  (refus attendu).
- **Ne rien retirer** des configs clients tant que le test manuel n'est pas concluant.

## 9. Note honnête

Le classement de la recherche est **lexical** : il compare des mots, pas du sens. Le passage en
français vers l'anglais est géré par un lexique explicite (12 langues), pas par une traduction
libre — donc « lit le fichier » et « read the file » aboutissent, mais une formulation très
libre peut encore manquer. Les embeddings restent volontairement exclus (installation sans
dépendance externe) ; ils pourront revenir en option plus tard.

## 10. Session 2026-11-04 — signaux de confiance (V2.1.0)

### Constat

La spécification MCP (`2026-07-28`) traite la **description d'un outil** et le **contenu de son
résultat** comme des entrées non fiables, et normalise ce qu'un serveur déclare de lui-même
(`annotations`). IcyLoom classait les effets de bord par heuristique sur le nom
(`infer_side_effects`), ignorait les `outputSchema` déclarés, et servait les résultats tels
quels. C'est le point faible d'un relais qui se veut « gouverné ».

Cinq améliorations ont été ajoutées — sans dépendance externe, sans GPU, sans réseau :

| # | Amélioration | Où |
|---|---|---|
| 1 | Annotations MCP lues (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) : elles complètent la classification d'effets de bord | `registry.effective_side_effects`, `toolproxy._annotations_of` |
| 2 | Empreinte de définition (SHA) + table `tool_drift` : un changement de description, de schéma ou d'annotations est journalisé (champ, avant, après) | `registry.definition_hash`, `registry.drift` |
| 3 | `structuredContent` vérifié contre l'`outputSchema` déclaré : plus de rupture silencieuse du serveur amont | `icyloom/schema_check.py` |
| 4 | Résultats analysés : instruction override, role hijack, fuite de credentials, faux tool call, markup caché | `icyloom/untrusted.py` |
| 5 | Deux meta-tools : `tool_drift` (dérive) et `tool_simulate` (décision de policy **sans exécution**) — 21 outils au total | `mcpserver.py` |

### Règle de confiance retenue

Le mode par défaut des annotations est `raise` : une déclaration de serveur peut **escalader**
l'effet de bord — un outil au nom anodin qui se déclare destructeur est traité comme destructeur —
mais **jamais l'abaisser**. Le mode `authoritative`
(`tool_registry.annotations_trust`) fait confiance au serveur : à réserver à un serveur maîtrisé.

Les trois signaux ne bloquent jamais un appel. Ils **signalent** (`schema_valid`,
`schema_errors`, `untrusted`) et laissent la décision à l'agent : un relais qui bloque un
résultat légitime sur une heuristique de mot-clé ferait perdre plus de temps qu'il n'en économise.

### Suite de tests

`pytest` : **232 tests, 0 échec**.

Deux compteurs d'outils codés en dur (19) ont été mis à jour (21) dans `clientcheck.py` et
`tests/test_mcp_transports.py` — l'auto-test client vérifie le nombre d'outils exposés.

Le serveur `everything` (`mcp-server-everything`, **absent de PyPI**) a été remis en
`enabled: false` dans `icyloom.yaml` : réactivé, il était effectivement lancé par `uvx` à chaque
rechargement du catalogue et faisait échouer les trois tests « mission A1/A3 ».

### Console

Le panneau **Trust signals** de la vue Gateway V2 affiche ce que le gateway sait des serveurs
qu'il relaie : les annotations déclarées, la dérive de définition (`tool_drift`), et les
résultats signalés (contenu non fiable, résultat structuré non conforme). Les compteurs sont
alimentés par les événements de relais (`tool_untrusted`, `tool_schema_invalid`).

### Version

`2.0.0` → **`2.1.0`**. La version mineure est justifiée par la surface publique : deux
meta-tools (`tool_drift`, `tool_simulate`) et un nouveau panneau de console.

## 11. Session 2026-11-04 — identité visuelle de la console (logo)

### Ce qui a été demandé

Le logo était **introuvable** dans la console : il n'existait que comme favicon
(`/assets/logo.svg`) et comme glyphe de 38 px dans la barre latérale. Le hero de
la vue Overview ne montrait que le motto.

### Ce qui a été fait

| Emplacement | Avant | Après |
|---|---|---|
| Hero de l'Overview | *(rien)* | `<img class="hero-mark">` **132 px**, à gauche du motto, incliné à −2,5° avec une ombre portée suivant la forme (filtre `drop-shadow`, pas de `box-shadow` carré) |
| Barre latérale (`.brand-mark`) | 38 px | 46 px |
| Favicon / onglet | `logo.svg` | inchangé |

```html
<div class="hero-brand">
  <img class="hero-mark" src="/assets/logo.svg" width="132" height="132" alt="IcyLoom">
  <div>
    <span class="sticker gold">✦ IcyLoom · unified console</span>
    <h1 id="hero-title">ONE <span class="hl">LEDGER</span>,<br>EVERY CALL.</h1>
  </div>
</div>
```

`hero-brand` est un `flex` avec `flex-wrap`, donc le logo passe au-dessus du
titre sur écran étroit (media query 760 px → 84 px).

### Identité du logo — ce qui a été écarté

Trois variantes ont été produites et **refusées** : glassmorphism / cartoon,
extrusion 3D pleine, et relief « topo » fil de fer. Retenu : **le logo original
intact, avec une palette plus bleue** — warp `#2F6BE8`, weft `#7A34C9`, pastille
bleue. Le SVG est la source unique ; `logo.png` (512×512 RGBA), `logo.ico`
(16→256) et `docs/logo.png` en sont dérivés.

Le `CHANGELOG` décrivait encore l'extrusion 3D (variante refusée) : la note a
été réécrite pour décrire la palette bleue réellement livrée.

### Vérification

- Rendu contrôlé en Chrome headless (1440 px) : logo présent dans le hero, à
  la bonne taille, sans déformation.
- La console **qui tourne** (`:8153/dashboard`) sert déjà le nouveau balisage
  (`SERVED hero-mark = True`) — un simple rafraîchissement de l'onglet suffit.
- Suite complète : **232 tests, 0 échec** (`PYTEST_EXIT=0`).

---

# progress.md — Session 2026-11-04 · Métrique d'économie LIVE (console V2.2.0)

Contexte : la barre d'outils de la vue Overview portait un compteur figé (`143`).
Objectif de Malik : une métrique d'économie **live**, qui bouge, vraie et vérifiable.

## 1. Ce qui bloquait

| Problème | Constat |
|---|---|
| Route absente | `GET /api/live` → **404** : le serveur en service servait du code antérieur (jamais redémarré) |
| Console morte | Un artefact de corruption (`</parameter>`) dans le `<script>` inline de `dashboard.html` cassait tout le JS : « connecting... », graphes vides, registry en tirets |
| Doublon de processus | Deux `serve` actifs (venv `pythonw` → enfant `Python312`) ; seul l'enfant tenait le port 8153 |

## 2. La métrique honnête

Formulation littérale : `live = SUM(ledger.tokens_saved) + (context.tokens_before − context.anchor_tokens)`.
`ledger_entries` contient déjà 154 lignes `context_compress` (236 089 tok) qui sont les
**mêmes** événements que les 154 lignes de `context_sources` (234 286 tok) : la compression
était comptée **deux fois**. Résultat : 491 296 sur la barre d'outils contre 373 190 sur la
carte Registry — deux chiffres pour la même chose, à l'écran, en même temps.

Formulation retenue — **domaines disjoints** :

| Domaine | Source | Tokens |
|---|---|---|
| Cache LLM | `llm_events` (`kind='hit'` → `saved_prompt+saved_completion`) | 20 250 |
| Compression | `context_sources` (`tokens_before − anchor_tokens`) | 234 286 |
| Outils | `tool_audit` (`decision='allow'`, `tokens_saved>0`, `saved_chars IS NOT NULL`) | 118 654 |
| Tool-cache | `ledger_entries.tool_tokens_saved` | 671 |
| **Total** | | **373 861** |

Ratio = 373 861 / (373 861 + 27 721 réellement payés) = **93,1 %** — même base que la carte
Registry (« Baseline 400 911 »). La ligne de legacy `tool_audit` en caractères reste
exclue : elle mesure des caractères, pas des tokens.

## 3. Patchs

- `src/icyloom/admin.py` — `/api/live` recalcule par domaines disjoints et expose
  `economy_ratio`, `baseline`, `paid_total` et `domains` (le détail par domaine).
- `src/icyloom/static/dashboard.html` — le poll snapshot n'écrit **plus** la headline
  (le nombre alternait toutes les 2 s entre deux valeurs : `poll` écrasait `pollLive`) ;
  la headline lit `economy_ratio` ; le tooltip détaille les 4 domaines.
- `icyloom.yaml` — `qdrant` est épinglé (`mcp-server-qdrant==0.8.1`, version résolue
  présente dans le cache uv local) : `test_A1_uvx_servers_are_pinned` était **rouge
  depuis le commit a666ced** — `main` était déjà rouge.

## 4. Preuves

- `GET /api/live` → 200 : `total_economy=373861`, `economy_ratio=0.931`, `baseline=401582`,
  `paid_total=27721`, `domains={cache:20250, compression:234286, tools:118654, tool_cache:671}`.
- SQL rejoué à la main : les 4 domaines redonnent exactement 373 861.
- Console (navigateur) : headline `💰 373,861 tok économisés · 93.1%`, ticker
  `✚ 373,861` (convergence easing vérifiée), horloge `⏱ mm:ss` qui tick, registry
  peuplée, **0 erreur console**.
- `node --check` sur le JS inline → exit 0 ; `py_compile admin.py` → exit 0.
- Suite complète : **245 passed, 3 skipped, 0 failed** (248 collectés, exit 0).
  Le seul échec (`test_A1_uvx_servers_are_pinned`, `qdrant` non épinglé) était
  **antérieur** à cette session : il venait du commit `a666ced`, donc `main` était
  déjà rouge. Corrigé par l'épinglage `mcp-server-qdrant==0.8.1` (version résolue
  retrouvée dans le cache uv local, sans appel réseau).

## 5. Ce qui reste

- **Release v2.2.0** (tag + publication) prévue vendredi : rien n'est taggé à ce jour, et
  le profil GitHub pointait vers un release `v2.2.0` inexistante — le lien du profil pointe
  donc le CHANGELOG jusqu'à la publication.
- Écart connu : les lignes `ledger_entries.kind='token_tool_results'` portent `tokens_saved=0`
  alors que `tool_audit` mesure 118 654 tok réels → le ledger ne porte pas l'économie
  de compaction des résultats d'outils.
