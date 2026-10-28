"""Tests du moteur de contexte (couche 2)."""

from __future__ import annotations

import time

import pytest

from icyloom.context.anchors import extract
from icyloom.context.compress import Profile, build_anchor
from icyloom.context.redact import contains_secret, redact_text
from icyloom.context.sandbox import CommandSandbox, SandboxUnavailable
from icyloom.context.verify import verify_anchor, verify_roundtrip


# --------------------------------------------------------------------------
# invariants
# --------------------------------------------------------------------------
def test_invariants_cover_ports_paths_versions_dates() -> None:
    text = ("Déployer le service sur le port 8151 depuis D:/Projets/Service, "
            "version 3.12.10, le 2026-10-26, empreinte 9f86d081884c7d659a2feaa0c55ad015. "
            "L'API ne doit jamais écrire dans la base.")
    kinds = {inv.kind for inv in extract(text)}
    assert "port" in kinds, kinds
    assert "path" in kinds, kinds
    assert "version" in kinds, kinds
    assert "date" in kinds, kinds
    assert "hash" in kinds, kinds
    assert "constraint" in kinds, kinds


def test_port_is_not_confused_with_number() -> None:
    invariants = extract("le port 8151 est ouvert")
    ports = [i.value for i in invariants if i.kind == "port"]
    assert "8151" in ports


def test_date_has_its_own_kind() -> None:
    invariants = extract("le 2026-10-26 à 12:30")
    dates = [i.value for i in invariants if i.kind == "date"]
    assert dates, "une date doit être un invariant de kind 'date'"


def test_strict_profile_keeps_every_invariant() -> None:
    """Le profil strict ne doit jamais écarter un invariant (bug corrigé)."""
    text = ("port 8151, chemin D:/a/b, version 1.2.3, date 2026-10-26, "
            "empreinte abcdef1234567890abcdef1234567890abcdef12, "
            "contrainte : ne jamais écrire, nombre 42")
    invariants = extract(text, max_invariants=40)
    anchor, kept = build_anchor(text, Profile.resolve("strict"))
    result = verify_anchor(kept, anchor)
    assert result.ok, result.missing
    assert result.total == len(invariants)


def test_anchor_verification_detects_loss() -> None:
    invariants = extract("port 8151 et version 1.2.3")
    broken = verify_anchor(invariants, "ancre vide")
    assert not broken.ok
    assert broken.missing


def test_aggressive_profile_is_flagged_lossy() -> None:
    profile = Profile.resolve("aggressive")
    assert profile.lossy
    anchor, _ = build_anchor("port 8151", profile)
    assert "LOSSY" in anchor


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------
@pytest.mark.parametrize("secret", [
    "Authorization: Bearer abcdef1234567890",
    "clé sk-abcdefghijklmnop1234",
    "password=super-secret",
    "api_key: 1234567890",
    "ghp_abcdefghijklmnopqrstuvwxyz0123",
])
def test_secrets_are_masked(secret: str) -> None:
    redacted, count = redact_text(f"voici {secret} fin")
    assert count >= 1
    assert not contains_secret(redacted), redacted


def test_ordinary_text_is_untouched() -> None:
    text = "Le service écoute sur le port 8151 et répond en 42 ms."
    redacted, count = redact_text(text)
    assert count == 0
    assert redacted == text


# --------------------------------------------------------------------------
# sandbox (fail-closed)
# --------------------------------------------------------------------------
def test_sandbox_refuses_without_allowed_dirs() -> None:
    sandbox = CommandSandbox(allowed_dirs=[], enabled=True)
    with pytest.raises(SandboxUnavailable):
        sandbox.run("echo hello")


def test_sandbox_refuses_when_disabled() -> None:
    sandbox = CommandSandbox(allowed_dirs=[], enabled=False)
    with pytest.raises(SandboxUnavailable):
        sandbox.run("echo hello")


def test_sandbox_allows_configured_directory(tmp_path) -> None:
    sandbox = CommandSandbox(allowed_dirs=[str(tmp_path)], enabled=True)
    assert sandbox.is_allowed(tmp_path / "x.txt")
    assert not sandbox.is_allowed("C:/Windows/System32")


def test_sandbox_runs_command(tmp_path) -> None:
    import sys

    sandbox = CommandSandbox(allowed_dirs=[str(tmp_path)], enabled=True)
    # Pas de shell : on appelle un vrai exécutable avec des arguments.
    result = sandbox.run(f'"{sys.executable}" -c "print(\'bonjour\')"')
    assert result.exit_code == 0, result.as_dict()
    assert "bonjour" in result.stdout


# --------------------------------------------------------------------------
# tiroir : byte-exact, vérification, sessions
# --------------------------------------------------------------------------
@pytest.fixture()
def store(config):
    from icyloom.context.store import ContextStore

    return ContextStore(config)


def test_store_and_get_is_byte_exact(store) -> None:
    payload = ("build 2026-10-26T01:11:00Z\n"
               "version=3.12.10 port=8151 host=127.0.0.1\n"
               "unicode=café naïve — 漢字 — emoji 🚀\n")
    outcome = store.store_text(payload, "default", "build.log")
    assert outcome.chunk_ids
    chunks = [store.get_chunk(str(cid)) for cid in outcome.chunk_ids]
    rebuilt = "".join(c["content"] for c in chunks)
    assert rebuilt == payload
    assert verify_roundtrip(payload, [c["content"] for c in chunks])


def test_verify_reports_full_coverage(store) -> None:
    store.store_text("Service sur le port 8151, chemin D:/x, version 2.0.1", "default", "a")
    report = store.verify()
    assert report["ok"], report
    assert report["coverage"] == 1.0


def test_check_reports_no_loss(store) -> None:
    store.store_text("port 8151 et version 1.0.0", "default", "b")
    check = store.check("default")
    assert check["ok"], check
    assert check["invariants_missing"] == []
    assert check["roundtrip_ok"]


def test_secrets_are_redacted_before_storage(store) -> None:
    store.store_text("mon token sk-abcdefghijklmnop1234 est ici", "default", "c")
    chunks = store.drawer.chunks_of_session(
        store.sessions.list()[0].session_id)
    assert not any(contains_secret(c.body) for c in chunks)


def test_sessions_are_created_and_purged(store) -> None:
    outcome = store.store_text("contenu de test", "default", "d")
    assert store.sessions.get(outcome.session_id) is not None
    store.sessions.purge(outcome.session_id)
    assert store.sessions.get(outcome.session_id) is None


def test_profile_switch_is_validated(store) -> None:
    assert store.set_profile("balanced")["ok"]
    assert store.profile.name == "balanced"
    assert store.set_profile("inexistant")["ok"] is False
    assert store.set_profile("strict")["ok"]


def test_search_finds_stored_content(store) -> None:
    store.store_text("le marqueur ALPHA_UNIQUE_XYZ est ici", "default", "e")
    time.sleep(0.05)
    result = store.search("ALPHA_UNIQUE_XYZ")
    assert result["count"] >= 1, result


def test_transfer_moves_chunks(store) -> None:
    outcome = store.store_text("contenu transférable", "default", "f")
    target = store.sessions.create("cible")
    result = store.transfer(outcome.session_id, target, outcome.chunk_ids)
    assert result["ok"]
    assert result["count"] == len(outcome.chunk_ids)
