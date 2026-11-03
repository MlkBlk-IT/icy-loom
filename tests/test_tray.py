"""System tray : chemins, port, instance unique, CLI."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from icyloom import paths  # noqa: E402
from icyloom import tray  # noqa: E402
from icyloom.cli import build_parser  # noqa: E402


def test_resolve_config_prefers_the_explicit_path(tmp_path) -> None:
    target = tmp_path / "custom.yaml"
    assert tray.resolve_config(str(target)) == target.resolve()


def test_resolve_config_falls_back_to_the_project() -> None:
    assert tray.resolve_config() == paths.config_path()


def test_port_open_detects_a_listening_socket() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert tray.port_open(port) is True
    assert tray.port_open(port) is False


def test_pid_alive_detects_the_current_process() -> None:
    assert tray.pid_alive(os.getpid()) is True
    assert tray.pid_alive(0) is False


def test_acquire_lock_refuses_a_second_instance(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(paths, "runtime_dir", lambda: tmp_path)

    lock, acquired = tray.acquire_lock()
    assert acquired is True
    assert lock.read_text(encoding="utf-8") == str(os.getpid())

    # un verrou détenu par un autre processus vivant = une icône tourne déjà
    lock.write_text(str(os.getppid()), encoding="utf-8")
    _, again = tray.acquire_lock()
    assert again is False


def test_acquire_lock_takes_over_a_dead_lock(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(paths, "runtime_dir", lambda: tmp_path)
    lock = tmp_path / tray.LOCK_NAME
    lock.write_text("999999", encoding="utf-8")

    _, acquired = tray.acquire_lock()
    assert acquired is True


def test_tray_image_uses_the_bundled_logo() -> None:
    pytest.importorskip("PIL")
    image = tray.tray_image(32)
    assert image.size == (32, 32)


def test_cli_tray_does_not_clobber_the_global_config() -> None:
    parser = build_parser()
    args = parser.parse_args(["--config", "D:/tmp/icyloom.yaml", "tray"])
    assert args.config == "D:/tmp/icyloom.yaml"
    assert args.command == "tray"


def test_cli_tray_accepts_its_own_config() -> None:
    parser = build_parser()
    args = parser.parse_args(["tray", "--config", "D:/tmp/other.yaml", "--background"])
    assert args.config == "D:/tmp/other.yaml"
    assert args.background is True
