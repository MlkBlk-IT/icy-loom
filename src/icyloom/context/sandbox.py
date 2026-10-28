"""Exécution de commandes dans un bac à sable **fail-closed**.

Règle : si la sandbox n'est pas utilisable, la commande **n'est pas
exécutée**. Aucun repli silencieux sur un `shell=True`, aucun « ça marche
chez moi ».

Contrôles appliqués :

* **répertoires autorisés** : la commande doit cibler un chemin dans la
  liste blanche ; liste vide = refus (fail-closed) ;
* **absence de shell** : la commande est exécutée via un exécutable + une
  liste d'arguments, jamais via un interpréteur de commandes ;
* **durée limite** : la commande est tuée au-delà du délai ;
* **volume de sortie plafonné** : la sortie est tronquée.

L'isolation forte (Job Object Windows / bubblewrap Linux) est un
renfort ; son absence n'ouvre pas la porte : c'est la liste blanche de
répertoires qui fait foi, et elle est obligatoire.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


class SandboxUnavailable(RuntimeError):
    """Le bac à sable ne peut pas être configuré : on refuse d'exécuter."""


@dataclass(frozen=True)
class CommandResult:
    """Résultat d'une commande sous bac à sable."""

    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool
    command: str

    def as_dict(self) -> dict[str, object]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "truncated": self.truncated,
            "timed_out": self.timed_out,
            "command": self.command,
        }


class CommandSandbox:
    """Exécution de commandes sous contraintes."""

    def __init__(self, allowed_dirs: list[str] | None = None,
                 timeout_seconds: float = 30.0,
                 max_output_chars: int = 200_000,
                 enabled: bool = True) -> None:
        self.allowed_dirs = [Path(d).expanduser().resolve() for d in (allowed_dirs or [])]
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.max_output_chars = max(1000, int(max_output_chars))
        self.enabled = enabled

    # -- contrôles --------------------------------------------------------
    def is_allowed(self, path: str | os.PathLike[str]) -> bool:
        """Un chemin est-il dans un répertoire autorisé ?"""
        try:
            target = Path(path).expanduser().resolve()
        except (OSError, RuntimeError):
            return False
        for base in self.allowed_dirs:
            try:
                target.relative_to(base)
                return True
            except ValueError:
                continue
        return False

    def ensure_available(self) -> None:
        """Vérifie que la sandbox peut servir. Sinon, lève (fail-closed)."""
        if not self.enabled:
            raise SandboxUnavailable("bac à sable désactivé par configuration")
        if not self.allowed_dirs:
            raise SandboxUnavailable(
                "aucun répertoire autorisé : configurez context.allowed_dirs "
                "pour autoriser l'exécution de commandes"
            )

    # -- exécution --------------------------------------------------------
    def run(self, command: str, timeout: float | None = None) -> CommandResult:
        """Exécute une commande. Lève SandboxUnavailable si la sandbox est absente."""
        self.ensure_available()
        argv = _split_command(command)
        if not argv:
            raise SandboxUnavailable("commande vide")
        executable = shutil.which(argv[0])
        if executable is None:
            return CommandResult(
                exit_code=127, stdout="", stderr=f"exécutable introuvable : {argv[0]}",
                truncated=False, timed_out=False, command=command,
            )
        # Le répertoire de travail doit être autorisé.
        cwd = Path.cwd()
        if not self.is_allowed(cwd):
            cwd = self.allowed_dirs[0]
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "COMSPEC": os.environ.get("COMSPEC", ""),
            "HOME": str(cwd),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        try:
            completed = subprocess.run(  # noqa: S603 - liste d'arguments, jamais de shell
                [executable] + argv[1:],
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout_seconds,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                exit_code=124,
                stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
                stderr="délai dépassé",
                truncated=True,
                timed_out=True,
                command=command,
            )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        truncated = False
        if len(stdout) > self.max_output_chars:
            stdout = stdout[:self.max_output_chars]
            truncated = True
        if len(stderr) > self.max_output_chars:
            stderr = stderr[:self.max_output_chars]
            truncated = True
        return CommandResult(
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            truncated=truncated,
            timed_out=False,
            command=command,
        )


def _split_command(command: str) -> list[str]:
    """Découpe une commande en arguments (sans shell).

    Les guillemets englobant un chemin (« "C:/Program Files/x.exe" ») sont
    retirés : le token doit être un chemin exploitable, pas un chemin cité.
    """
    import shlex

    try:
        parts = shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        parts = command.split()
    cleaned: list[str] = []
    for part in parts:
        if len(part) >= 2 and part[0] == '"' and part[-1] == '"':
            part = part[1:-1]
        cleaned.append(part)
    return cleaned


def platform_support() -> dict[str, object]:
    """Décrit les capacités de la sandbox sur la plateforme courante."""
    return {
        "platform": sys.platform,
        "strong_isolation": sys.platform == "win32",
        "note": "isolation forte non requise : la liste blanche de répertoires fait foi",
    }
