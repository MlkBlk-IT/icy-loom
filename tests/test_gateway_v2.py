"""V2 : registre centralisé, sélection lexicale, politique, compression, audit.

Ces tests ne touchent **aucun réseau** : le registre est alimenté à la main
et la politique est évaluée avant toute connexion à un serveur descendant.
"""

from __future__ import annotations

import asyncio
import json

from conftest import build_config

from icyloom.audit import AuditTrail
from icyloom.config import PolicyConfig, PolicyRule
from icyloom.db import Database
from icyloom.policy import PolicyEngine
from icyloom.registry import ToolRegistry
from icyloom.toolcompress import compress_result, compress_schema, detect_sensitive
from icyloom.toolproxy import ToolProxy
from icyloom.tokens import TokenMeter

GITHUB_TOOLS = [
    {"name": "create_issue", "description": "Create a new issue in a repository"},
    {"name": "search_issues", "description": "Search issues by keyword"},
]
GITHUB_SCHEMAS = {
    "create_issue": {
        "title": "CreateIssue",
        "type": "object",
        "description": "Cree un ticket dans le depot. " + "detail " * 60,
        "properties": {
            "repo": {"type": "string", "description": "Repository name"},
            "title": {"type": "string", "description": "Issue title"},
        },
        "required": ["repo"],
        "examples": [{"repo": "owner/name"}],
    },
    "search_issues": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Keyword"}},
        "required": ["query"],
    },
}
FS_TOOLS = [
    {"name": "read_file", "description": "Read the content of a file"},
    {"name": "write_file", "description": "Write content to a file"},
    {"name": "run_command", "description": "Run a shell command"},
]
FS_SCHEMAS = {
    "read_file": {"type": "object", "properties": {"path": {"type": "string"}}},
    "write_file": {"type": "object", "properties": {"path": {"type": "string"}}},
    "run_command": {"type": "object", "properties": {"cmd": {"type": "string"}}},
}


def _registry(config) -> ToolRegistry:
    db = Database(config)
    db.ensure_schema()
    registry = ToolRegistry(db)
    registry.register("github", GITHUB_TOOLS, GITHUB_SCHEMAS)
    registry.register("fs", FS_TOOLS, FS_SCHEMAS)
    return registry


def test_registering_twice_does_not_duplicate(config):
    """Ré-enregistrer un serveur ne doit pas dupliquer l'index FTS."""
    registry = _registry(config)
    before = registry.search("creer un ticket")
    registry.register("github", GITHUB_TOOLS, GITHUB_SCHEMAS)
    after = registry.search("creer un ticket")
    assert len(after) == len({(hit["server"], hit["tool"]) for hit in after})
    assert len(after) == len(before)


def test_registry_indexes_every_tool(config):
    stats = _registry(config).stats()
    assert stats["tools"] == 5
    assert stats["by_side_effects"]["destructive"] == 1
    assert stats["by_side_effects"]["write"] == 2
    assert stats["by_side_effects"]["none"] == 2


def test_search_ranks_by_intent(config):
    registry = _registry(config)
    hits = registry.search("lire un fichier")
    assert hits, "la recherche doit remonter des outils"
    assert hits[0]["name"] == "fs.read_file"


def test_search_bridges_french_intent_to_english_tool(config):
    registry = _registry(config)
    hits = registry.search("creer un ticket")
    assert hits[0]["name"] == "github.create_issue"
    assert hits[0]["side_effects"] == "write"


def test_search_bridges_twelve_languages(config):
    """La sélection ne doit pas dépendre de la langue de la requête."""
    registry = _registry(config)
    queries = (
        "create an issue",        # en
        "creer un ticket",        # fr
        "crear una incidencia",   # es
        "erstelle einen issue",   # de
        "creare un issue",        # it
        "criar uma issue",        # pt
        "maak een issue",         # nl
        "utworz issue",           # pl
        "создать issue",          # ru
        "olustur issue",          # tr
        "创建 issue",              # zh
        "issue を作成",            # ja
    )
    for query in queries:
        hits = registry.search(query)
        assert hits, f"aucun résultat pour {query!r}"
        assert hits[0]["name"] == "github.create_issue", \
            f"{query!r} a donné {hits[0]['name']}"


def test_search_can_filter_on_side_effects(config):
    registry = _registry(config)
    hits = registry.search("fichier", side_effects="none")
    assert hits and all(hit["side_effects"] == "none" for hit in hits)


def test_contract_is_compressed_and_keeps_argument_order(config):
    registry = _registry(config)
    contract = registry.contract("github", "create_issue")
    assert contract is not None
    args = contract["arguments"]
    assert "title" not in args
    assert "examples" not in args
    assert list(args["properties"]) == ["repo", "title"]
    assert contract["side_effects"] == "write"
    assert contract["required"] == ["repo"]


def test_schema_compression_shortens_descriptions():
    schema = {"type": "object", "description": "x" * 500,
              "properties": {"a": {"type": "string", "description": "y" * 300}}}
    out = compress_schema(schema, 40)
    assert len(out["description"]) <= 41
    assert len(out["properties"]["a"]["description"]) <= 41
    assert list(out["properties"]["a"]) == ["type", "description"]


def test_result_compression_summarizes_json():
    payload = {"content": [json.dumps([{"i": i, "v": "x" * 50} for i in range(200)])]}
    text, meta = compress_result(payload, max_chars=500)
    assert meta["truncated"] is True
    assert meta["kind"] == "json"
    assert len(text) < len(payload["content"][0])


def test_result_compression_keeps_log_head_and_tail():
    payload = {"content": ["\n".join(f"line {i}" for i in range(500))]}
    text, meta = compress_result(payload, max_chars=600)
    assert meta["kind"] == "log"
    assert "line 0" in text and "line 499" in text


def test_result_compression_strips_html():
    payload = {"content": ["<html><body>" + "<p>hello</p>" * 100 + "</body></html>"]}
    text, meta = compress_result(payload, max_chars=200)
    assert meta["kind"] == "html"
    assert "<p>" not in text


def test_detect_sensitive_flags_secrets():
    assert "github_token" in detect_sensitive("ghp_" + "a" * 30)
    assert "password" in detect_sensitive("password: hunter2")


def test_destructive_tool_requires_approval():
    engine = PolicyEngine(PolicyConfig(default_role="operator"))
    pending = engine.decide(role="operator", server="github", tool="delete_issue",
                            side_effects="destructive")
    assert pending.effect == "approval"
    granted = engine.decide(role="operator", server="github", tool="delete_issue",
                             side_effects="destructive", confirmed=True)
    assert granted.effect == "allow"


def test_reader_role_cannot_write():
    engine = PolicyEngine(PolicyConfig(default_role="reader"))
    decision = engine.decide(role="reader", server="github", tool="create_issue",
                             side_effects="write")
    assert decision.effect == "deny"


def test_explicit_deny_rule_wins():
    engine = PolicyEngine(PolicyConfig(
        default_role="operator",
        roles={"operator": [PolicyRule(server="fs", tool="write_file",
                                      effect="deny")]},
    ))
    decision = engine.decide(role="operator", server="fs", tool="write_file",
                             side_effects="write")
    assert decision.effect == "deny"


def test_write_outside_project_root_is_refused(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    engine = PolicyEngine(PolicyConfig(default_role="operator",
                                       project_roots=[str(root)]))
    decision = engine.decide(role="operator", server="fs", tool="write_file",
                             side_effects="write",
                             args={"path": str(tmp_path / "outside.txt")})
    assert decision.effect == "deny"
    assert decision.reason == "outside_project_scope"


def test_audit_trail_and_token_meter(config):
    db = Database(config)
    db.ensure_schema()
    audit = AuditTrail(db)
    audit.record(actor="agent", role="operator", intent="read", server="github",
                 tool="read_file", decision="allow", reason="role_allows",
                 tokens_in=120, tokens_saved=40, latency_ms=12)
    tail = audit.tail()
    assert tail and tail[0]["tool"] == "read_file"
    assert audit.stats()["by_decision"]["allow"] == 1

    meter = TokenMeter(db)
    meter.record("tool_results", 100, server="github")
    meter.record("tool_schemas", 40, server="github")
    summary = meter.summary()
    assert summary["by_category"]["tool_results"] == 100
    assert summary["by_category"]["tool_schemas"] == 40


def _proxy_with_github(tmp_path) -> ToolProxy:
    config = build_config(tmp_path, tool_proxy={"servers": [
        {"name": "github", "command": "npx", "args": ["server-github"],
         "enabled": True},
    ]})
    proxy = ToolProxy(config)
    # schéma déjà connu : la politique est évaluée avant toute connexion.
    proxy._schemas["github"] = {"delete_issue": {}}
    proxy.registry.register(
        "github", [{"name": "delete_issue", "description": "Delete an issue"}],
        {"delete_issue": {}},
    )
    return proxy


def test_proxy_call_refuses_destructive_tool(tmp_path):
    proxy = _proxy_with_github(tmp_path)
    outcome = asyncio.run(proxy.call("github", "delete_issue", {}))
    assert outcome["ok"] is False
    assert outcome["error"] == "approval_required"


def test_proxy_call_allows_destructive_tool_once_confirmed(tmp_path):
    proxy = _proxy_with_github(tmp_path)
    outcome = asyncio.run(proxy.call("github", "delete_issue", {}, confirmed=True))
    # l'appel passe la politique, puis échoue sur la connexion absente :
    # ce qui compte ici, c'est de ne pas avoir été refusé par la politique.
    assert outcome.get("error") != "approval_required"


def test_sandbox_script_is_read_only_by_default(tmp_path):
    proxy = _proxy_with_github(tmp_path)
    script = "r = await call('github', 'delete_issue', {})\nreturn r"
    outcome = asyncio.run(proxy.run_script(script))
    assert outcome["ok"] is True
    assert outcome["result"]["error"] == "read_only_script"
