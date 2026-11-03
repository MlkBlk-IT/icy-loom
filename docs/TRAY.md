# Le system tray IcyLoom

L'icône IcyLoom est le mode « sans terminal » de la pile : elle démarre le
serveur, ouvre la console, montre l'état, écrit la configuration des clients
MCP d'un clic et se place au démarrage de session.

Elle est **livrée avec le paquet** (`pip install icyloom` suffit sur Windows) :
rien à installer à part. Guide d'installation complet : **[INSTALL.md](INSTALL.md)**.

---

## 1. Lancer l'icône

```bash
icyloom tray                 # avec une fenêtre : on voit les messages
icyloom tray --background    # sans fenêtre (ce que fait l'autostart)
icyloom tray --no-start      # n'ouvre pas la pile, juste l'icône
```

Depuis un checkout de développement : `tools\launch_tray.cmd` (Windows) ou
`python tools/tray.py`.

Au lancement, l'icône **démarre la pile** si elle ne tourne pas déjà
(`icyloom serve` dans un processus séparé, sans fenêtre). Un clic sur
l'icône affiche le menu.

---

## 2. Le menu

| Entrée | Effet |
|---|---|
| **Ouvrir la console** | ouvre `http://127.0.0.1:8153/dashboard` (double-clic = action par défaut) |
| **Documentation API** | ouvre `http://127.0.0.1:8153/docs` |
| **Démarrer la pile** | lance `icyloom serve` en arrière-plan |
| **Arrêter la pile** | arrête le processus et ses enfants |
| **Redémarrer la pile** | arrêt puis démarrage |
| **Clients MCP** | sous-menu : Cursor, Claude Code, Codex, Windsurf |
| **Démarrage automatique** | sous-menu : activer / désactiver, ouvrir le dossier de l'icône |
| *Pile : en marche (pid …)* | état en lecture seule |
| *passerelle ✓ · mcp http ✓ · …* | état des quatre services, en lecture seule |
| **Quitter IcyLoom** | ferme l'icône (et la pile si elle l'a démarrée) |

Le trait `✓` / `✗` indique si le service écoute sur son port.

---

## 3. Brancher les clients MCP depuis l'icône

**Clients MCP → Cursor** écrit le bloc `mcpServers` dans la configuration du
client (transport **stdio** : le client lance lui-même `icyloom mcp`). Les
mêmes écritures que :

```bash
icyloom client --target cursor --install
```

Cibles reconnues : `cursor`, `claude` (Desktop), `claude-code`, `windsurf`,
`codex`.

> Après l'écriture, **redémarrez le client MCP** : il lit sa configuration au
> démarrage.

---

## 4. Démarrage automatique

Un mécanisme par plateforme, **au niveau utilisateur** (aucun droit
administrateur) :

| Plateforme | Emplacement | Entrée |
|---|---|---|
| Windows | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` | `IcyLoom` |
| Linux | `~/.config/autostart/icyloom.desktop` | `Exec=… -m icyloom tray --background` |
| macOS | `~/Library/LaunchAgents/com.icyloom.tray.plist` | `RunAtLoad` |

Commandes :

```bash
icyloom tray --install-autostart     # active
icyloom tray --uninstall-autostart   # désactive
icyloom tray --status                # état complet
```

La commande enregistrée est **figée** au moment de l'installation
(`<pythonw> -m icyloom --config <yaml> tray --background`) : le chemin du
YAML est explicite, l'icône ouvre donc toujours le même fichier, même lancée
depuis un autre dossier.

Sur Windows, `pythonw.exe` est utilisé : aucune fenêtre ne s'ouvre à la
connexion.

---

## 5. Une seule instance

L'icône s'auto-verrouille (fichier `tray.lock` dans le dossier d'exécution).
Un second lancement ne crée pas une seconde icône : il affiche « IcyLoom est
déjà présent dans la zone de notification » et sort.

Le verrou est repris automatiquement si le processus précédent a été tué.

---

## 6. Où sont les journaux

| Fichier | Contenu |
|---|---|
| `logs/tray-stack.log` | sortie de la pile lancée par l'icône |
| `logs/serve.log` | journal applicatif de la pile |
| `logs/mcp.log`, `logs/gateway.log`, `logs/admin.log` | journaux par service |

Le dossier dépend du mode : `<dépôt>/var/` dans un checkout, sinon le dossier
de données utilisateur (`%LOCALAPPDATA%\icyloom`, `~/.local/share/icyloom`).

---

## 7. Dépannage

| Symptôme | Cause | Solution |
|---|---|---|
| L'icône n'apparaît pas | elle est dans l'**Overflow** (`^`) de la barre de notification | cliquer sur `^`, puis glisser l'icône hors du Overflow |
| `Le system tray requiert pystray et Pillow` | extra non installé | `pip install "icyloom[tray]"` |
| La pile ne démarre pas | erreur au lancement | lire `logs/tray-stack.log` |
| « IcyLoom est déjà présent… » | une icône tourne déjà | quitter l'icône existante, ou supprimer `tray.lock` si le processus est mort |
| La console ne s'ouvre pas | le port 8153 est occupé par autre chose | `icyloom tray --status` pour voir qui répond |
| L'icône est écrasée par une autre | pas d'icône dédiée | sans conséquence : le logo est régénéré à la volée si le PNG manque |

---

## 8. Retirer l'icône

```bash
icyloom tray --uninstall-autostart
```

L'icône disparaît de la zone de notification quand l'application est fermée
(« Quitter IcyLoom »). Le paquet lui-même reste installé : la console et le
serveur fonctionnent sans l'icône.
