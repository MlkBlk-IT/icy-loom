"""Les racines accordées par la configuration atteignent les serveurs descendants.

Un serveur de fichiers MCP limite son accès aux dossiers que son client
déclare. Derrière IcyLoom, c'est IcyLoom qui est le client : s'il ne transmet
que les racines du client amont, un serveur refuse ``D:\\`` alors que la
policy du gateway l'autorise. Les racines de ``policy.project_roots`` sont
donc transmises **en plus** des racines du client.
"""
import asyncio
from pathlib import Path

from conftest import build_config
from mcp.types import Root

from icyloom.toolproxy import ToolProxy, _root_uri


def _proxy(tmp_path: Path) -> ToolProxy:
    return ToolProxy(build_config(tmp_path, policy={
        "enabled": True,
        "default_role": "operator",
        "environment": "dev",
        "project_roots": ["C:\\", "D:\\"],
    }))


def test_root_uri_accepts_windows_paths() -> None:
    assert _root_uri("D:\\") == "file:///D:/"
    assert _root_uri("C:\\Users\\Tech") == "file:///C:/Users/Tech/"
    assert _root_uri("file:///D:/") == "file:///D:/"
    assert _root_uri("") == ""


def test_granted_roots_come_from_the_policy(tmp_path: Path) -> None:
    proxy = _proxy(tmp_path)
    uris = [str(root.uri) for root in proxy._granted_roots()]
    assert "file:///D:/" in uris
    assert "file:///C:/" in uris


def test_client_roots_are_unioned_with_granted_roots(tmp_path: Path) -> None:
    proxy = _proxy(tmp_path)
    proxy.set_client_roots([Root(uri="file:///C:/Users/Tech", name="home")])
    result = asyncio.run(proxy._roots_callback())
    uris = [str(root.uri) for root in result.roots]
    assert "file:///C:/Users/Tech" in uris
    assert "file:///D:/" in uris


def test_a_root_is_never_declared_twice(tmp_path: Path) -> None:
    proxy = _proxy(tmp_path)
    proxy.set_client_roots([Root(uri="file:///D:/", name="depot")])
    result = asyncio.run(proxy._roots_callback())
    uris = [str(root.uri) for root in result.roots]
    assert uris.count("file:///D:/") == 1
