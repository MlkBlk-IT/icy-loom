"""IcyLoom dans la zone de notification de Windows (system tray).

La pile IcyLoom (passerelle HTTP + serveur MCP + API admin + console) tourne
dans un processus séparé : l'icône permet de l'**ouvrir** (console dans le
navigateur) et de la **fermer** (arrêt propre du processus), sans jamais
toucher à une fenêtre de terminal.

Lancement::

    .venv\\Scripts\\python.exe  tools\\tray.py     # avec fenêtre
    .venv\\Scripts\\pythonw.exe tools\\tray.py     # sans fenêtre (raccourci)

Options::

    --config <chemin>   configuration à servir (défaut : var\\demo.yaml si
                         présent, sinon icyloom.yaml)
    --port <n>          port de l'API admin (défaut : lu dans la config)
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
STATIC = PROJECT / "src" / "icyloom" / "static"
VAR = PROJECT / "var"
LOG = VAR / "tray-stack.log"

CONSOLE_URL = "http://127.0.0.1:8153/dashboard"
DOCS_URL = "http://127.0.0.1:8153/docs"

ICON = None  # pystray.Icon, filled in main()


# ---------------------------------------------------------------------------
# état de la pile
# ---------------------------------------------------------------------------
def port_open(port: int, host: str = "127.0.0.1") -> bool:
    """Vrai si quelque chose écoute sur le port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def listening_pid(port: int) -> int:
    """PID du processus qui écoute sur le port (0 si personne)."""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, text=True, timeout=8, check=False,
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


def kill_tree(pid: int) -> bool:
    """Arrête un processus et ses enfants (taskkill /T)."""
    if pid <= 0:
        return False
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=15, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    for _ in range(40):
        if not port_open(ADMIN_PORT):
            return True
        time.sleep(0.15)
    return True


# ---------------------------------------------------------------------------
# la pile
# ---------------------------------------------------------------------------
class Stack:
    """Démarre et arrête la pile IcyLoom."""

    def __init__(self, config: Path) -> None:
        self.config = config
        self.proc: subprocess.Popen | None = None
        self.owns = False

    @property
    def running(self) -> bool:
        return port_open(ADMIN_PORT)

    @property
    def pid(self) -> int:
        if self.proc is not None and self.proc.poll() is None:
            return self.proc.pid
        return listening_pid(ADMIN_PORT)

    def start(self) -> bool:
        if self.running:
            return True
        VAR.mkdir(parents=True, exist_ok=True)
        handle = open(LOG, "ab", buffering=0)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "icyloom", "--config", str(self.config), "serve"],
            cwd=str(PROJECT), stdout=handle, stderr=handle,
            creationflags=flags, close_fds=True,
        )
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
# icône
# ---------------------------------------------------------------------------
def tray_image(size: int = 64):
    """Logo IcyLoom (PNG du paquet), sinon un repli dessiné à la main."""
    from PIL import Image, ImageDraw

    png = STATIC / "logo.png"
    if png.exists():
        try:
            return Image.open(png).convert("RGBA").resize((size, size), Image.LANCZOS)
        except OSError:
            pass
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, size - 3, size - 3), radius=size // 6,
                           fill=(253, 243, 227, 255), outline=(70, 35, 26, 255), width=3)
    draw.polygon(
        [(size * 0.5, size * 0.16), (size * 0.66, size * 0.48), (size * 0.62, size * 0.56),
         (size * 0.74, size * 0.6), (size * 0.68, size * 0.78), (size * 0.5, size * 0.86),
         (size * 0.3, size * 0.78), (size * 0.26, size * 0.58), (size * 0.36, size * 0.46)],
        fill=(255, 90, 54, 255), outline=(70, 35, 26, 255),
    )
    return image


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------
def notify(message: str, title: str = "IcyLoom") -> None:
    if ICON is not None:
        try:
            ICON.notify(message, title)
        except Exception:      # pragma: no cover - notification best effort
            pass


def main() -> int:
    global ADMIN_PORT, ICON

    args = sys.argv[1:]
    config = PROJECT / "icyloom.yaml"
    for index, arg in enumerate(args):
        if arg == "--config" and index + 1 < len(args):
            config = Path(args[index + 1])
        elif arg.startswith("--config="):
            config = Path(arg.split("=", 1)[1])
    if not config.exists():
        demo = VAR / "demo.yaml"
        if demo.exists():
            config = demo
    if not config.exists():
        print(f"configuration introuvable : {config}", file=sys.stderr)
        return 2

    ADMIN_PORT = 8153
    try:
        import yaml
        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
        ports = (data.get("ports") or {}).get("admin")
        if ports:
            ADMIN_PORT = int(ports)
    except Exception:
        pass

    console_url = f"http://127.0.0.1:{ADMIN_PORT}/dashboard"
    docs_url = f"http://127.0.0.1:{ADMIN_PORT}/docs"
    stack = Stack(config)

    def open_console(_icon=None, _item=None) -> None:
        webbrowser.open(console_url)
        notify("Console ouverte", "IcyLoom")

    def open_docs(_icon=None, _item=None) -> None:
        webbrowser.open(docs_url)

    def start(_icon=None, _item=None) -> None:
        if stack.running:
            notify("La pile tourne déjà", "IcyLoom")
            return
        if stack.start():
            notify("Pile démarrée — console prête", "IcyLoom")
        else:
            notify(f"Échec du démarrage, voir {LOG.name}", "IcyLoom")
        refresh()

    def stop(_icon=None, _item=None) -> None:
        if not stack.running:
            notify("La pile est déjà arrêtée", "IcyLoom")
            return
        if stack.stop():
            notify("Pile arrêtée", "IcyLoom")
        else:
            notify("Arrêt partiel — vérifiez le processus", "IcyLoom")
        refresh()

    def restart(_icon=None, _item=None) -> None:
        stack.stop()
        time.sleep(0.6)
        start()

    def quit_app(icon=None, _item=None) -> None:
        if stack.owns and stack.running:
            stack.stop()
        if icon is not None:
            icon.stop()

    def status_text(_icon=None, _item=None) -> str:
        if stack.running:
            return f"Pile : en marche (pid {stack.pid})"
        return "Pile : arrêtée"

    import pystray

    menu = pystray.Menu(
        pystray.MenuItem("Ouvrir la console", open_console, default=True),
        pystray.MenuItem("Documentation API", open_docs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Démarrer la pile", start),
        pystray.MenuItem("Arrêter la pile", stop),
        pystray.MenuItem("Redémarrer", restart),
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quitter IcyLoom", quit_app),
    )

    ICON = pystray.Icon(
        "icyloom",
        tray_image(),
        f"IcyLoom — {console_url}",
        menu,
    )

    def refresh() -> None:
        if ICON is not None:
            try:
                ICON.update_menu()
            except Exception:      # pragma: no cover
                pass

    def watcher() -> None:
        """Suit l'état de la pile et rafraîchit le menu seulement quand le statut change."""
        prev = None
        while True:
            time.sleep(2.0)
            cur = stack.running
            if cur != prev:
                refresh()
                prev = cur

    threading.Thread(target=watcher, daemon=True).start()

    if not stack.running:
        stack.start()
    refresh()
    ICON.run()
    return 0


ADMIN_PORT = 8153

if __name__ == "__main__":
    raise SystemExit(main())
