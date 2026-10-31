"""Justesse du cache d'outils : mesure du taux de faux hits.

Un faux serveur d'outils **avec état** est interrogé par un trafic où lectures
et écritures sont entrelacées. Chaque réponse servie depuis le cache est
comparée à la vérité du serveur à cet instant : un écart est un faux hit.

Mesure seule (sans assertion), utile avant/après une modification :

    python tests/test_cache_correctness.py

Les tests pytest posent ensuite des seuils stricts (zéro faux hit).
La base est toujours temporaire : ``var/icyloom.db`` n'est jamais touchée.
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import build_config  # noqa: E402

from icyloom.toolproxy import ToolProxy  # noqa: E402

SERVER = "notes"


class FakeNotes:
    """Serveur descendant avec état (lectures + écritures)."""

    def __init__(self) -> None:
        self.notes: dict[str, str] = {
            "a": "v0", "b": "v0", "c": "v0",
            "a b": "X", "a  b": "Y",
            "8150": "S", "int:8150": "I",
        }
        self.calls = 0
        self.errors_left = 0

    def answer(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Vérité à l'instant T, sans effet de bord."""
        if tool == "read_note":
            name = args.get("name")
            key = f"int:{name}" if isinstance(name, int) else str(name)
            return {"content": [self.notes.get(key, "absent")], "is_error": False}
        if tool == "list_notes":
            return {"content": [",".join(sorted(self.notes))], "is_error": False}
        return {"content": ["ok"], "is_error": False}

    def invoke(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if tool in ("write_note", "annotate", "remember"):
            self.notes[str(args["name"])] = str(args["text"])
            return {"content": ["ok"], "is_error": False}
        if tool == "read_note" and args.get("name") == "boom" and self.errors_left > 0:
            self.errors_left -= 1
            return {"content": ["upstream failure"], "is_error": True}
        return self.answer(tool, args)


def make_proxy(tmp: Path, cache_tools: dict[str, bool], **tool_proxy: Any):
    """Proxy branché sur le faux serveur ; garde-boucle relâchée par défaut."""
    server = {"name": SERVER, "command": "unused", "cache_enabled": True,
              "ttl_seconds": 3600, "cache_tools": cache_tools}
    settings: dict[str, Any] = {"loop_warn_after": 10**6, "loop_block_after": 10**6}
    settings.update(tool_proxy)
    settings["servers"] = [server]
    proxy = ToolProxy(build_config(tmp, tool_proxy=settings))
    fake = FakeNotes()

    async def _invoke(_cfg: Any, tool: str, args: dict[str, Any]) -> Any:
        return fake.invoke(tool, args)

    proxy._invoke = _invoke  # type: ignore[method-assign]
    # This fixture replaces the upstream invocation with FakeNotes. Seed the
    # catalogue too, so tests never try to spawn the intentionally fake
    # ``unused`` MCP command just to discover an input schema.
    proxy._schemas[SERVER] = {
        tool: {} for tool in ("read_note", "list_notes", "write_note", "annotate", "remember")
    }
    return proxy, fake


async def interleaved(tmp: Path, n: int = 600, seed: int = 7) -> dict[str, Any]:
    """Lectures cachables entrelacées avec des écritures (dont une au nom banal)."""
    proxy, fake = make_proxy(tmp, {"read_note": True, "list_notes": True})
    rng = random.Random(seed)
    names = ["a", "b", "c", "n0", "n1"]
    reads = hits = false_hits = 0
    for i in range(n):
        if rng.random() < 0.25:
            tool = rng.choice(["write_note", "annotate"])
            await proxy.call(SERVER, tool, {"name": rng.choice(names), "text": f"v{i}"})
            continue
        if rng.random() < 0.2:
            tool, args = "list_notes", {}
        else:
            tool, args = "read_note", {"name": rng.choice(names)}
        resp = await proxy.call(SERVER, tool, args)
        reads += 1
        if resp.get("cached"):
            hits += 1
            if resp["result"] != fake.answer(tool, args):
                false_hits += 1
    return {"reads": reads, "hits": hits, "false_hits": false_hits,
            "false_hit_rate": round(false_hits / hits, 4) if hits else 0.0}


async def replayed_writes(tmp: Path) -> dict[str, Any]:
    """Sans allowlist, une écriture au nom banal est-elle rejouée du cache ?"""
    proxy, fake = make_proxy(tmp, {})
    replayed = 0
    for i in range(40):
        resp = await proxy.call(SERVER, "remember", {"name": "xy"[i % 2], "text": "t"})
        replayed += 1 if resp.get("cached") else 0
    return {"writes": 40, "replayed_from_cache": replayed, "upstream_calls": fake.calls}


async def error_replay(tmp: Path) -> dict[str, Any]:
    """Une erreur amont est-elle mémorisée puis rejouée ?"""
    proxy, fake = make_proxy(tmp, {"read_note": True})
    fake.errors_left = 1
    first = await proxy.call(SERVER, "read_note", {"name": "boom"})
    second = await proxy.call(SERVER, "read_note", {"name": "boom"})
    return {"first_is_error": first["result"]["is_error"],
            "second_cached": bool(second.get("cached")),
            "second_is_error": second["result"]["is_error"]}


async def loop_guard(tmp: Path) -> dict[str, Any]:
    """12 lectures identiques, seuils par défaut : combien sont bloquées ?"""
    proxy, _fake = make_proxy(tmp, {"read_note": True},
                              loop_warn_after=3, loop_block_after=5)
    blocked = 0
    for _ in range(12):
        resp = await proxy.call(SERVER, "read_note", {"name": "a"})
        blocked += 1 if resp.get("error") == "loop_blocked" else 0
    return {"calls": 12, "blocked": blocked}


async def canonical_overmerge(tmp: Path) -> dict[str, Any]:
    """Deux arguments réellement différents sont-ils fusionnés à tort ?"""
    proxy, fake = make_proxy(tmp, {"read_note": True})
    pairs = [("a b", "a  b"), ("8150", 8150)]
    wrong = 0
    for first, second in pairs:
        await proxy.call(SERVER, "read_note", {"name": first})
        resp = await proxy.call(SERVER, "read_note", {"name": second})
        if resp["result"] != fake.answer("read_note", {"name": second}):
            wrong += 1
    return {"pairs": len(pairs), "wrong_merges": wrong}


async def batch_stop(tmp: Path) -> dict[str, Any]:
    """Un lot continue-t-il après une erreur ?"""
    proxy, fake = make_proxy(tmp, {"read_note": True})
    fake.errors_left = 1
    await proxy.call_batch([
        {"server": SERVER, "tool": "read_note", "args": {"name": "boom"}},
        {"server": SERVER, "tool": "write_note", "args": {"name": "a", "text": "zz"}},
    ])
    return {"write_executed_after_error": fake.notes["a"] == "zz"}


def measure() -> dict[str, Any]:
    report: dict[str, Any] = {}
    for label, scenario in (
        ("interleaved", interleaved), ("replayed_writes", replayed_writes),
        ("error_replay", error_replay), ("loop_guard", loop_guard),
        ("canonical_overmerge", canonical_overmerge), ("batch_stop", batch_stop),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            report[label] = asyncio.run(scenario(Path(tmp)))
    return report


# -- seuils stricts (pytest) ---------------------------------------------------

def test_no_stale_hits_and_cache_still_useful(tmp_path):
    out = asyncio.run(interleaved(tmp_path))
    assert out["false_hits"] == 0
    assert out["hits"] > 0, "le cache ne doit pas devenir inutile"


def test_unlisted_tools_are_never_replayed(tmp_path):
    out = asyncio.run(replayed_writes(tmp_path))
    assert out["replayed_from_cache"] == 0
    assert out["upstream_calls"] == out["writes"]


def test_errors_are_not_cached(tmp_path):
    out = asyncio.run(error_replay(tmp_path))
    assert out["first_is_error"] is True
    assert out["second_cached"] is False and out["second_is_error"] is False


def test_loop_guard_counts_cache_hits(tmp_path):
    assert asyncio.run(loop_guard(tmp_path))["blocked"] > 0


def test_canonicalisation_never_merges_different_arguments(tmp_path):
    assert asyncio.run(canonical_overmerge(tmp_path))["wrong_merges"] == 0


def test_schema_guided_canonicalisation(tmp_path):
    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        proxy, _fake = make_proxy(tmp_path, {"read_note": True})
        proxy._schemas = {SERVER: {"read_note": {
            "type": "object", "properties": {"name": {"type": "integer"}}}}}
        await proxy.call(SERVER, "read_note", {"name": 8150})
        merged = await proxy.call(SERVER, "read_note", {"name": "8150"})
        proxy._schemas = {SERVER: {"read_note": {
            "type": "object", "properties": {"name": {"type": "string"}}}}}
        await proxy.call(SERVER, "read_note", {"name": "a b"})
        kept = await proxy.call(SERVER, "read_note", {"name": "a  b"})
        return merged, kept

    merged, kept = asyncio.run(run())
    assert merged.get("cached") is True and merged.get("matched") == "canonical"
    assert not kept.get("cached")


def test_batch_stops_at_first_error(tmp_path):
    assert asyncio.run(batch_stop(tmp_path))["write_executed_after_error"] is False


if __name__ == "__main__":
    print(json.dumps(measure(), indent=2, ensure_ascii=False))
