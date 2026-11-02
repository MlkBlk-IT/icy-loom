# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) — versioning:
[SemVer](https://semver.org/spec/v2.0.0.html).

## [2.0.0] — 2026-11-02

IcyLoom becomes a **policy-aware MCP tool gateway**. The client no longer sees
every downstream tool: it sees a small set of meta-tools, and the proxy
decides what to expose, what to allow, and what to compress.

### Added

- **Central tool registry** (`tool_registry`): one catalogue for every
  downstream MCP tool — qualified name, description, input/output schema,
  domain, intent, tags, required inputs, side effects, security level,
  scope, owner, risk, call count and measured latency.
- **Lexical selection index** (SQLite FTS5/BM25, with a Python fallback when
  FTS5 is missing): `search_tools` returns the tools ranked by intent, with
  a per-server cap so one server cannot take over the list. Synonyms are
  carried by the index itself, so a French intent ("creer un ticket")
  reaches an English-named tool (`create_issue`). **No embeddings and no
  external service**: the app stays installable from GitHub.
- **Dynamic tool selection**: new meta-tools `search_tools`,
  `get_tool_schema`, `invoke_tool`, `run_tool_script`, `tool_policy` and
  `gateway_stats`.
- **Tool contracts**: the agent sees a compressed schema (short
  descriptions, no `title`/`examples`); the full schema stays in the
  registry and is re-injected at call time. Property order is preserved, so
  the prompt cache still hits.
- **Permission engine** (`tool_policy`): roles, project scope, environment
  and risk level. A destructive tool always requires an explicit approval;
  a write outside the configured project roots is refused.
- **Execution sandbox** (`run_tool_script`): a restricted Python subset
  (AST allowlist, no imports, no network, instruction budget, timeout) that
  chains several tool calls in a single turn. Read-only by default.
- **Result compression**: oversized results (JSON, logs, diffs, HTML) are
  summarised before entering the history; the raw payload stays retrievable
  through its reference.
- **Policy audit trail** (`tool_audit`): actor, role, intent, tool,
  arguments hash, decision and reason, what was compressed, final result.
- **Token accounting per category**: system prompt, tool schemas, tool
  results, cache reads, cache writes, LLM calls.

### Changed

- Every tool call now goes through the policy engine and the result
  compressor. The default role is `operator` (read + write); destructive
  tools still require `confirm=true`.
- `tool_proxy.search` now ranks through the registry instead of a substring
  match on names and descriptions; the in-memory catalogue remains as a
  fallback for servers whose catalogue has not been loaded yet.

## [1.0.1] — 2026-11-01

Correctness fixes to the cache expiry, the tool-cache TTL and the token
accounting, plus a stampede guard and a safe configuration reload. No
breaking change, no schema migration required.

### Fixed

- **The cache never expired (silent).** `CacheStore._expired` built its SQL
  with a parameterised **table name** (`FROM ?`), which SQLite always rejects
  with `OperationalError`; the `except` swallowed it, so every entry lived
  forever. The age is now computed in Python (`datetime.strptime` on the
  stored timestamp). An unreadable timestamp is treated as **expired**
  (fail-closed: never serve a result whose age cannot be proven) instead of
  "not expired", and the anomaly is logged instead of being dropped.
- **Tool-cache rows recorded the wrong TTL.** `cache_set` stored the global
  `default_ttl_seconds` even when a per-server `ttl_seconds` was the value
  actually applied on read (`_ttl(server)`). The stored TTL now matches the
  enforced one, so any maintenance performed from the table is truthful.
- **`tool_tokens_saved` was hard-coded to 0 on every tool-cache hit.** The
  ledger published "0 tokens saved" whatever the payload size, so the
  tool cache looked like a pure latency win. The hit now measures the tokens
  of the cached payload with the local counter (`tiktoken` when installed,
  estimator otherwise) and records the counter name and byte size in the
  entry metadata.
- **A first entry created by one caller was recomputed by its N twins.**
  Identical concurrent non-streaming requests all missed and all paid the
  upstream. A single-flight gate now lets the first request produce while the
  others await its result and re-read the cache (one upstream call for N
  simultaneous identical requests). Streaming requests are deliberately left
  out: the consumer must see the bytes as they arrive.
- **A configuration change was applied by overwriting the file.** Writing the
  tool registry now keeps a timestamped `.bak` copy first, reloads the
  catalog from disk, and reports the backup path and the active servers, so a
  bad edit is always reversible.

### Changed

- Version `1.0.0` → `1.0.1`.
- README: the tool path now documents the measured token saving, and the
  "no measured token savings on tool calls" limitation is replaced by the
  real constraint — the counter is **local** (exact with `tiktoken`,
  estimated otherwise), never the upstream's own count.

## [1.0.0] — 2026-10-31

First public release. Everything below was verified by an executed command on
Windows and by the CI matrix (Ubuntu + Windows, Python 3.11 / 3.12 / 3.13).

### Added

- **Installable package**: `pip install` from a wheel or from the GitHub
  release. The normalisation rules and the tool registry now ship **inside the
  package** (`src/icyloom/data/`) and are read through `importlib.resources`.
- **User data directory**: outside a repository checkout, the configuration,
  the SQLite database and the logs are written to `%LOCALAPPDATA%\icyloom`
  (Windows) or `$XDG_DATA_HOME/icyloom` / `~/.local/share/icyloom` (Linux,
  macOS) — never inside `site-packages`. Override with `ICYLOOM_DATA_DIR`.
- **Self-sufficient default configuration**: `icyloom init` writes a
  configuration with no upstream and no tool server. LM Studio, Node, `uvx`
  and any paid API are optional, never required.
- **`doctor` separates warnings from failures**: a missing upstream or a
  missing tool executable is a warning; a missing configuration, unreadable
  data, an unreadable database or a corrupt tool registry are still failures.
- **Automated proofs** (`tests/test_proofs.py`):
  - cache round trip — first request misses and calls the upstream, the
    identical second request is served from the cache, the response is
    identical and the ledger records both;
  - tool relay — a real MCP stdio server is reached, answers, and its result
    is served from the cache on the second identical call;
  - installation — `icyloom serve` followed by `icyloom check --transport all`
    returns 21/21.
- **CI** (`.github/workflows/ci.yml`): test matrix, wheel build + `twine
  check` + wheel install outside the repository, and a job that runs the
  README quickstart command for command.
- `TESTING.md`: what a tester should check and how to report.

### Fixed

- `icyloom check` used the hardcoded ports 8151/8152. The HTTP and SSE
  endpoints are now read from the configuration.
- `icyloom check` endpoints never dial a wildcard host (`0.0.0.0`, `::`).

### Changed

- The internal design documents moved to `docs/archive/` (nothing deleted).
- `pyproject.toml`: real author, project URLs, operating-system and Python
  classifiers, `py.typed` marker.

### Known limitations

- Context compression stays **off by default**: `ctx_verify` proves the
  invariants survive a round trip, but answer quality is not proven.
- Token savings on *tool* calls are not measured: a tool-cache hit is
  ledgered with 0 tokens saved.
- The semantic (vector) cache stays opt-in and needs an external vector store.
- Python 3.14 is **not** in the supported range yet and is excluded from CI.
