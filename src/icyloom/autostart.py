"""Démarrage automatique de l'icône IcyLoom à l'ouverture de session.

Un mécanisme par plateforme, toujours **au niveau utilisateur** — aucun droit
administrateur n'est requis :

* **Windows** : clé ``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``
  (valeur ``IcyLoom``) ;
* **Linux** : entrée XDG ``~/.config/autostart/icyloom.desktop`` ;
* **macOS** : agent de lancement ``~/Library/LaunchAgents/com.icyloom.tray.plist``.

La commande écrite est **figée** au moment de l'installation :
``<python> -m icyloom --config <yaml> tray --background``. Le chemin du YAML
est donc explicite : au démarrage suivant, l'icône ouvre le même fichier, même
lancée depuis un autre dossier.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "IcyLoom"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "IcyLoom"
LINUX_DESKTOP_NAME = "icyloom.desktop"
MACOS_LABEL = "com.icyloom.tray"


# ---------------------------------------------------------------------------
# plateforme
# ---------------------------------------------------------------------------
def mechanism() -> str:
    """Mécanisme d'autostart de la plateforme (``unsupported`` si inconnu)."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unsupported"


def pythonw() -> Path:
    """Interpréteur à utiliser pour une exécution sans fenêtre."""
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        return executable
    candidate = executable.with_name("pythonw.exe")
    if candidate.is_file():
        return candidate
    return executable


def command(config_path: str | os.PathLike[str] | None = None) -> list[str]:
    """Ligne de commande qui lance l'icône sans fenêtre."""
    args = [str(pythonw()), "-m", "icyloom"]
    if config_path:
        args += ["--config", str(config_path)]
    args += ["tray", "--background"]
    return args


def quote(args: list[str]) -> str:
    """Ligne de commande Shell : les chemins qui contiennent un espace sont cités."""
    return " ".join(f'"{part}"' if " " in part else part for part in args)


# ---------------------------------------------------------------------------
# Windows : clé Run du registre utilisateur
# ---------------------------------------------------------------------------
def windows_location() -> str:
    return f"HKCU\\{RUN_KEY_PATH}\\{RUN_VALUE_NAME}"


def _windows_read() -> str | None:
    try:
        import winreg
    except ImportError:  # pragma: no cover - hors Windows
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE_NAME)
    except OSError:
        return None
    return str(value)


def _windows_write(command_line: str) -> None:
    import winreg

    key = winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
    )
    try:
        winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, command_line)
    finally:
        winreg.CloseKey(key)


def _windows_delete() -> bool:
    try:
        import winreg
    except ImportError:  # pragma: no cover - hors Windows
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, RUN_VALUE_NAME)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Linux : entrée XDG autostart
# ---------------------------------------------------------------------------
def linux_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / LINUX_DESKTOP_NAME


def _linux_write(args: list[str]) -> None:
    path = linux_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Passerelle MCP locale : cache LLM + tiroir de contexte\n"
        f"Exec={quote(args)}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# macOS : agent de lancement
# ---------------------------------------------------------------------------
def macos_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"


def _macos_write(args: list[str]) -> None:
    import plistlib

    path = macos_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": MACOS_LABEL,
        "ProgramArguments": args,
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    }
    with path.open("wb") as handle:
        plistlib.dump(payload, handle)


# ---------------------------------------------------------------------------
# API publique
# ---------------------------------------------------------------------------
def install(config_path: str | os.PathLike[str] | None = None) -> str:
    """Active le démarrage automatique. Renvoie l'emplacement écrit."""
    args = command(config_path)
    kind = mechanism()
    if kind == "windows":
        _windows_write(quote(args))
        return windows_location()
    if kind == "linux":
        _linux_write(args)
        return str(linux_path())
    if kind == "macos":
        _macos_write(args)
        return str(macos_path())
    raise RuntimeError("démarrage automatique non supporté sur cette plateforme")


def uninstall() -> bool:
    """Désactive le démarrage automatique. Vrai si une entrée a été retirée."""
    kind = mechanism()
    if kind == "windows":
        return _windows_delete()
    if kind == "linux":
        path = linux_path()
    elif kind == "macos":
        path = macos_path()
    else:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def status(config_path: str | os.PathLike[str] | None = None) -> dict:
    """État de l'autostart : ``mechanism``, ``supported``, ``enabled``, ``location``."""
    kind = mechanism()
    info: dict = {
        "mechanism": kind,
        "supported": kind != "unsupported",
        "enabled": False,
        "location": "",
        "command": "",
    }
    if kind == "windows":
        info["location"] = windows_location()
        current = _windows_read()
        info["enabled"] = bool(current)
        info["command"] = current or ""
    elif kind == "linux":
        info["location"] = str(linux_path())
        info["enabled"] = linux_path().is_file()
    elif kind == "macos":
        info["location"] = str(macos_path())
        info["enabled"] = macos_path().is_file()
    if info["enabled"] and not info["command"]:
        info["command"] = quote(command(config_path))
    return info
