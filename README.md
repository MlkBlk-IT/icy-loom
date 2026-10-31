# IcyLoom

A local MCP server that combines an LLM cache, a context drawer and a tool relay behind one configuration file and one SQLite database.

## Status

Active development. The project is local-first and binds to `127.0.0.1` by default. Check the test suite and the status section in the repository history for the latest verified numbers.

## Why it exists

IcyLoom keeps useful context close to the tools that need it without hiding what happened. It records cache hits, upstream usage and avoided work in a single ledger.

## Main pieces

- **Cache** — normalises, hashes and stores requests.
- **Context drawer** — stores sources, chunks and anchors for later retrieval.
- **Tool relay** — forwards downstream MCP tools with explicit, deny-by-default rules.
- **Ledger** — separates tokens paid upstream from tokens avoided by a cache hit.
- **Console** — provides local views for the registry, activity and measurements.
- **Privacy** — local binding by default; no credentials belong in the repository.

## Quick start

```powershell
git clone https://github.com/MlkBlk-IT/icy-loom.git
cd icy-loom
python -m pip install -r requirements.txt
python -m icyloom
```

The repository also contains client setup helpers and the current test commands. Start with the files in `docs/` and the project configuration before connecting an external MCP client.

## Important measurement note

Demo dashboards may contain synthetic data. A demo number is not a production measurement. When evaluating the cache, use the ledger and the project's verification commands.

## Repository map

- `src/` — server and application code
- `tests/` — automated tests
- `tools/` — setup, verification and demo helpers
- `docs/` — design and operating notes

## Author

Built by [Malik Belkacemi](https://github.com/MlkBlk-IT).
