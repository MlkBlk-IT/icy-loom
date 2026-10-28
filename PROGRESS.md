# PROGRESS - IcyLoom

Last updated: 2026-10-27.

This file records what is **proven**, what is **not**, and what is next. Every number below comes from a run we did; anything carried over from earlier notes or from outside sources is labelled as such.

## 0. Summary

| Area | Status |
|---|---|
| Tool-result cache correctness (stale hits, replayed writes, cached errors, loop detection) | **Done and verified** (section 2) |
| Latency | **No problem**: gateway p95 is about 16 ms against a 200 ms budget |
| Token savings on tool calls | **Not delivered, not measured.** The tool cache saves latency, and the tool ledger records 0 tokens saved on a hit |
| Context-compression prototype (outside the repo) | Large estimated savings, **answer quality not proven**, not integrated (section 5) |

No `.git` on purpose: the licence is still undecided, so the project is not published and has no repository yet. The safety net is a folder copy: `D:\_audit\backup-20261027\`.

## 1. Tool cache correctness pass (2026-10-27) - DONE

Changes in `src/icyloom/toolproxy.py`, plus one comment in `config.py` and `fetch: true` added to `icyloom.yaml`:

1. **Deny by default.** Only tools set to `true` in a server's `cache_tools` are cached. The name-based guess ("does it look like a write?") is gone.
2. **State epoch per server.** Any call that is not cacheable (a possible write) advances the server's epoch. The epoch is part of the cache key, so reads cached before a write are never served after it.
3. **Errors are never cached.** An upstream result with `is_error` is not stored.
4. **Schema-guided canonicalisation.** Near-identical calls are matched only according to the tool's `inputSchema` (integer, number, boolean, path-like string parameters). No schema means exact match only. Never: case changes, whitespace folding inside strings, list reordering.
5. **The loop guard counts cache hits.** A loop served from the cache is still a loop.
6. **A batch stops at the first error.** Remaining calls are reported as `skipped_after_error`.

Behaviour changes to know about:
- The cache no longer survives a relay restart (the epoch carries a boot token), and old `tool_cache` rows can no longer be served.
- The 5th identical call within the loop window is blocked, even when answered from the cache.
- Canonicalisation only starts working after `list_tools` has loaded the schemas.
- A server with `cache_enabled: true` and no `cache_tools` now caches nothing.

## 2. Evidence (2026-10-27)

**Correctness harness** (`tests/test_cache_correctness.py`): a stateful fake tool server, interleaved reads and writes (25 percent writes, seed 7, 600 operations), temporary database. Every cached answer is compared with the server's true state at that moment.

| Scenario | Before | After |
|---|---|---|
| Stale answers served from cache | 425 of 436 hits | 0 of 140 hits |
| Writes replayed from cache (no allowlist) | 38 of 40 | 0 |
| Upstream error replayed | yes | no |
| Blocked by the loop guard (12 identical calls) | 0 | 8 |
| Wrong merges in canonicalisation | 2 of 2 | 0 |
| Batch keeps going after an error | yes | no |

The drop from 436 to 140 hits is expected: almost all removed hits were stale. This traffic is synthetic and write-heavy, so 97 percent stale is a worst case, not a real-world rate.

**Full test suite:** all 102 tests pass.

**End-to-end** (`tests/proof_e2e.py`, real stack on ports 8150-8154, 200 requests per series):

| Series | Before (p50 / p95) | After (p50 / p95) |
|---|---|---|
| direct (fake upstream) | 1.3 / 1.9 ms | 1.2 / 1.9 ms |
| gateway MISS | 13.0 / 16.1 ms | 13.5 / 15.9 ms |
| gateway HIT | 10.5 / 12.5 ms | 10.7 / 12.4 ms |

The differences are measurement noise. All end-to-end proofs pass (4 services answer, the 13 tools are identical over HTTP and SSE).

**Real MCP server** (`tests/real_mcp_fetch.py`, the real `mcp-server-fetch`, 8 of 8 checks pass):
- the tool's `inputSchema` is captured (`url`, `max_length`, `start_index`, `raw`);
- first call 1527 ms, identical second call 24 ms with the same content;
- `max_length` as `"5000"` merges with `5000` because the schema declares an integer;
- a different URL is never merged;
- a real error (unknown host) is returned as an error and never replayed.

`var/icyloom.db` was not modified by any of these runs (last write 2026-10-26 20:08).

## 3. What is NOT proven

- **Token savings on tool calls.** The "50 percent tokens saved" printed by `proof_e2e.py` is true by construction: the test sends every request twice. It says nothing about real traffic. In the tool path, a cache hit is ledgered with `tool_tokens_saved=0`.
- The correctness harness is synthetic. "0 stale hits" proves our design against our fake server, not against real servers whose state changes outside the relay (the TTL, 1 hour by default, is the only protection there).
- The state epoch is not exercised against a real server: `fetch` has no write tool. A real test needs a server with both reads and writes (for example `github`).
- The real fetch test used one static page.
- The HIT path performs several SQLite writes (about 10 ms). It was left alone on purpose: latency is not a problem.

## 4. Token-savings research (external, 2026-10-27, NOT verified on our traffic)

Findings from public sources, to be treated as hypotheses:
- In one vendor's corpus of about 13.8k real agent sessions, tool results were roughly 60 percent of all tokens, tool calls about 18 percent.
- Rewriting history invalidates the provider's cached prefix from the edit onward (cached reads cost about 0.1x, writes 1.25x to 2x). To pay off, a rewrite must remove about 8 percent of the content with one turn left, 46 percent with ten, 90 percent with a hundred. The safe saving is to not admit the tokens in the first place.
- Anthropic offers server-side clearing of old tool results (Anthropic models only) with a minimum-tokens-to-clear setting, plus native tool search and programmatic tool calling.
- Published savings range from about 22 to 27 percent (a 10-turn test with real API calls) up to 84 percent (a reported 100-turn web-search benchmark). 50 percent is plausible for long tool-heavy sessions and unrealistic for short ones.

## 5. Context-compression prototype (carried over from the 2026-10-26 notes, not re-verified today)

Status: prototype outside the repo (`D:\_audit\proto`), not integrated, `gateway.py` not modified.

**Code audit (by reading):**
- `gateway.py` imports nothing from `context/`. `_handle_llm` computes the cache key on the raw body, so without the prototype there is no compression.
- `config.py` `auto_store_threshold` was read nowhere before the prototype. `CompressionConfig.enabled` is false by default.
- `context/compress.py` `build_anchor` is deterministic: header, invariants (max 40), and an excerpt of the first 800 characters. The anchor holds no chunk reference.
- `drawer.store_source` does no deduplication: source and chunk ids change on every call.
- The gateway cache uses one slot per key shared by JSON and streaming (deduced from code, not tested alone).
- "strict = lossless" only holds for the invariants, not for the text the model sees.

**Prototype:** an ASGI wrapper around `create_app` that replaces large earlier tool results and large earlier user text with a content-hash marker, deduplicates by content, and rehydrates on the way back (JSON only, not streaming). If an anchor is missing, the response is delivered with an `X-Ctx-Warning` header. Protected, never touched: tool definitions, tool config, system instruction, generation config, function calls, thought signatures, the last turn. Offline smoke test: 12 of 12 real checks pass (test 13 is a dummy, ignore it); 14004 to 597 tokens; protected fields intact; deterministic; byte-exact rehydration.

**Real Gemini test (gemini-3.5-flash-lite):** 20 of 20 requests returned 200, no errors, no 429.
- Real prompt tokens: 6451 to 1060 (-84 percent) and 13241 to 1069 (-92 percent).
- Estimated totals: baseline 122,991 tokens, paid 23,079, compression saving 92,479, cache saving 7,433, combined 99,912 (81 percent). These are **estimates** (tiktoken cl100k, which underestimates Gemini).
- Latency: 2644-2814 ms without compression, about 868 ms on a compressed miss, 27-85 ms on a hit (one unexplained outlier at 1709 ms).
- Verified: 0 stale hits, cache replay returns the same answer, a real thought signature was accepted when replayed.
- **Answer quality is NOT proven.** Every `ok_answer` was false because of a test bug (forced tool config made the model call the tool again). The only readable answer was wrong: the model said "E1000" instead of "E7778", because the single FATAL line in the middle of the log was lost by the anchor.

**LM Studio test: interrupted at 12 of 25** (model qwen3.5-9b, 8192 context, CPU only). Cause: an upstream `httpx.ReadTimeout` during streaming, not caught in `gateway.py` around line 578 (`async for raw in response.aiter_bytes()`). This is an existing gateway bug, independent of the prototype. Partial results: compression -73 to -81 percent, cache hits 47-85 ms, 0 stale hits. The three baselines without compression all failed with 502 after about 240 s, so no quality comparison exists.

## 6. Open issues

1. **Quality (central):** the anchor drops important lines (for example the FATAL line in the middle of a log). Compression saves tokens by losing information; this must be solved before any compression is enabled.
2. Gateway cache: one slot shared by JSON and streaming (a hit was missed).
3. Ledger: the compression saving is counted twice (once at store, once per request).
4. Rehydration does not work for streaming.
5. Unexplained 1709 ms hit latency outlier.
6. LM Studio baselines time out at 240 s: raise the timeout or use shorter logs.
7. Gemini test must be fixed (auto tool config, full answers, history baselines).
8. Gateway streaming `ReadTimeout` is not caught (around line 578 of `gateway.py`).
9. Token accounting for the tool path is missing (0 recorded on hits).

## 7. Next steps (not started)

1. Measure first: read a **copy** of the real ledger, read-only, to see how much tool output is actually repeated and which tools are slow.
2. If the measurement supports it, in this order, one change at a time with before/after numbers:
   - **Cap oversized tool results at the entry point** (head and tail plus a handle to fetch the rest). Nothing already sent is rewritten, so the provider cache is not touched.
   - **Short reference for re-reads** of a result the model already received in the same conversation, falling back to the full text after some turns in case the client truncated the history.
   - **Native tool-result clearing for Anthropic upstreams**, after checking the exact parameter names in the official documentation.
3. Deliberately dropped: tool aliases and `head=N` derivation. They save latency, not tokens, and carry the highest risk of wrong answers.
4. Porting the compression prototype into IcyLoom needs explicit approval, and a `gateway.py.bak` copy first.
