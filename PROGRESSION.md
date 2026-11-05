# IcyLoom — Progression

Session log. Each entry: what was asked, what was found (with evidence), what
changed, and how it was verified. No entry without a real test run.

---

## §1 — 2026-11-04 — Mission 6 bugs : fail-closed, parité simulate/invoke, statuts, drift, métriques, catalogue 50 (v2.1.0 → v2.2.0)

**Asked.** Fix 6 bugs in `D:\icy-loom`, targeted deltas, no rewrite. Red→green
per bug (targeted test only during the switch, full suite at the end).
Baseline known: pytest green, ~100 s, 0 failures.

**Found (read-only analysis first).**

1. **Fail-open on unknown tool.** `tool_simulate(filesystem, delete_file, reader)`
   returned `allow` with `side_effects="none"`, although `delete_file` is not
   in the registry — and the audit showed `filesystem.delete_file` ×4 and
   `github.delete_file` ×13 actually called. Root cause:
   `str((row or {}).get("side_effects") or "none")` — missing row → `"none"` → allow.
2. **`tool_simulate != invoke_tool`.** `simulate(pwsh.execute_command, reader)`
   → `approval`; real `invoke_tool(confirm:true)` → `deny`. Root cause:
   `PolicyEngine.decide()` put the destructive-approval step (3) before the
   role-capability step (5), and `simulate()` is always non-confirmed.
3. **brave-search dead + invisible.** `invoke` → `McpError: Connection closed`,
   0 tools in the registry, while `proxy_info(servers)` listed it. Root cause:
   no `env:` block for `brave-search` in `icyloom.yaml` → no `BRAVE_API_KEY` →
   the process dies at startup. No key anywhere in the repo (verified by grep).
4. **Drift = noise (1 817 entries).** Only `$schema` dialect flips and key order
   changed; one real change (filesystem.get_file_info description). Root cause:
   `definition_hash()` included `$schema` verbatim and `_record_drift()` compared
   raw column strings.
5. **Metrics at 0.** `cache_read`/`cache_write`/`llm_calls` = 0 in
   `stats.tokens.by_category`; `tokens_saved` had no verifiable formula.
6. **Error rate.** `tool_error` 135 vs `tool_call` 58 (+ `tool_blocked` 17).
   Top 5 (grouped from the audit DB, before fixing): everything/list_tools 50,
   sqlite/list_tools 40, brave-search/list_tools 21, basic-memory/list_tools 13,
   filesystem/list_tools 4. Root cause: `refresh_catalog` kept the server list
   frozen at startup, so disabled servers kept being polled in an error loop.

**Changed.**

- `config.py`: new `PolicyConfig.unknown_tool_policy: deny|approval` (default `deny`).
- `policy.py`: `unknown` side effects treated as potentially destructive
  (deny by default, approval if configured and the role can write); role
  capabilities checked **before** destructive approval; new
  `pwsh_command_effect()` — `pwsh.execute_command` classified by the **command**
  (option A): read-only verbs (`Get-*`, `Test-*`, `Select-*`…) without `;` → `none`, else `destructive`.
- `registry.py`: new `normalize_schema()` (strips `$schema` recursively), used by
  `definition_hash()` and by `_record_drift()` (normalized comparison).
- `toolproxy.py`: single `_decide()` point used by `call()`, `simulate()` and
  `run_script`; `_resolve_effects()` returns `("unknown", False)` for a tool
  absent from the registry **and** never published by the server (a published
  but unregistered tool is a sync gap, not an unknown tool); `server_statuses()`
  (`up`/`down`/`no-tools` + `tool_count` + `last_error`); `_last_error` fed by
  `list_tools` and `call`; clearer error messages; `cache_get`/`cache_set`
  really record `cache_read`/`cache_write`.
- `mcpserver.py`: `proxy_info(servers)` returns the real statuses;
  `refresh_catalog` re-reads `tools.server_names()` every cycle (fallback to
  `active_servers()` for test doubles).
- `admin.py`: `/api/tools/catalog` exposes `status`, `tool_count`, `last_error`.
- `static/dashboard.html`: catalog cards show the real badge
  (actif / en panne / sans outils / dormant), tool count and last error in red.
- `icyloom.yaml`: catalog 30 → **50 unique servers** (20 new dormant entries).
  Backup: `icyloom.yaml.bak-20261104-170909`.
- DB (one-shot, approved): 910 cosmetic `tool_drift` rows purged (1 881 → 971);
  the 971 kept are real changes.

**Verified (real output).**

- New `tests/test_mission_20251104.py`: RED before the fix
  (`ModuleNotFoundError: normalize_schema`, 5 FAILED), then green.
- Full suite at the end: **245 passed, 3 skipped, 0 failed** (exit 0). The 3
  skips are server-dependent tests in the isolated test config; the logic is
  covered by direct `_decide()` tests.
- One regression caught and fixed during the run: `test_cache_correctness`
  (fake server tools now pass through the "published by the server" path) and
  `FakeProxy` compatibility in `refresh_catalog`.

**Left open.**

- `brave-search` still needs a real `BRAVE_API_KEY` supplied outside the repo
  (never committed) to be activated.
- The 20 new catalog entries are dormant; their commands are plausible but
  untested at runtime — validate each one when enabling it.
- `llm_calls` stays 0 by construction (no LLM call in the proxy) — field kept
  for compatibility.

---

## §2 — 2026-11-05 — 3 fixes runtime + tests rouge→vert + ledger compaction

**Asked.** (a) Lancer la suite complète et corriger toute régression.
(b) Ajouter les tests rouge→vert des 3 fixes de la session précédente
(ledger.daily, flux coupé, migrate mapping). (c) Ledger : les lignes
`token_tool_results` portaient `tokens_saved=0` alors que `tool_audit` mesure
118 654 tok réels de compaction.

**Found (read-only first).**

1. **Suite pytest.** `245 passed, 3 skipped, 0 failed` (exit 0). Aucune
   régression par les 3 fixes précédents.
2. **`tokens.py:TokenMeter.record()`** ne passait jamais `tokens_saved` au
   ledger — `LedgerEntry(tokens_in=tokens, meta=meta)` sans `tokens_saved`.
   La compaction était mesurée dans `tool_audit.tokens_saved` mais invisible
   côté ledger.
3. **`toolproxy.py:_present()`** appelait `meter.record("tool_results", ...)`
   sans `saved=`, donc `tokens_saved=0` sur chaque ligne `token_tool_results`.
4. **`/api/live`** calcule `total_economy` depuis 4 domaines disjoints
   (`llm_events` / `context_sources` / `tool_audit` / `ledger.tool_tokens_saved`).
   Le `tokens_saved` ajouté sur `token_tool_results` n'entre dans aucun de ces
   4 domaines → `total_economy` inchangé. Seul `tok_saved` (affichage
   secondaire) devient plus précis. Pas de double comptage.

**Changed.**

- `tests/test_fixes_20251105.py` (nouveau, 3 tests) :
  - `test_ledger_daily_groups_by_date` : 2 jours → `daily(7)` groupe par
    `date(ts)`, pas de doublons, ordre croissant, totaux corrects.
  - `test_stream_breaker_on_disconnect` : serveur TCP brut qui envoie un chunk
    SSE puis ferme (chunked tronqué) → HTTP 200, ≥1 event `error` stream=1,
    0 `cache_entries`, breaker.failures ≥ 1.
  - `test_migrate_dict_mapping` : base source avec `prompt_hash`/`normalized_prompt`/
    `original_prompt` → après `run_migration`, `cache_entries` peuplée dans
    `key`/`normalized`/`original`, 5/5 lignes, relance idempotente.
- `tokens.py` : `record(category, tokens, *, saved=0, **meta)` →
  `LedgerEntry(tokens_saved=int(saved))` + fallback SQL `tokens_saved`.
- `toolproxy.py` : `meter.record("tool_results", ..., saved=int(meta.get("saved_tokens", 0)), ...)`.
- `CHANGELOG.md` : sous-section `### Fixed (session 2 — 2026-11-05)`.
- `PROGRESSION.md` : ce §.

**Verified (real output).**

- `pytest tests/test_fixes_20251105.py -q` : 3 passed (après correction).
- Suite complète relancée après les patches source : (voir ci-dessous).

**Left open.**

- Le test flux coupé utilise `RemoteProtocolError` (chunked tronqué), pas un
  vrai `ReadTimeout` — limite connue documentée.
- `pydantic_core` cassé dans le venv → `ContextStore` toujours inutilisable.
- `migrate` ne couvre pas les chunks Loom (`context_chunks`).
- Aucun commit (attente du go utilisateur).
