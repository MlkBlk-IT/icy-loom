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
