# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) — versioning:
[SemVer](https://semver.org/spec/v2.0.0.html).

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
- Python 3.14 is probed in CI but is **not** in the supported range yet.
