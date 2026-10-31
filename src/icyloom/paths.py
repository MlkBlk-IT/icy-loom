"""Résolution des chemins.

Deux situations, deux comportements :

* **dans un checkout** (un ``icyloom.yaml`` existe dans l'arborescence de
  travail, ou le paquet est installé en editable depuis un dépôt) : tout reste
  dans le dépôt — configuration à la racine, état dans ``var/`` ;
* **hors checkout** (installation par wheel) : rien n'est écrit dans le
  ``site-packages``. Configuration, base et journaux vont dans le **dossier de
  données utilisateur** :

  * Windows : ``%LOCALAPPDATA%\\icyloom`` ;
  * Linux / macOS : ``$XDG_DATA_HOME/icyloom`` ou ``~/.local/share/icyloom``.

  Ce dossier est surchargeable par la variable d'environnement
  ``ICYLOOM_DATA_DIR``.

Les fichiers de données (``normalization_rules.json``,
``tools_registry.json``) sont **embarqués dans le paquet** et lus via
``importlib.resources`` : ils existent donc toujours, même après une
installation hors dépôt. Hors checkout, ils sont recopiés une fois dans le
dossier de données utilisateur pour rester modifiables.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

#: Nom du fichier de configuration unique.
CONFIG_FILENAME = "icyloom.yaml"

#: Noms des fichiers de données externalisés (copiés à l'identique : le hash de
#: cache en dépend, les modifier invalide les entrées existantes).
DATA_FILENAMES = ("normalization_rules.json", "tools_registry.json")

#: Sous-dossier du paquet qui contient les données embarquées.
PACKAGE_DATA_DIR = "data"


# --------------------------------------------------------------------------
# dossier de données utilisateur
# --------------------------------------------------------------------------
def user_data_dir() -> Path:
    """Dossier de données utilisateur (hors dépôt).

    Surchargeable par ``ICYLOOM_DATA_DIR``.
    """
    env = os.environ.get("ICYLOOM_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    else:
        base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return (Path(base) / "icyloom").resolve()


# --------------------------------------------------------------------------
# détection du checkout
# --------------------------------------------------------------------------
def _checkout_root() -> Path | None:
    """Racine d'un checkout de développement, ou ``None``.

    Deux recherches :

    1. depuis le répertoire courant — un ``icyloom.yaml`` posé là par
       l'utilisateur est une intention explicite ;
    2. depuis l'emplacement du paquet — c'est le cas d'une installation
       editable ; on exige en plus un ``pyproject.toml`` pour ne pas
       confondre un ``site-packages`` avec un dépôt.
    """
    cwd = Path.cwd().resolve()
    for base in (cwd, *cwd.parents):
        if (base / CONFIG_FILENAME).is_file():
            return base
    for base in Path(__file__).resolve().parents:
        if (base / CONFIG_FILENAME).is_file() and (base / "pyproject.toml").is_file():
            return base
    return None


def is_checkout() -> bool:
    """Vrai si IcyLoom tourne depuis une arborescence de développement."""
    return _checkout_root() is not None


# --------------------------------------------------------------------------
# racines
# --------------------------------------------------------------------------
def project_root() -> Path:
    """Ancre des chemins relatifs (jamais codée en dur)."""
    env = os.environ.get("ICYLOOM_HOME")
    if env:
        return Path(env).expanduser().resolve()
    root = _checkout_root()
    if root is not None:
        return root
    return user_data_dir()


def config_path() -> Path:
    """Chemin du YAML de configuration (variable d'environnement prioritaire)."""
    env = os.environ.get("ICYLOOM_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    root = _checkout_root()
    if root is not None:
        return root / CONFIG_FILENAME
    return user_data_dir() / CONFIG_FILENAME


# --------------------------------------------------------------------------
# données embarquées
# --------------------------------------------------------------------------
def bundled_data_dir() -> Path:
    """Dossier des données livrées avec le paquet (lecture seule)."""
    try:
        from importlib.resources import files

        return Path(str(files("icyloom").joinpath(PACKAGE_DATA_DIR)))
    except Exception:  # pragma: no cover - dépend de l'installation
        return Path(__file__).resolve().parent / PACKAGE_DATA_DIR


def _seed_data(target: Path) -> Path:
    """Recopie les données embarquées vers le dossier utilisateur (une fois)."""
    source = bundled_data_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
        for name in DATA_FILENAMES:
            destination = target / name
            if destination.exists():
                continue
            origin = source / name
            if origin.is_file():
                shutil.copyfile(origin, destination)
    except OSError:  # pragma: no cover - dossier non inscriptible
        return source
    return target


def data_dir() -> Path:
    """Dossier des fichiers de données externalisés."""
    env = os.environ.get("ICYLOOM_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    if is_checkout():
        return bundled_data_dir()
    return _seed_data(user_data_dir() / PACKAGE_DATA_DIR)


def data_file(name: str) -> Path:
    """Chemin d'un fichier de données (créé/vérifié par l'appelant)."""
    if name not in DATA_FILENAMES:
        raise ValueError(f"fichier de données inconnu : {name}")
    return data_dir() / name


# --------------------------------------------------------------------------
# état d'exécution
# --------------------------------------------------------------------------
def resolve(relative: str) -> Path:
    """Résout un chemin de configuration contre la racine du projet.

    Un chemin absolu est Respecté tel quel (utile pour un montage externe),
    un chemin relatif est ancré sur la racine du projet.
    """
    raw = Path(str(relative)).expanduser()
    if raw.is_absolute():
        return raw
    return (project_root() / raw).resolve()


def runtime_dir() -> Path:
    """Dossier des fichiers d'exécution (base SQLite, logs)."""
    env = os.environ.get("ICYLOOM_STATE_DIR")
    if env:
        base = Path(env).expanduser()
    else:
        root = _checkout_root()
        base = (root / "var") if root is not None else user_data_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base.resolve()


def db_file(relative: str) -> Path:
    """Chemin du fichier SQLite unique."""
    raw = Path(str(relative)).expanduser()
    target = raw if raw.is_absolute() else (runtime_dir() / raw)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target.resolve()


def log_file(name: str = "icyloom.log") -> Path:
    """Fichier de log applicatif."""
    target = runtime_dir() / "logs" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    return target
