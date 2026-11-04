"""Tests mission 2026-11-04 : bugs 1-5 + option A (pwsh) + catalogue 50."""
from __future__ import annotations

import pytest

from icyloom.config import PolicyConfig
from icyloom.policy import PolicyEngine
from icyloom.registry import definition_hash, normalize_schema
from icyloom.toolproxy import ToolProxy
from tests.conftest import build_config


def _engine(**kw):
    return PolicyEngine(PolicyConfig(default_role="reader", **kw))


def _proxy(tmp_path, **kw):
    cfg = build_config(tmp_path, **kw)
    return ToolProxy(cfg)


# -- BUG 1 : tool inconnu = fail-closed --------------------------------------
def test_unknown_tool_reader_denied(tmp_path):
    proxy = _proxy(tmp_path)
    out = proxy.simulate("filesystem", "delete_file", role="reader")
    if out.get("ok") is False:
        pytest.skip(f"server filesystem non configure dans le test: {out}")
    assert out["side_effects"] == "unknown"
    assert out["known_tool"] is False
    assert out["decision"]["effect"] == "deny"
    assert out["decision"]["reason"] == "unknown_tool"


def test_unknown_tool_writer_approval_when_policy_allows(tmp_path):
    engine = _engine(unknown_tool_policy="approval")
    d = engine.decide(role="writer", server="filesystem", tool="delete_file",
                      side_effects="unknown")
    assert d.effect == "approval"


def test_unknown_tool_writer_denied_by_default(tmp_path):
    engine = _engine()
    d = engine.decide(role="writer", server="filesystem", tool="delete_file",
                      side_effects="unknown")
    assert d.effect == "deny"


# -- BUG 2 : parite simulate/invoke (capabilities avant approval) -----------
def test_reader_destructive_denied_even_unconfirmed():
    engine = _engine()
    d = engine.decide(role="reader", server="pwsh", tool="execute_command",
                      side_effects="destructive", confirmed=False)
    assert d.effect == "deny"
    assert d.reason == "role_forbids_side_effect"


def test_operator_destructive_unconfirmed_still_approval():
    engine = PolicyEngine(PolicyConfig(default_role="operator"))
    d = engine.decide(role="operator", server="pwsh", tool="execute_command",
                      side_effects="destructive", confirmed=False)
    assert d.effect == "approval"


def test_simulate_matches_policy_decide(tmp_path):
    proxy = _proxy(tmp_path)
    out = proxy.simulate("pwsh", "execute_command", role="reader")
    if out.get("ok") is False:
        pytest.skip(f"pwsh non configure: {out}")
    # reader + destructive -> deny meme sans confirmation (bug 2)
    assert out["decision"]["effect"] == "deny"


# -- OPTION A : pwsh execute_command classe par la commande -------------------
def test_pwsh_readonly_command_is_none():
    from icyloom.policy import pwsh_command_effect
    assert pwsh_command_effect("Get-Date") == "none"
    assert pwsh_command_effect("Get-ChildItem D:\\") == "none"
    assert pwsh_command_effect("Test-Path C:\\x") == "none"


def test_pwsh_writing_command_is_destructive():
    from icyloom.policy import pwsh_command_effect
    assert pwsh_command_effect("Remove-Item C:\\x") == "destructive"
    assert pwsh_command_effect("Set-Content a b") == "destructive"


# -- BUG 4 : normalisation avant hash -----------------------------------------
def test_normalize_strips_schema_and_sorts_keys():
    a = {"type": "object", "$schema": "https://json-schema.org/draft/2020-12/schema",
         "properties": {"path": {"type": "string"}}}
    b = {"properties": {"path": {"type": "string"}}, "type": "object",
         "$schema": "http://json-schema.org/draft-07/schema#"}
    assert normalize_schema(a) == normalize_schema(b)
    assert definition_hash("t", "d", a, {}, {}) == definition_hash("t", "d", b, {}, {})


def test_description_change_still_drifts():
    a = {"type": "object", "properties": {}}
    assert definition_hash("t", "desc A", a, {}, {}) != definition_hash("t", "desc B", a, {}, {})


# -- BUG 5 : cache_read/cache_write instrumentes -------------------------------
def test_cache_read_write_counted(tmp_path):
    proxy = _proxy(tmp_path)
    proxy.cache_set("k1", "filesystem", "read_file", {"x": 1}, 5, ttl=60)
    proxy.cache_get("k1", 60)
    summary = proxy.meter.summary()
    assert summary["by_category"]["cache_write"] > 0
    assert summary["by_category"]["cache_read"] > 0


# -- BUG 3 : statut par serveur ------------------------------------------------
def test_server_status_shape(tmp_path):
    proxy = _proxy(tmp_path)
    statuses = proxy.server_statuses()
    assert isinstance(statuses, list)
    for s in statuses:
        assert {"name", "status", "tool_count", "last_error"} <= set(s)


# -- Catalogue : 100 serveurs uniques ------------------------------------------
def test_catalog_has_100_unique_servers():
    import yaml
    raw = yaml.safe_load(open("icyloom.yaml", encoding="utf-8"))
    names = {s["name"] for s in raw["tool_proxy"]["servers"]} | {s["name"] for s in raw["tool_proxy"]["catalog"]}
    assert len(names) == 100, f"catalogue: {len(names)} uniques"


def test_proxy_decide_unknown_tool_fail_closed(tmp_path):
    """_decide() direct : outil absent du registre -> unknown + deny (reader)."""
    proxy = _proxy(tmp_path)
    effects, known, _row = proxy._resolve_effects("srv", "delete_file", {})
    assert effects == "unknown" and known is False
    decision, effects, known, _r = proxy._decide(
        "srv", "delete_file", {}, role="reader")
    assert effects == "unknown" and known is False
    assert decision.effect == "deny" and decision.reason == "unknown_tool"


def test_simulate_parity_with_decide(tmp_path):
    """simulate() et _decide() donnent le même effet (bug 2)."""
    proxy = _proxy(tmp_path)
    proxy._servers["pwsh"] = proxy.config.tool_proxy.servers[0] if proxy.config.tool_proxy.servers else None
    if proxy._servers.get("pwsh") is None:
        pytest.skip("aucun serveur de test")
    out = proxy.simulate("pwsh", "execute_command",
                         args={"command": "Remove-Item dummy"}, role="reader")
    assert out["decision"]["effect"] == "deny"


def test_pwsh_readonly_allowed_for_reader(tmp_path):
    """Option A : commande en lecture seule -> none -> allow pour reader."""
    proxy = _proxy(tmp_path)
    d, effects, known, _r = proxy._decide(
        "pwsh", "execute_command", {"command": "Get-Date"}, role="reader")
    assert effects == "none" and d.effect == "allow"
