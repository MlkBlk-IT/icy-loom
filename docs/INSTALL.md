# Installer IcyLoom (v2)

IcyLoom s'installe en quelques minutes : un environnement Python, un paquet, un
fichier de configuration. Sur Windows, l'icône de zone de notification
(**system tray**) est installée **avec** le paquet — pas d'étape supplémentaire.

Guide du system tray seul : **[TRAY.md](TRAY.md)**.

---

## Hermes Agent Plugin (Catalog Ready)

IcyLoom is also packaged as a **Hermes Agent plugin** — installable by name once listed in the plugin catalog:

```bash
# After catalog merge (pending PR)
hermes plugins install icyloom
```

The plugin declares 21 MCP tools and runs the full gateway stack (ports 8150-8153).
See `plugin.yaml` for the manifest and `__init__.py` for the entrypoint.

---

## 1. Prérequis

| | |
|---|---|
| Python | 3.11, 3.12 ou 3.13 (3.12 recommandé) |
| Système | Windows 10/11, Linux (X11 / AppIndicator), macOS |
| Matériel | aucun GPU requis, 2 Go de RAM suffisent |
| Compte | aucun compte cloud, aucun fournisseur imposé |

---

## 2. Windows — installation guidée (une commande)

Depuis un clone du dépôt :

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
```

Ce que fait le script, dans l'ordre :

1. cherche un Python 3.11+ (`py -3` puis `python`) ;
2. crée l'environnement virtuel `%LOCALAPPDATA%\icyloom\venv` ;
3. installe `icyloom[tray]` — le system tray est inclus ;
4. écrit la configuration (`icyloom init`) ;
5. enregistre l'icône au démarrage de session (`HKCU\…\Run\IcyLoom`) ;
6. configure le client MCP `cursor` ;
7. affiche `icyloom doctor` puis `icyloom tray --status`.

Options utiles :

```powershell
# plusieurs clients MCP d'un coup
.\scripts\install-windows.ps1 -Clients cursor,codex,claude-code

# sans démarrage automatique
.\scripts\install-windows.ps1 -NoAutostart

# une version précise, ou un wheel local
.\scripts\install-windows.ps1 -Source "git+https://github.com/MlkBlk-IT/icy-loom.git@v2.0.0"
.\scripts\install-windows.ps1 -Source C:\Downloads\icyloom-2.0.0-py3-none-any.whl

# ailleurs que dans %LOCALAPPDATA%\icyloom
.\scripts\install-windows.ps1 -InstallDir "D:\Tools\icyloom"
```

Rien n'est écrit hors de `%LOCALAPPDATA%\icyloom` (et de la clé de registre
`HKCU` pour l'autostart) : **aucun droit administrateur n'est demandé**.

---

## 3. Installation manuelle

### Windows (PowerShell)

```powershell
py -3.12 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install "icyloom[tray] @ git+https://github.com/MlkBlk-IT/icy-loom.git@v2.0.0"

icyloom init
icyloom doctor
icyloom tray --install-autostart
icyloom client --target cursor --install
icyloom tray
```

### Linux / macOS

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install "icyloom[tray] @ git+https://github.com/MlkBlk-IT/icy-loom.git@v2.0.0"

icyloom init
icyloom doctor
icyloom tray --install-autostart
icyloom client --target cursor --install
icyloom tray
```

Sur Linux, l'icône a besoin d'un **hôte de notification** (barre d'état) :

```bash
sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1   # Debian / Ubuntu
pip install "icyloom[tray-linux]"                              # si pystray ne suffit pas
```

Sur macOS, `pystray` installe lui-même les liaisons Cocoa ; si le trait
`pyobjc-framework-Quartz` manque :

```bash
pip install "icyloom[tray-macos]"
```

> **Windows sans gestion du paquet** : `pip install icyloom` suffit — `pystray`
> et `Pillow` y sont des dépendances par défaut, donc l'icône est disponible
> sans `[tray]`. L'extra reste utile sur Linux et macOS.

### Depuis le wheel publié

```powershell
python -m pip install https://github.com/MlkBlk-IT/icy-loom/releases/download/v2.0.0/icyloom-2.0.0-py3-none-any.whl
```

(L'URL exacte de l'asset est publiée avec la release.)

---

## 4. Vérifier l'installation

```bash
icyloom doctor          # chemins, versions, ports, schéma de la base
icyloom check --transport all   # un vrai client se connecte et appelle les outils
icyloom tray --status   # état de la pile, des services et de l'autostart
```

`icyloom check` joue le rôle du client : il démarre le serveur, se connecte,
liste les outils et les appelle. Il teste les **trois** transports (stdio,
streamable-HTTP, SSE). Si HTTP ou SSE échoue, la pile n'est pas démarrée —
le mode **stdio** est autonome.

---

## 5. Configuration

Un seul fichier YAML, `icyloom.yaml`. Sa position dépend du mode :

| Situation | Fichier |
|---|---|
| Dans un checkout (repo cloné) | `<dépôt>/icyloom.yaml` |
| Installé par wheel | `%LOCALAPPDATA%\icyloom\icyloom.yaml` (Windows) |
| | `$XDG_DATA_HOME/icyloom/icyloom.yaml` ou `~/.local/share/icyloom/icyloom.yaml` |

```bash
icyloom init                 # écrit la configuration par défaut
icyloom init --path C:/...  # ailleurs
icyloom paths                # affiche tous les chemins résolus
icyloom config get ports     # lit une clé
```

Variables d'environnement :

| Variable | Effet |
|---|---|
| `ICYLOOM_CONFIG` | chemin du YAML (prioritaire) |
| `ICYLOOM_DATA_DIR` | dossier de données utilisateur |
| `ICYLOOM_STATE_DIR` | dossier de la base et des journaux |
| `ICYLOOM_HOME` | racine du projet |

Ports par défaut :

| Service | Port |
|---|---|
| passerelle HTTP (OpenAI/Anthropic) | 8150 |
| MCP streamable-HTTP | 8151 |
| MCP SSE | 8152 |
| API d'administration + console | 8153 |

---

## 6. Démarrer

```bash
icyloom serve      # les quatre services dans un terminal
icyloom tray       # l'icône : elle démarre la pile toute seule
```

L'icône est le mode « sans terminal » : elle démarre la pile, ouvre la
console, montre l'état et écrit la configuration des clients MCP.
Détails : **[TRAY.md](TRAY.md)**.

---

## 7. Brancher les clients MCP

**stdio** (Cursor, Claude Code, Codex, Windsurf) — chaque client lance son
propre processus IcyLoom, c'est le mode le plus simple :

```bash
icyloom client --target cursor --install
icyloom client --target claude-code --install
icyloom client --target codex --install
icyloom client --target windsurf --install
```

**http / sse** — un seul processus partagé, utile quand plusieurs clients
parlent au même serveur :

```bash
icyloom client --target json --transport http   # affiche le bloc à coller
```

Redémarrez le client MCP après l'écriture de la configuration.

---

## 8. Mettre à jour

```bash
python -m pip install --upgrade "icyloom[tray] @ git+https://github.com/MlkBlk-IT/icy-loom.git@main"
icyloom doctor
```

La configuration et la base sont conservées : ni le YAML ni SQLite ne sont
écrasés par une mise à jour.

---

## 9. Désinstaller

```bash
icyloom tray --uninstall-autostart   # retire l'entrée de démarrage
pip uninstall icyloom
```

Restent alors, à supprimer si vous voulez : le dossier de données utilisateur
(`%LOCALAPPDATA%\icyloom`, `~/.local/share/icyloom`) et la clé
`HKCU\Software\Microsoft\Windows\CurrentVersion\Run\IcyLoom` si elle existe
encore.

---

## 10. Dépannage

| Symptôme | Cause probable | Solution |
|---|---|---|
| `Le system tray requiert pystray et Pillow` | extra non installé | `pip install "icyloom[tray]"` |
| L'icône n'apparaît pas | elle est dans le Overflow (`^`) de la barre | cliquer sur `^` puis glisser l'icône |
| `configuration introuvable` | YAML absent ou `--config` erroné | `icyloom init` puis `icyloom paths` |
| Le port 8153 est occupé | une autre pile tourne | `icyloom tray --status` pour voir le pid |
| `PermissionError` sur `%LOCALAPPDATA%` | droits du dossier | vérifier `%LOCALAPPDATA%` ou `ICYLOOM_DATA_DIR` |
| Le client MCP ne voit rien | configuration non rechargée | redémarrer le client MCP, puis `icyloom check` |
| `command not found: icyloom` | venv non activé | `.venv\Scripts\Activate.ps1` |

---

## Voir aussi

- **[TRAY.md](TRAY.md)** — l'icône en détail
- [README](../README.md) — vue d'ensemble et capacités
- [TESTING.md](../TESTING.md) — la suite de tests
