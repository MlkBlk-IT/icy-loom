"""Fixtures partagées : configuration de test et amont factice."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_upstream import FakeUpstream  # noqa: E402

from icyloom.config import Config, DatabaseConfig, LedgerConfig, PortsConfig  # noqa: E402
from icyloom.normalize import Canonicalizer  # noqa: E402
from icyloom.paths import data_file  # noqa: E402

#: Fichier de règles de normalisation (embarqué dans le paquet).
RULES = data_file("normalization_rules.json")

#: Port du faux amont. Surchargeable pour que la suite puisse tourner
#: pendant qu'une pile IcyLoom occupe 8154 (démo, développement) :
#: ``$env:ICYLOOM_TEST_PORT = 8159``.
TEST_PORT = int(os.environ.get("ICYLOOM_TEST_PORT", "8154"))


def build_config(tmp_path: Path, upstream_url: str = "http://127.0.0.1:8154/v1",
                 **kwargs) -> Config:
    """Configuration isolée : base temporaire, amont factice, ports de test."""
    payload = {
        "server": {"name": "icyloom-test", "host": "127.0.0.1"},
        "ports": PortsConfig(gateway_http=8154, mcp_http=8155, mcp_sse=8156,
                             admin=8157).model_dump(),
        "database": DatabaseConfig(path=str(tmp_path / "icyloom-test.db")).model_dump(),
        "ledger": LedgerConfig(enabled=True).model_dump(),
        "upstreams": [{
            "name": "fake",
            "base_url": upstream_url,
            "priority": 10,
            "kind": "openai",
        }],
    }
    payload.update(kwargs)
    return Config.model_validate(payload)


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    return build_config(tmp_path, upstream_url=f"http://127.0.0.1:{TEST_PORT}/v1")


@pytest.fixture()
def upstream() -> FakeUpstream:
    fake = FakeUpstream(port=TEST_PORT)
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture()
def canonicalizer() -> Canonicalizer:
    return Canonicalizer(rules_path=RULES)
