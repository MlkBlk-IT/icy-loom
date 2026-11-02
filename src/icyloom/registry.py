"""Catalogue centralisé des outils : registre, index de sélection, contrats.

Le registre est la source de vérité du gateway : chaque outil descendant y
est décrit (schéma d'entrée, domaine, intention, effets de bord, niveau de
sécurité, portée, propriétaire, risque mesuré). L'index de sélection est
**lexical** (SQLite FTS5/BM25 pour la sélection, Python pour le score) :
aucun embedding, donc aucune dépendance à un service externe — l'application
reste installable depuis GitHub chez quelqu'un qui n'a ni LM Studio ni GPU.

Vocabulaire des effets de bord :

``none``        lecture seule ;
``write``       modifie l'état (création, mise à jour) ;
``destructive`` supprime, écrase ou exécute du code.

Niveaux de sécurité : ``low`` (lecture), ``medium`` (écriture réversible),
``high`` (écriture destructive ou exécution de code).
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from typing import Any, Iterable

from .db import Database
from .tokenizer import count_tokens

#: Effets de bord, du plus inoffensif au plus dangereux.
SIDE_EFFECTS = ("none", "write", "destructive")

#: Niveaux de sécurité.
SECURITY_LEVELS = ("low", "medium", "high")

#: Intentions reconnues : c'est l'axe principal de l'index de sélection.
INTENTS = ("search", "read", "create", "update", "delete", "execute", "navigate", "other")

#: Verbes d'écriture : l'outil modifie l'état.
_WRITE_HINTS = (
    "create", "add", "new", "post", "insert", "update", "edit", "patch", "modify",
    "set", "write", "put", "push", "merge", "comment", "review", "approve",
    "save", "install", "apply", "rename", "move", "copy", "upload", "send",
    "publish", "close", "reopen", "assign", "label", "book", "schedule",
)

#: Verbes destructeurs : l'appel est irréversible ou quasi.
_DESTRUCTIVE_HINTS = (
    "delete", "remove", "drop", "purge", "truncate", "uninstall", "destroy",
    "reset", "revoke", "wipe", "clear", "kill", "terminate", "erase", "detach",
)

#: Verbes d'exécution de code : le bac à sable est obligatoire.
_EXECUTE_HINTS = (
    "run", "exec", "execute", "script", "command", "shell", "spawn", "eval",
    "bash", "powershell", "eval",
)

#: Serveurs dont les outils agissent sur le système de fichiers.
_FS_SERVERS = ("fs", "filesystem", "files", "file")

#: Langues du lexique, dans l'ordre des colonnes de :data:`LEXICON`.
LANGUAGES = ("en", "fr", "es", "de", "it", "pt", "nl", "pl", "ru", "tr", "zh", "ja")

#: Lexique multilingue : concept -> formes de surface, **une colonne par
#: langue** (ordre : en, fr, es, de, it, pt, nl, pl, ru, tr, zh, ja).
#:
#: Sans embeddings, c'est ce qui fait le lien entre une intention formulée
#: dans n'importe quelle langue et un outil nommé en anglais : «创建一个问题 »
#: ou «创建一个 ticket » doivent atteindre ``create_issue``. Chaque concept
#: est indexé avec **toutes** ses formes, donc une requête dans l'une des
#: 12 langues atteint le même outil.
LEXICON: dict[str, str] = {
    "create": "create,new|creer,ajouter|crear,anadir|erstellen,hinzufugen|creare,aggiungere|criar,adicionar|maken,maak,aanmaken|utworz,dodaj|создать,добавить|olustur,ekle|创建,新建|作成,追加",
    "read": "read,open|lire,ouvrir|leer,abrir|lesen,offnen|leggere,aprire|ler,abrir|lezen,openen|czytac,otworz|читать,открыть|oku,ac|读取,打开|閲覧,開く,読む",
    "write": "write,save|ecrire,sauvegarder|escribir,guardar|schreiben,speichern|scrivere,salvare|escrever,guardar|schrijven,opslaan|zapisac,zapisz|писать,сохранить|yaz,kaydet|写入,保存|書込,保存,書く",
    "update": "update,edit|modifier,mettre a jour|actualizar,editar|aktualisieren,bearbeiten|aggiornare,modificare|atualizar,editar|bijwerken,bewerken|aktualizowac,edytowac|обновить,изменить|guncelle,duzenle|更新,编辑|更新,編集",
    "delete": "delete,remove|supprimer,effacer|eliminar,borrar|loschen,entfernen|eliminare,cancellare|apagar,excluir|verwijderen,wissen|usunac,skasowac|удалить,стереть|sil,kaldir|删除,移除|削除,除去",
    "search": "search,find|rechercher,chercher|buscar,encontrar|suchen,finden|cercare,trovare|pesquisar,buscar|zoeken,vinden|szukac,znalezc|искать,найти|ara,bul|搜索,查找|検索,探す",
    "list": "list,enumerate|lister,enumerer|listar,enumerar|auflisten,aufzahlen|elencare,listare|listar,enumerar|opsommen,lijsten|wymienic,wypisac|список,перечислить|listele,saymak|列出,枚举|一覧,列挙",
    "execute": "run,execute|executer,lancer|ejecutar,lanzar|ausfuhren,starten|eseguire,avviare|executar,iniciar|uitvoeren,starten|uruchomic,wykonac|запустить,выполнить|calistir,yurut|运行,执行|実行,起動",
    "download": "download,fetch|telecharger,recuperer|descargar,obtener|herunterladen,laden|scaricare,ottener|baixar,obter|downloaden,ophalen|pobrac,odczytac|скачать,загрузить|indir,indirme|下载,获取|ダウンロード,取得",
    "upload": "upload,attach|televerser,joindre|subir,adjuntar|hochladen,anhangen|caricare,allegare|enviar,anexar|uploaden,bijvoegen|wgrac,zalacz|загрузить,прикрепить|yukle,ekle|上传,附件|アップロード,添付",
    "comment": "comment,annotate|commenter,annoter|comentar,anotar|kommentieren,annotieren|commentare,annotare|comentar,anotar|reageren,annoteren|skomentowac,adnotowac|комментировать,примечание|yorum,not|评论,批注|コメント,注釈",
    "approve": "approve,accept|approuver,accepter|aprobar,aceptar|genehmigen,akzeptieren|approvare,accettare|aprovar,aceitar|goedkeuren,accepteren|zatwierdzic,akceptowac|утвердить,принять|onayla,kabul|批准,接受|承認,受理",
    "merge": "merge,combine|fusionner,combiner|fusionar,combinar|mergen,verbinden|unire,combinare|mesclar,combinar|samenvoegen,combineren|polaczyc,scal|объединить,слияние|birlestir,birlestir|合并,整合|マージ,統合",
    "navigate": "navigate,browse|naviguer,parcourir|navegar,recorrer|navigieren,durchsuchen|navigare,consultare|navegar,percorrer|navigeren,doorlopen|nawigowac,przegladac|навигация,просмотр|gezin,tara|浏览,导航|参照,ナビゲート",
    "deploy": "deploy,publish|deployer,publier|desplegar,publicar|ausliefern,veroffentlichen|distribuire,pubblicare|implantar,publicar|uitrollen,publiceren|wdrazac,publikowac|развернуть,публикация|dagit,yayinla|部署,发布|デプロイ,公開",
    "send": "send,message|envoyer,message|enviar,mensaje|senden,nachricht|inviare,messaggio|enviar,mensagem|verzenden,bericht|wyslac,wiadomosc|отправить,сообщение|gonder,mesaj|发送,消息|送信,メッセージ",
    "move": "move,rename|deplacer,renommer|mover,renombrar|verschieben,umbenennen|spostare,rinominare|mover,renomear|verplaatsen,hernoemen|przeniesc,zmienic nazwe|переместить,переименовать|tasi,yeniden adlandir|移动,重命名|移動,改名",
    "copy": "copy,duplicate|copier,dupliquer|copiar,duplicar|kopieren,duplizieren|copiare,duplicare|copiar,duplicar|kopieren,dupliceren|kopiowac,duplikowac|копировать,дублировать|kopyala,ikile|复制,克隆|コピー,複製",
    "export": "export,dump|exporter,dumper|exportar,vaciar|exportieren,dumpen|esportare,scaricare|exportar,despejar|exporteren,dumpen|eksportowac,zrzucic|экспорт,дамп|disa aktar,dok|导出,转储|エクスポート,出力",
    "import": "import,load|importer,charger|importar,cargar|importieren,laden|importare,caricare|importar,carregar|importeren,laden|importowac,wczytac|импорт,загрузка|ice aktar,yukle|导入,加载|インポート,取込",
    "monitor": "monitor,watch|surveiller,observer|monitorizar,observar|uberwachen,beobachten|monitorare,osservare|monitorar,observar|monitoren,observeren|monitorowac,obserwowac|мониторинг,наблюдение|izle,gozlem|监控,观察|監視,状態確認",
    "test": "test,check|tester,verifier|probar,comprobar|testen,prufen|testare,verificare|testar,verificar|testen,controleren|testowac,sprawdzic|тест,проверка|test et,denetle|测试,检查|テスト,検証",
    "file": "file,files|fichier,fichiers|archivo,archivos|datei,dateien|file,file|arquivo,arquivos|bestand,bestanden|plik,pliki|файл,файлы|dosya,dosyalar|文件,档案|ファイル,書類",
    "repo": "repo,repository|depot,depot git|repositorio,repositorio|repository,repository|repository,repository|repositorio,repositorio|repository,repository|repozytorium,repo|репозиторий,репозиторий|depo,depo|仓库,存储库|リポジトリ",
    "issue": "issue,ticket|ticket,issue|incidencia,incidencia|issue,ticket|issue,ticket|issue,chamado|issue,ticket|issue,zgloszenie|задача,баг|issue,is|问题,议题|課題,チケット",
    "branch": "branch|branche|rama|branch|ramo|branch,tamo|branch,tak|gałaz,branch|ветка,branch|dal|分支,分支|ブランチ,枝",
    "commit": "commit|commit,commit|commit|commit|commit|commit|commit|commit|commit|commit|提交,提交|コミット",
    "database": "database,db|base de donnees,bd|base de datos,bd|datenbank,db|database,db|banco de dados,bd|database,db|baza danych,bd|база данных,бд|veritabani,db|数据库,数据库|データベース,DB",
    "server": "server|serveur|servidor|server|server|servidor|server|serwer|сервер,сервер|sunucu|服务器,服务器|サーバー,サーバ",
    "container": "container,docker|conteneur,docker|contenedor,docker|container,docker|container,docker|container,docker|container,docker|kontener,docker|контейнер,docker|konteyner,docker|容器,容器|コンテナ,コンテナ",
    "message": "message|message|mensaje|nachricht|messaggio|mensagem|bericht|wiadomosc|сообщение|mesaj|消息,消息|メッセージ,通知",
    "user": "user|utilisateur|usuario|benutzer|utente|usuario|gebruiker|uzytkownik|пользователь|kullanici|用户,用户|ユーザー,利用者",
    "project": "project|projet|proyecto|projekt|progetto|projeto|project|projekt|проект|proje|项目,项目|プロジェクト,案件",
    "document": "document,doc|document,doc|documento,doc|dokument,doc|documento,doc|documento,doc|document,doc|dokument,doc|документ,doc|belge,doc|文档,文档|ドキュメント,文書",
    "page": "page,website|page,site web|pagina,sitio web|seite,webseite|pagina,sito web|pagina,site|pagina,website|strona,strona|страница,сайт|sayfa,web sitesi|页面,网页|ページ,画面",
    "terminal": "terminal,shell|terminal,shell|terminal,shell|terminal,shell|terminale,shell|terminal,shell|terminal,shell|terminal,shell|terminal,shell|terminal,shell|终端,终端|ターミナル,端末",
}


def expand_terms(tokens: list[str]) -> set[str]:
    """Enveloppe les tokens dans leurs groupes de synonymes."""
    out = set(tokens)
    for group in SYNONYM_GROUPS:
        if any(token in group for token in tokens):
            out.update(group)
    return out

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
#: Chinois, japonais, coréen : ces langues n'ont pas d'espaces.
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def strip_accents(text: str) -> str:
    """Minuscules sans diacritiques : la recherche lexicale ignore les accents."""
    folded = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()


def words(text: str) -> list[str]:
    """Mots alphanumériques (accents inclus).

    Chinois et japonais n'ont pas d'espaces : on ajoute des **bigrammes**
    pour qu'un segment comme ``读取文件`` livre ``读取`` et ``文件``.
    """
    out: list[str] = []
    for token in _WORD_RE.findall(text or ""):
        out.append(token)
        if _CJK_RE.search(token):
            chars = [ch for ch in token if _CJK_RE.match(ch)]
            out.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return out


def split_identifier(name: str) -> list[str]:
    """``createIssue`` / ``create_issue`` -> ``['create', 'issue']``."""
    parts: list[str] = []
    for chunk in _CAMEL_RE.split(name or ""):
        parts.extend(w.lower() for w in words(chunk))
    return parts


def _hits(hints: tuple[str, ...], tokens: list[str]) -> bool:
    return any(h in tokens for h in hints)


def _parse_lexicon() -> dict[str, set[str]]:
    """Déplie le lexique : concept -> ensemble de formes (sans accents)."""
    out: dict[str, set[str]] = {}
    for concept, blob in LEXICON.items():
        forms: set[str] = set()
        for column in blob.split("|"):
            for term in column.split(","):
                term = strip_accents(term.strip())
                if term:
                    forms.add(term)
        out[concept] = forms
    return out


#: concept -> toutes ses formes, dans toutes les langues.
CONCEPTS: dict[str, set[str]] = _parse_lexicon()

#: forme -> concepts qui la revendiquent.
TERM_CONCEPTS: dict[str, set[str]] = {}
for _concept, _forms in CONCEPTS.items():
    for _form in _forms:
        TERM_CONCEPTS.setdefault(_form, set()).add(_concept)


def concept_terms(tokens: Iterable[str]) -> set[str]:
    """Toutes les formes (toutes langues) des concepts visés par les tokens."""
    out: set[str] = set()
    for token in tokens:
        for concept in TERM_CONCEPTS.get(strip_accents(token), ()):
            out |= CONCEPTS[concept]
            out.add(concept)
    return out


def infer_intent(name: str, description: str = "") -> str:
    """Intention principale d'un outil, d'après son nom puis sa description.

    Le lexique est multilingue : ``create_issue``, ``creer_ticket`` et
    ``作成する`` tombent dans la même intention.
    """
    tokens = set(split_identifier(name))
    concepts = {c for token in tokens
                for c in TERM_CONCEPTS.get(strip_accents(token), ())}
    for intent in ("delete", "execute", "create", "update", "search",
                   "navigate", "read"):
        if intent in concepts:
            return intent
    if "write" in concepts:
        return "update"
    if "download" in concepts or "export" in concepts:
        return "read"
    if "list" in concepts:
        return "search"
    for intent, hints in (
        ("delete", _DESTRUCTIVE_HINTS),
        ("execute", _EXECUTE_HINTS),
        ("create", ("create", "add", "new", "post", "insert", "open", "publish")),
        ("update", ("update", "edit", "patch", "modify", "set", "write", "put")),
        ("search", ("search", "find", "query", "grep", "lookup", "list", "filter")),
        ("read", ("read", "get", "fetch", "show", "view", "describe", "cat")),
    ):
        if _hits(hints, list(tokens)):
            return intent
    desc = set(split_identifier(description))
    desc_concepts = {c for token in desc
                     for c in TERM_CONCEPTS.get(strip_accents(token), ())}
    for intent in ("delete", "execute", "create", "search"):
        if intent in desc_concepts:
            return intent
    return "other"


def infer_side_effects(name: str, description: str = "") -> str:
    """Effets de bord : ``none``, ``write`` ou ``destructive``."""
    tokens = set(split_identifier(name))
    concepts = {c for token in tokens
                for c in TERM_CONCEPTS.get(strip_accents(token), ())}
    if "test" in concepts and not (concepts & {"delete", "create", "update",
                                               "write"}):
        return "none"
    if concepts & {"delete", "execute"}:
        return "destructive"
    if concepts & {"create", "update", "write", "upload", "send", "deploy",
                   "move", "copy", "merge", "approve", "comment", "import",
                   "export"}:
        return "write"
    if _hits(_DESTRUCTIVE_HINTS, list(tokens)):
        return "destructive"
    if _hits(_WRITE_HINTS, list(tokens)):
        return "write"
    desc = set(split_identifier(description))
    if _hits(_DESTRUCTIVE_HINTS, list(desc)):
        return "destructive"
    return "none"


def infer_security_level(side_effects: str) -> str:
    """Le niveau de sécurité découle de l'effet de bord."""
    if side_effects == "destructive":
        return "high"
    if side_effects == "write":
        return "medium"
    return "low"


def required_inputs(schema: dict[str, Any]) -> list[str]:
    """Noms des arguments obligatoires d'un schéma."""
    if not isinstance(schema, dict):
        return []
    required = schema.get("required")
    if not isinstance(required, list):
        return []
    return [str(name) for name in required]


def build_tags(name: str, description: str, domain: str, intent: str) -> str:
    """Capacités indexables : nom éclaté, formes multilingues, domaine, résumé.

    Chaque concept reconnu apporte **toutes** ses formes (les 12 langues) :
    c'est ce qui rend la sélection indépendante de la langue de la requête.
    """
    tokens = split_identifier(name)
    if domain:
        tokens += split_identifier(domain)
    if intent:
        tokens.append(intent)
    tags: list[str] = list(tokens)
    tags.extend(concept_terms(tokens))
    summary = words(description)[:60]
    tags.extend(concept_terms(summary))
    for word in summary:
        if len(word) > 3 or _CJK_RE.search(word):
            tags.append(word.lower())
    seen: dict[str, None] = {}
    for tag in tags:
        key = strip_accents(tag)
        if key and key not in seen:
            seen[key] = None
    return " ".join(seen)


def _fts5_available() -> bool:
    """FTS5 est-il compilé dans ce SQLite ? (sinon : repli sans index)."""
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
        finally:
            conn.close()
        return True
    except sqlite3.Error:
        return False


class ToolRegistry:
    """Catalogue unique des outils, avec sélection lexicale et contrats."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.fts = _fts5_available()

    # -- écriture --------------------------------------------------------
    def register(self, server: str, tools: list[dict[str, Any]],
                 schemas: dict[str, Any], *, domain: str = "",
                 owners: dict[str, str] | None = None,
                 environment: str = "") -> int:
        """Enregistre les outils d'un serveur (mise à jour idempotente)."""
        if not tools:
            return 0
        owners = owners or {}
        domain = domain or server
        rows = []
        for tool in tools:
            name = str(tool.get("name") or "")
            if not name:
                continue
            description = str(tool.get("description") or "")
            schema = schemas.get(name) or {}
            intent = infer_intent(name, description)
            side_effects = infer_side_effects(name, description)
            rows.append({
                "server": server,
                "tool": name,
                "qualified_name": f"{server}.{name}",
                "description": description,
                "input_schema": json.dumps(schema, ensure_ascii=False, default=str),
                "output_schema": json.dumps(tool.get("outputSchema") or {},
                                            ensure_ascii=False, default=str),
                "domain": domain,
                "intent": intent,
                "tags": build_tags(name, description, domain, intent),
                "required_inputs": " ".join(required_inputs(schema)),
                "side_effects": side_effects,
                "security_level": infer_security_level(side_effects),
                "scope": "*",
                "owner": owners.get(server, ""),
                "risk": infer_security_level(side_effects),
                "environment": environment,
            })
        if not rows:
            return 0
        with self.db.transaction() as cur:
            for row in rows:
                old = cur.execute(
                    "SELECT fid FROM tool_registry WHERE server=? AND tool=?",
                    (row["server"], row["tool"]),
                ).fetchone()
                if old is not None and old["fid"] and self.fts:
                    cur.execute("DELETE FROM tool_registry_fts WHERE rowid=?", (old["fid"],))
                cur.execute(
                    "INSERT INTO tool_registry (server,tool,qualified_name,description,"
                    "input_schema,output_schema,domain,intent,tags,required_inputs,"
                    "side_effects,security_level,scope,owner,risk,environment,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))"
                    " ON CONFLICT(server,tool) DO UPDATE SET qualified_name=excluded.qualified_name,"
                    " description=excluded.description, input_schema=excluded.input_schema,"
                    " output_schema=excluded.output_schema, domain=excluded.domain,"
                    " intent=excluded.intent, tags=excluded.tags,"
                    " required_inputs=excluded.required_inputs,"
                    " side_effects=excluded.side_effects,"
                    " security_level=excluded.security_level, scope=excluded.scope,"
                    " owner=excluded.owner, risk=excluded.risk,"
                    " environment=excluded.environment, updated_at=datetime('now')",
                    (row["server"], row["tool"], row["qualified_name"], row["description"],
                     row["input_schema"], row["output_schema"], row["domain"], row["intent"],
                     row["tags"], row["required_inputs"], row["side_effects"],
                     row["security_level"], row["scope"], row["owner"], row["risk"],
                     row["environment"]),
                )
                if self.fts:
                    cur.execute(
                        "INSERT INTO tool_registry_fts (qualified_name,description,tags,"
                        "intent,domain,server,tool) VALUES (?,?,?,?,?,?,?)",
                        (strip_accents(row["qualified_name"]),
                         strip_accents(row["description"]),
                         strip_accents(row["tags"]),
                         row["intent"], row["domain"], row["server"], row["tool"]),
                    )
                    cur.execute(
                        "UPDATE tool_registry SET fid=? WHERE server=? AND tool=?",
                        (cur.lastrowid, row["server"], row["tool"]),
                    )
        return len(rows)

    def record_call(self, server: str, tool: str, latency_ms: int) -> None:
        """Mémorise l'usage réel : nombre d'appels et latence moyenne."""
        with self.db.transaction() as cur:
            cur.execute(
                "UPDATE tool_registry SET calls=calls+1,"
                " avg_latency_ms=((avg_latency_ms*calls)+?) / (calls+1)"
                " WHERE server=? AND tool=?",
                (int(latency_ms), server, tool),
            )

    # -- lecture ---------------------------------------------------------
    def get(self, server: str, tool: str) -> dict[str, Any] | None:
        with self.db.cursor(commit=False) as cur:
            row = cur.execute(
                "SELECT * FROM tool_registry WHERE server=? AND tool=?",
                (server, tool),
            ).fetchone()
        return self._row(row) if row is not None else None

    def list_all(self, *, server: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM tool_registry"
        params: tuple[Any, ...] = ()
        if server:
            sql += " WHERE server=?"
            params = (server,)
        sql += " ORDER BY server, tool"
        with self.db.cursor(commit=False) as cur:
            rows = cur.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        with self.db.cursor(commit=False) as cur:
            total = cur.execute("SELECT COUNT(*) FROM tool_registry").fetchone()[0]
            by_server = cur.execute(
                "SELECT server, COUNT(*) AS n FROM tool_registry GROUP BY server"
            ).fetchall()
            by_effect = cur.execute(
                "SELECT side_effects, COUNT(*) AS n FROM tool_registry GROUP BY side_effects"
            ).fetchall()
            by_intent = cur.execute(
                "SELECT intent, COUNT(*) AS n FROM tool_registry GROUP BY intent"
            ).fetchall()
        return {
            "tools": int(total),
            "fts": self.fts,
            "by_server": {row["server"]: int(row["n"]) for row in by_server},
            "by_side_effects": {row["side_effects"]: int(row["n"]) for row in by_effect},
            "by_intent": {row["intent"]: int(row["n"]) for row in by_intent},
        }

    def _row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("input_schema", "output_schema"):
            try:
                data[key.replace("_schema", "")] = json.loads(data.pop(key) or "{}")
            except (ValueError, TypeError):
                data[key.replace("_schema", "")] = {}
        return data

    # -- sélection -------------------------------------------------------
    def search(self, query: str, *, server: str = "", top_k: int = 10,
               max_per_server: int = 3, side_effects: str | None = None,
               intent: str = "", domain: str = "") -> list[dict[str, Any]]:
        """Sélectionne les outils pertinents pour une intention formulée en langage naturel.

        L'index lexical (FTS5) sert à **éliminer** les candidats hors sujet ;
        le score final est calculé en Python pour tenir compte de l'intention,
        du domaine et de l'effet de bord demandé.
        """
        terms = [strip_accents(w) for w in words(query)]
        terms = [t for t in terms if len(t) > 1]
        if not terms:
            return []
        # La requête n'est PAS élargie aux synonymes : c'est l'index qui
        # porte les variantes. La précision de la requête reste intacte.
        terms = sorted(set(terms))
        candidates = self._candidates(terms, server)
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in candidates:
            if side_effects and row["side_effects"] != side_effects:
                continue
            if intent and row["intent"] != intent:
                continue
            if domain and row["domain"] != domain:
                continue
            score = self._score(row, terms, query)
            if score <= 0:
                continue
            scored.append((score, row))
        scored.sort(key=lambda item: (-item[0], item[1]["qualified_name"]))
        out: list[dict[str, Any]] = []
        per_server: dict[str, int] = {}
        seen: set[tuple[str, str]] = set()
        for score, row in scored:
            key = (row["server"], row["tool"])
            if key in seen:
                continue
            seen.add(key)
            seen_count = per_server.get(row["server"], 0)
            if seen_count >= max_per_server:
                continue
            per_server[row["server"]] = seen_count + 1
            entry = {
                "name": row["qualified_name"],
                "server": row["server"],
                "tool": row["tool"],
                "description": row["description"][:280],
                "intent": row["intent"],
                "domain": row["domain"],
                "side_effects": row["side_effects"],
                "security_level": row["security_level"],
                "required_inputs": row["required_inputs"],
                "score": round(score, 3),
            }
            out.append(entry)
            if len(out) >= top_k:
                break
        return out

    def _candidates(self, terms: list[str], server: str) -> list[dict[str, Any]]:
        """Candidats FTS5 si disponible, sinon tout le catalogue."""
        if self.fts:
            match = " OR ".join('"%s"' % t.replace('"', '""') for t in terms)
            sql = ("SELECT r.* FROM tool_registry_fts f JOIN tool_registry r"
                   " ON r.server=f.server AND r.tool=f.tool"
                   " WHERE f.tool_registry_fts MATCH ?")
            params: list[Any] = [match]
            if server:
                sql += " AND f.server=?"
                params.append(server)
            sql += " LIMIT 400"
            try:
                with self.db.cursor(commit=False) as cur:
                    rows = cur.execute(sql, params).fetchall()
                return [self._row(row) for row in rows]
            except sqlite3.Error:
                pass
        return self.list_all(server=server)

    def _score(self, row: dict[str, Any], terms: list[str], query: str) -> float:
        """Score de pertinence : nom > tags > description, plus bonus d'intention."""
        name_tokens = set(split_identifier(row["tool"]))
        tag_tokens = set(row["tags"].split())
        desc_tokens = set(words(row["description"]))
        score = 0.0
        for term in terms:
            if term in name_tokens:
                score += 5.0
            elif term in tag_tokens:
                score += 3.0
            elif term in desc_tokens:
                score += 1.5
        if row["intent"] in terms:
            score += 4.0
        if row["domain"] in terms:
            score += 2.0
        if row["qualified_name"].lower() == (query or "").strip().lower():
            score += 20.0
        return score

    # -- contrat d'outil -------------------------------------------------
    def contract(self, server: str, tool: str, *,
                 max_description_chars: int = 160,
                 compress: bool = True) -> dict[str, Any] | None:
        """Contrat réduit d'un outil : ce que l'agent voit.

        Le schéma complet reste dans le registre et est réinjecté au moment
        de l'appel réel : l'agent n'a jamais besoin du JSON Schema complet.
        """
        from .toolcompress import compress_schema

        row = self.get(server, tool)
        if row is None:
            return None
        schema = row.get("input") or {}
        shown = compress_schema(schema, max_description_chars) if compress else schema
        return {
            "name": row["qualified_name"],
            "description": row["description"][:max_description_chars],
            "arguments": shown,
            "required": required_inputs(schema),
            "intent": row["intent"],
            "domain": row["domain"],
            "side_effects": row["side_effects"],
            "security_level": row["security_level"],
            "scope": row["scope"],
            "owner": row["owner"],
        }

    def schema_tokens(self, server: str = "") -> dict[str, int]:
        """Tokens que coûteraient les schémas exposés, serveur par serveur."""
        out: dict[str, int] = {}
        for row in self.list_all(server=server):
            tokens = count_tokens(json.dumps(row.get("input") or {}, ensure_ascii=False))
            out[row["server"]] = out.get(row["server"], 0) + tokens
        return out

    def fs_servers(self) -> list[str]:
        """Serveurs dont les outils touchent le système de fichiers."""
        return sorted({row["server"] for row in self.list_all()
                       if row["server"].lower() in _FS_SERVERS})
