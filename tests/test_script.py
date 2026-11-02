"""Bac à sable d'exécution des scripts d'outils.

``pytest-asyncio`` n'est pas installé dans le venv du dépôt : les tests
restent synchrones et pilotent la coroutine via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

from icyloom.script import run_script


class _Recorder:
    """Faux ``call`` : mémorise les appels, renvoie un résultat compact."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.fail = fail

    async def __call__(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((server, tool, args))
        if self.fail is not None:
            raise self.fail
        return {"ok": True, "tool": tool, "echo": args}


def _run(coro: Any) -> dict[str, Any]:
    """Exécute la coroutine du bac à sable hors boucle événementielle."""
    return asyncio.run(coro)


def test_script_calls_two_tools_and_returns_a_dict() -> None:
    rec = _Recorder()
    source = """
a = await call("srv", "list", {"n": 1})
b = await call("srv", "fetch", {"n": 2})
return {"a": a["tool"], "b": b["tool"]}
"""
    out = _run(run_script(source, rec))

    assert out["ok"] is True
    assert out["steps"] == 2
    assert out["result"] == {"a": "list", "b": "fetch"}
    assert out["printed"] == []
    assert [c[1] for c in rec.calls] == ["list", "fetch"]


def test_script_without_return_yields_none() -> None:
    out = _run(run_script("x = 1", _Recorder()))

    assert out["ok"] is True
    assert out["result"] is None
    assert out["steps"] == 0


def test_bounded_for_loop_is_allowed() -> None:
    rec = _Recorder()
    source = """
total = 0
for i in range(3):
    total = total + i
    await call("srv", "tick", {"i": i})
return total
"""
    out = _run(run_script(source, rec))

    assert out["ok"] is True
    assert out["result"] == 3
    assert out["steps"] == 3


@pytest.mark.parametrize("source", [
    "import os\nreturn os.getcwd()",
    "from os import path\nreturn path",
    "while True:\n    pass",
    'open("x")',
    "await other_tool()",
    "x = ().__class__",
    "x = globals()",
    "for i in items:\n    pass",
    "for i in range(10**9):\n    pass",
    "def f():\n    return 1",
    "with open('x') as f:\n    pass",
])
def test_forbidden_syntax_is_refused(source: str) -> None:
    out = _run(run_script(source, _Recorder()))

    assert out["ok"] is False
    assert out["error"] == "forbidden_syntax"


def test_syntax_error_is_reported() -> None:
    out = _run(run_script("def (:\n    pass", _Recorder()))

    assert out["ok"] is False
    assert out["error"] == "syntax_error"


def test_empty_script_is_reported() -> None:
    out = _run(run_script("   \n# rien à faire\n", _Recorder()))

    assert out["ok"] is False
    assert out["error"] == "empty_script"


def test_step_limit_stops_the_script() -> None:
    source = """
for i in range(10):
    await call("srv", "tick", {"i": i})
"""
    out = _run(run_script(source, _Recorder(), max_steps=3))

    assert out["ok"] is False
    assert out["error"] == "step_limit"


def test_instruction_budget_stops_the_script() -> None:
    source = """
total = 0
for i in range(1000):
    total = total + i
return total
"""
    out = _run(run_script(source, _Recorder(), max_instructions=50))

    assert out["ok"] is False
    assert out["error"] == "budget_exceeded"


def test_timeout_stops_a_slow_call() -> None:
    async def slow(server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(5)
        return {}

    out = _run(run_script('return await call("srv", "slow", {})', slow, timeout=0.05))

    assert out["ok"] is False
    assert out["error"] == "timeout"


def test_error_raised_by_the_tool_is_a_runtime_error() -> None:
    rec = _Recorder(fail=RuntimeError("outil cassé"))
    out = _run(run_script('return await call("srv", "boom", {})', rec))

    assert out["ok"] is False
    assert out["error"] == "runtime_error"
    assert "outil cassé" in out["message"]


@pytest.mark.parametrize("source", [
    'return await call("", "tool", {})',
    'return await call("srv", "", {})',
])
def test_empty_server_or_tool_is_refused(source: str) -> None:
    out = _run(run_script(source, _Recorder()))

    assert out["ok"] is False
    assert out["error"] == "runtime_error"


def test_tracer_is_restored_after_a_failure() -> None:
    source = """
total = 0
for i in range(1000):
    total = total + i
"""
    before = sys.gettrace()
    out = _run(run_script(source, _Recorder(), max_instructions=50))

    assert out["error"] == "budget_exceeded"
    assert sys.gettrace() is before


def test_print_is_captured_and_stdout_stays_clean(capsys: pytest.CaptureFixture[str]) -> None:
    source = """
print("hello", 42)
print("second")
await call("srv", "tick", {})
"""
    out = _run(run_script(source, _Recorder()))
    captured = capsys.readouterr()

    assert out["ok"] is True
    assert out["printed"] == ["hello 42", "second"]
    assert captured.out == ""
