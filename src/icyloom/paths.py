"""Résolution des chemins.

Règle du projet : **aucun chemin absolu codé en dur**. Tous les chemins sont
déduits de la racine du projet, elle-même localisée par :

1. la variable d'environnement ``ICYLOOM_HOME`` si elle est définie ;
2. à défaut, la première racine de projet (dossier contenant ``icyloom.yaml``)
   rencontrée en remontant depuis le répertoire courant ;
3. à défaut, la racine déduite de l'emplacement du paquet.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Nom du fichier de configuration unique.
CONFIG_FILENAME = "icyloom.yaml"

#: Noms des fichiers de données externalisés (copiés à l'identique : le hash de
#: cache en dépend, les modifier invalide les entrées existantes).
DATA_FILENAMES = ("normalization_rules.json", "tools_registry.json")


def project_root() -> Path:
    """Racine du projet IcyLoom (jamais codée en dur)."""
    env = os.environ.get("ICYLOOM_HOME")
    if env:
        return Path(env).expanduser().resolve()

    candidates = [Path.cwd().resolve(), *Path(__file__).resolve().parents]
    for base in candidates:
        if (base / CONFIG_FILENAME).is_file():
            return base
    # Paquet installé hors arborescence de projet : on prend le parent du paquet.
    return Path(__file__).resolve().parents[2]


def config_path() -> Path:
    """Chemin du YAML de configuration (variable d'environnement prioritaire)."""
    env = os.environ.get("ICYLOOM_CONFIG")
    if env:
        return Path(env).expanduser().resolve()
    return project_root() / CONFIG_FILENAME


def data_dir() -> Path:
    """Dossier des fichiers de données externalisés."""
    return project_root() / "data"


def data_file(name: str) -> Path:
    """Chemin d'un fichier de données (créé/vérifié par l'appelant)."""
    if name not in DATA_FILENAMES:
        raise ValueError(f"fichier de données inconnu : {name}")
    return data_dir() / name


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
    base = Path(env).expanduser() if env else project_root() / "var"
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
