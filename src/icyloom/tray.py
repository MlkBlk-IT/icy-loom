"""IcyLoom dans la zone de notification du système (system tray).

L'icône est le point d'entrée « sans terminal » de la pile : elle démarre le
serveur, ouvre la console, affiche l'état (passerelle, MCP, admin) et écrit la
configuration des clients MCP. Elle fonctionne aussi bien depuis un checkout de
développement que depuis une installation par wheel : tous les chemins passent
par :mod:`icyloom.paths`.

``pystray`` et ``Pillow`` sont importés **au moment de l'usage** : le paquet
reste importable sans interface graphique (serveur, CI, conteneur).
"""

from __future__ import annotations

import argparse
import errno
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

from . import autostart, paths

DEFAULT_ADMIN_PORT = 8153
LOCK_NAME = "tray.lock"
CLIENT_TARGETS = (
    ("Cursor", "cursor"),
    ("Claude Code", "claude-code"),
    ("Codex", "codex"),
    ("Windsurf", "windsurf"),
)


# ---------------------------------------------------------------------------
# réseau
# ---------------------------------------------------------------------------
def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    """Vrai si quelque chose écoute sur le port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _listening_pid_windows(port: int) -> int:
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        local = parts[1]
        if not local.rsplit(":", 1)[-1].isdigit():
            continue
        if int(local.rsplit(":", 1)[-1]) == port and parts[3].upper() == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                return 0
    return 0


def _listening_pid_posix(port: int) -> int:
    for command in (["ss", "-Hlntp"], ["netstat", "-lntp"]):
        try:
            out = subprocess.run(
                command, capture_output=True, text=True, timeout=5, check=False
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for line in out.splitlines():
            if f":{port}" not in line:
                continue
            if "pid=" in line:
                raw = line.split("pid=", 1)[1].split(",", 1)[0].strip()
                if raw.isdigit():
                    return int(raw)
            for token in line.replace("(", " ").replace(")", " ").split():
                if token.isdigit() and int(token) > 1:
                    return int(token)
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    for token in out.split():
        if token.isdigit():
            return int(token)
    return 0


def listening_pid(port: int) -> int:
    """PID du processus qui écoute sur le port (0 si personne)."""
    if sys.platform.startswith("win"):
        return _listening_pid_windows(port)
    return _listening_pid_posix(port)


def kill_tree(pid: int) -> bool:
    """Arrête un processus et ses enfants."""
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return True

    import signal

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
    for _ in range(30):
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except OSError:
        pass
    return True


# ---------------------------------------------------------------------------
# icône
# ---------------------------------------------------------------------------
def _bundled_asset(name: str) -> Path | None:
    """Fichier d'image livré dans le paquet (``static/``)."""
    try:
        from importlib.resources import files

        candidate = Path(str(files("icyloom").joinpath("static").joinpath(name)))
        if candidate.is_file():
            return candidate
    except Exception:  # pragma: no cover - dépend de l'installation
        pass
    local = Path(__file__).resolve().parent / "static" / name
    return local if local.is_file() else None


def tray_image(size: int = 64):
    """Logo IcyLoom (PNG embarqué dans le paquet), sinon un repli dessiné."""
    from PIL import Image, ImageDraw

    png = _bundled_asset("logo.png")
    if png is not None:
        try:
            return Image.open(png).convert("RGBA").resize((size, size), Image.LANCZOS)
        except OSError:
            pass
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (2, 2, size - 3, size - 3),
        radius=size // 6,
        fill=(253, 243, 227, 255),
        outline=(70, 35, 26, 255),
        width=3,
    )
    draw.polygon(
        [
            (size * 0.5, size * 0.16),
            (size * 0.66, size * 0.48),
            (size * 0.62, size * 0.56),
            (size * 0.74, size * 0.6),
            (size * 0.68, size * 0.78),
            (size * 0.5, size * 0.86),
            (size * 0.3, size * 0.78),
            (size * 0.26, size * 0.58),
            (size * 0.36, size * 0.46),
        ],
        fill=(255, 90, 54, 255),
        outline=(70, 35, 26, 255),
    )
    return image


# ---------------------------------------------------------------------------
# instance unique
# ---------------------------------------------------------------------------
def pid_alive(pid: int) -> bool:
    """Vrai si le processus existe encore."""
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def acquire_lock() -> tuple[Path, bool]:
    """Une seule icône à la fois : renvoie ``(fichier, obtenu)``."""
    path = paths.runtime_dir() / LOCK_NAME
    try:
        current = int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        current = 0
    if current and current != os.getpid() and pid_alive(current):
        return path, False
    path.write_text(str(os.getpid()), encoding="utf-8")
    return path, True


# ---------------------------------------------------------------------------
# la pile
# ---------------------------------------------------------------------------
class Stack:
    """Démarre et arrête la pile IcyLoom (passerelle + MCP + admin)."""

    def __init__(self, config: Path, admin_port: int = DEFAULT_ADMIN_PORT) -> None:
        self.config = config
        self.admin_port = admin_port
        self.proc: subprocess.Popen | None = None
        self.owns = False

    @property
    def running(self) -> bool:
        return port_open(self.admin_port)

    @property
    def pid(self) -> int:
        if self.proc is not None and self.proc.poll() is None:
            return self.proc.pid
        return listening_pid(self.admin_port)

    def start(self) -> bool:
        if self.running:
            return True
        log_path = paths.runtime_dir() / "logs" / "tray-stack.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        handle = open(log_path, "ab", buffering=0)
        try:
            self.proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "icyloom",
                    "--config",
                    str(self.config),
                    "serve",
                ],
                cwd=str(paths.project_root()),
                stdout=handle,
                stderr=handle,
                creationflags=flags,
                close_fds=True,
            )
        finally:
            handle.close()
        self.owns = True
        for _ in range(80):
            if self.running:
                return True
            if self.proc.poll() is not None:
                return False
            time.sleep(0.25)
        return self.running

    def stop(self) -> bool:
        pid = self.pid
        if not pid:
            return False
        ok = kill_tree(pid)
        if self.proc is not None:
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                pass
            self.proc = None
        self.owns = False
        return ok


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------
def resolve_config(explicit: str | None = None) -> Path:
    """Chemin du YAML : option, variable d'environnement, sinon défaut."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    return paths.config_path()


def ensure_config(config: Path) -> Path:
    """Crée une configuration par défaut si le fichier est absent."""
    if config.exists():
        return config
    from .config import write_default_config

    write_default_config(config)
    return config


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="icyloom tray",
        description="Icône IcyLoom dans la zone de notification",
    )
    parser.add_argument("--config", default=None, help="chemin du YAML de configuration")
    parser.add_argument("--background", action="store_true", help="sans fenêtre de terminal")
    parser.add_argument(
        "--no-start", action="store_true", help="ne démarre pas la pile au lancement"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        import pystray
    except ImportError:
        print(
            "Le system tray requiert pystray et Pillow.\n"
            '  Windows : pip install "icyloom[tray]"\n'
            '  Linux   : pip install "icyloom[tray-linux]"\n'
            '  macOS   : pip install "icyloom[tray-macos]"',
            file=sys.stderr,
        )
        return 2

    config = resolve_config(args.config)
    try:
        ensure_config(config)
    except OSError as exc:
        print(f"configuration inutilisable : {config} ({exc})", file=sys.stderr)
        return 2

    admin_port = DEFAULT_ADMIN_PORT
    gateway_port = DEFAULT_ADMIN_PORT
    mcp_http = DEFAULT_ADMIN_PORT
    mcp_sse = DEFAULT_ADMIN_PORT
    host = "127.0.0.1"
    try:
        from .config import load_config

        cfg = load_config(str(config))
        host = cfg.server.host
        admin_port = cfg.ports.admin
        gateway_port = cfg.ports.gateway_http
        mcp_http = cfg.ports.mcp_http
        mcp_sse = cfg.ports.mcp_sse
    except Exception:  # pragma: no cover - configuration partielle
        pass

    lock_path, acquired = acquire_lock()
    if not acquired:
        print("IcyLoom est déjà présent dans la zone de notification.", file=sys.stderr)
        return 0

    stack = Stack(config, admin_port)
    console_url = f"http://{host}:{admin_port}/dashboard"
    docs_url = f"http://{host}:{admin_port}/docs"
    state: dict[str, Any] = {"icon": None}

    def notify(message: str, title: str = "IcyLoom") -> None:
        icon = state.get("icon")
        if icon is not None:
            try:
                icon.notify(message, title)
            except Exception:  # pragma: no cover - notification best effort
                pass

    def refresh() -> None:
        icon = state.get("icon")
        if icon is not None:
            try:
                icon.update_menu()
            except Exception:  # pragma: no cover
                pass

    def open_console(_icon=None, _item=None) -> None:
        webbrowser.open(console_url)
        notify("Console ouverte")

    def open_docs(_icon=None, _item=None) -> None:
        webbrowser.open(docs_url)

    def start_stack(_icon=None, _item=None) -> None:
        if stack.running:
            notify("La pile tourne déjà")
        elif stack.start():
            notify("Pile démarrée — console prête")
        else:
            notify("Échec du démarrage — voir logs/tray-stack.log")
        refresh()

    def stop_stack(_icon=None, _item=None) -> None:
        if not stack.running:
            notify("La pile est déjà arrêtée")
        elif stack.stop():
            notify("Pile arrêtée")
        else:
            notify("Arrêt partiel — vérifiez le processus")
        refresh()

    def restart_stack(_icon=None, _item=None) -> None:
        stack.stop()
        time.sleep(0.6)
        start_stack()

    def status_text(_icon=None, _item=None) -> str:
        if stack.running:
            return f"Pile : en marche (pid {stack.pid})"
        return "Pile : arrêtée"

    def services_text(_icon=None, _item=None) -> str:
        marks = []
        for label, port in (
            ("passerelle", gateway_port),
            ("mcp http", mcp_http),
            ("mcp sse", mcp_sse),
            ("admin", admin_port),
        ):
            marks.append(f"{label} {'✓' if port_open(port, host) else '✗'}")
        return " · ".join(marks)

    def install_client(target: str, label: str):
        def handler(_icon=None, _item=None) -> None:
            try:
                from .clientcheck import install_client_config

                path = install_client_config(target, str(config), "stdio")
            except Exception as exc:
                notify(f"Échec {label} : {exc}")
                return
            notify(f"{label} configuré — {path}")

        return handler

    def autostart_enabled() -> bool:
        return bool(autostart.status(config).get("enabled"))

    def toggle_autostart(_icon=None, _item=None) -> None:
        if autostart_enabled():
            autostart.uninstall()
            notify("Démarrage automatique désactivé")
        else:
            try:
                autostart.install(config)
            except Exception as exc:
                notify(f"Échec autostart : {exc}")
                return
            notify("Démarrage automatique activé")
        refresh()

    def quit_app(icon=None, _item=None) -> None:
        if stack.owns and stack.running:
            stack.stop()
        if icon is not None:
            icon.stop()

    clients_menu = pystray.Menu(
        *[
            pystray.MenuItem(
                label,
                install_client(target, label),
                default=(target == "cursor"),
            )
            for label, target in CLIENT_TARGETS
        ],
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Afficher le bloc JSON à coller", lambda i, it: webbrowser.open(console_url)
        ),
    )

    autostart_menu = pystray.Menu(
        pystray.MenuItem(
            "Actif à l'ouverture de session",
            toggle_autostart,
            checked=lambda _item: autostart_enabled(),
        ),
        pystray.MenuItem(
            "Ouvrir le dossier de l'icône",
            lambda i, it: webbrowser.open(str(paths.runtime_dir())),
        ),
    )

    menu = pystray.Menu(
        pystray.MenuItem("Ouvrir la console", open_console, default=True),
        pystray.MenuItem("Documentation API", open_docs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Démarrer la pile", start_stack),
        pystray.MenuItem("Arrêter la pile", stop_stack),
        pystray.MenuItem("Redémarrer la pile", restart_stack),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Clients MCP", clients_menu),
        pystray.MenuItem("Démarrage automatique", autostart_menu),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem(services_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quitter IcyLoom", quit_app),
    )

    try:
        image = tray_image()
    except ImportError:
        print(
            'Pillow est requis pour l\'icône : pip install "icyloom[tray]"',
            file=sys.stderr,
        )
        return 2

    icon = pystray.Icon("icyloom", image, f"IcyLoom — {console_url}", menu)
    state["icon"] = icon

    def watcher() -> None:
        """Suit l'état de la pile et rafraîchit le menu au changement."""
        previous: bool | None = None
        while True:
            time.sleep(2.0)
            current = stack.running
            if current != previous:
                refresh()
                previous = current

    threading.Thread(target=watcher, daemon=True).start()

    try:
        if not args.no_start and not stack.running:
            stack.start()
        refresh()
        icon.run()
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
