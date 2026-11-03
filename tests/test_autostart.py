"""Démarrage automatique de l'icône : commande, mechanisms, cycle install/uninstall."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from icyloom import autostart  # noqa: E402


def test_mechanism_is_known() -> None:
    assert autostart.mechanism() in {"windows", "linux", "macos", "unsupported"}


def test_command_runs_the_tray_without_a_console() -> None:
    args = autostart.command()
    assert args[-2:] == ["tray", "--background"]
    assert "-m" in args and "icyloom" in args


def test_command_freezes_the_config_path() -> None:
    args = autostart.command("D:/tmp/icyloom.yaml")
    assert "--config" in args
    assert args[args.index("--config") + 1] == "D:/tmp/icyloom.yaml"
    # --config précède le sous-commande : le parseur global l'exige.
    assert args.index("--config") < args.index("tray")


def test_quote_protects_paths_with_spaces() -> None:
    assert autostart.quote(["C:/Program Files/python.exe"]) == '"C:/Program Files/python.exe"'
    assert autostart.quote(["python", "-m", "icyloom"]) == "python -m icyloom"


def test_linux_roundtrip(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(autostart, "mechanism", lambda: "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert autostart.status()["enabled"] is False

    location = autostart.install("/tmp/icyloom.yaml")
    desktop = Path(location)
    assert desktop.is_file()
    assert desktop.name == "icyloom.desktop"
    assert "tray --background" in desktop.read_text(encoding="utf-8")

    info = autostart.status()
    assert info["enabled"] is True
    assert info["mechanism"] == "linux"

    assert autostart.uninstall() is True
    assert autostart.status()["enabled"] is False


def test_uninstall_is_idempotent(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(autostart, "mechanism", lambda: "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert autostart.uninstall() is False


def test_install_refuses_unknown_platform(monkeypatch) -> None:
    monkeypatch.setattr(autostart, "mechanism", lambda: "unsupported")
    try:
        autostart.install()
    except RuntimeError:
        return
    raise AssertionError("RuntimeError attendue")
