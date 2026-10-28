"""Base SQLite unique d'IcyLoom.

Un seul fichier, un seul schéma, un seul point d'entrée d'écriture.

Choix structurants
------------------
* **WAL + `busy_timeout`** : la passerelle HTTP et le serveur MCP sont deux
  processus distincts qui écrivent dans le même fichier.
* **Connexions toujours fermées** : un ``with sqlite3.connect(...)`` valide la
  transaction mais ne ferme pas le descripteur ; sous Windows cela verrouille
  le fichier. Ici la fermeture est explicite et systématique.
* **DDL idempotent et additif** : ``CREATE TABLE IF NOT EXISTS`` puis
  ``ALTER TABLE ADD COLUMN`` pour les colonnes manquantes. Jamais de
  ``DROP``/``RECREATE`` : la donnée existante ne doit jamais être perdue.
* **Tables au nom distinct** : le journal LLM et le journal de préfixe ne
  peuvent pas partager un nom de table (colonnes incompatibles).
* **Un ledger unique** : `ledger_entries` enregistre tout ce qui coûte et tout
  ce qui économise ; c'est la seule source pour la question économique.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .config import Config

SCHEMA_VERSION = 1

#: Schéma de la base. Les requêtes sont volontairement explicites (pas de
#: migration automatique de types) pour rester lisibles et prévisibles.
SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # --- journal des appels LLM (tokens REELS remontés par l'upstream) ------
    """
    CREATE TABLE IF NOT EXISTS llm_events (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        ts                TEXT NOT NULL DEFAULT (datetime('now')),
        kind              TEXT NOT NULL,
        cache_key         TEXT,
        provider          TEXT,
        model             TEXT,
        stream            INTEGER NOT NULL DEFAULT 0,
        prompt_tokens     INTEGER,
        completion_tokens INTEGER,
        saved_prompt      INTEGER NOT NULL DEFAULT 0,
        saved_completion  INTEGER NOT NULL DEFAULT 0,
        latency_ms        INTEGER,
        upstream          TEXT,
        client            TEXT,
        detail            TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_llm_events_ts ON llm_events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_llm_events_kind ON llm_events(kind)",
    "CREATE INDEX IF NOT EXISTS idx_llm_events_upstream ON llm_events(upstream)",
    # --- journal du cache de préfixe (schéma distinct du journal LLM) -------
    """
    CREATE TABLE IF NOT EXISTS prefix_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT NOT NULL DEFAULT (datetime('now')),
        provider    TEXT,
        model       TEXT,
        stream      INTEGER NOT NULL DEFAULT 0,
        status      INTEGER,
        injected    TEXT,
        total_in    INTEGER NOT NULL DEFAULT 0,
        cache_read  INTEGER NOT NULL DEFAULT 0,
        cache_write INTEGER NOT NULL DEFAULT 0,
        out_tokens  INTEGER NOT NULL DEFAULT 0,
        latency_ms  INTEGER,
        measured    INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_prefix_events_ts ON prefix_events(ts)",
    # --- cache LLM (TTL + LRU) ---------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS cache_entries (
        key             TEXT PRIMARY KEY,
        normalized      TEXT NOT NULL,
        original        TEXT,
        response        TEXT NOT NULL,
        is_stream       INTEGER NOT NULL DEFAULT 0,
        provider        TEXT,
        model           TEXT,
        raw_hash        TEXT,
        prompt_tokens     INTEGER,
        completion_tokens INTEGER,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        last_accessed   TEXT NOT NULL DEFAULT (datetime('now')),
        access_count    INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cache_entries_created ON cache_entries(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_cache_entries_accessed ON cache_entries(last_accessed)",
    # --- cache des résultats d'outils ---------------------------------------
    """
    CREATE TABLE IF NOT EXISTS tool_cache (
        key            TEXT PRIMARY KEY,
        server         TEXT NOT NULL,
        tool           TEXT NOT NULL,
        result_json    TEXT NOT NULL,
        bytes          INTEGER NOT NULL DEFAULT 0,
        latency_ms     INTEGER NOT NULL DEFAULT 0,
        ttl            INTEGER NOT NULL DEFAULT 3600,
        created_at     TEXT NOT NULL DEFAULT (datetime('now')),
        last_accessed  TEXT NOT NULL DEFAULT (datetime('now')),
        hits           INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tool_cache_server ON tool_cache(server)",
    # --- journal des appels d'outils ----------------------------------------
    """
    CREATE TABLE IF NOT EXISTS tool_events (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        ts               TEXT NOT NULL DEFAULT (datetime('now')),
        kind             TEXT NOT NULL,
        server           TEXT,
        tool             TEXT,
        client           TEXT,
        args_hash        TEXT,
        bytes            INTEGER NOT NULL DEFAULT 0,
        latency_ms       INTEGER NOT NULL DEFAULT 0,
        saved_latency_ms INTEGER NOT NULL DEFAULT 0,
        detail           TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tool_events_ts ON tool_events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_tool_events_kind ON tool_events(kind)",
    # --- tiroir de contexte -------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS context_sessions (
        session_id TEXT PRIMARY KEY,
        label      TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        ttl_days   INTEGER NOT NULL DEFAULT 30
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS context_sources (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id     TEXT NOT NULL,
        label          TEXT NOT NULL DEFAULT '',
        digest         TEXT NOT NULL,
        byte_size      INTEGER NOT NULL DEFAULT 0,
        chunk_count    INTEGER NOT NULL DEFAULT 0,
        first_chunk_id INTEGER,
        last_chunk_id  INTEGER,
        tokens_before  INTEGER NOT NULL DEFAULT 0,
        anchor_tokens  INTEGER NOT NULL DEFAULT 0,
        redactions     INTEGER NOT NULL DEFAULT 0,
        created_at     REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_context_sources_session ON context_sources(session_id)",
    """
    CREATE TABLE IF NOT EXISTS context_chunks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT NOT NULL,
        source_digest TEXT NOT NULL,
        chunk_index INTEGER NOT NULL,
        label       TEXT NOT NULL DEFAULT '',
        body        TEXT NOT NULL,
        tokens      INTEGER NOT NULL DEFAULT 0,
        created_at  REAL NOT NULL,
        hits        INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_context_chunks_session ON context_chunks(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_context_chunks_source ON context_chunks(source_digest, chunk_index)",
    """
    CREATE TABLE IF NOT EXISTS context_invariants (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id  INTEGER NOT NULL,
        session_id TEXT NOT NULL,
        kind       TEXT NOT NULL,
        value      TEXT NOT NULL,
        priority   INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_context_invariants_source ON context_invariants(source_id)",
    """
    CREATE TABLE IF NOT EXISTS context_anchors (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        source_id  INTEGER,
        label      TEXT NOT NULL DEFAULT '',
        body       TEXT NOT NULL,
        tokens     INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_context_anchors_session ON context_anchors(session_id)",
    # --- ledger : une seule source pour les coûts et les gains -------------
    """
    CREATE TABLE IF NOT EXISTS ledger_entries (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        ts               TEXT NOT NULL DEFAULT (datetime('now')),
        kind             TEXT NOT NULL,
        session_id       TEXT,
        client           TEXT,
        upstream         TEXT,
        model            TEXT,
        tokens_in        INTEGER NOT NULL DEFAULT 0,
        tokens_out       INTEGER NOT NULL DEFAULT 0,
        tokens_saved     INTEGER NOT NULL DEFAULT 0,
        tool_tokens_saved INTEGER NOT NULL DEFAULT 0,
        latency_ms       INTEGER NOT NULL DEFAULT 0,
        saved_latency_ms INTEGER NOT NULL DEFAULT 0,
        meta             TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger_entries(ts)",
    "CREATE INDEX IF NOT EXISTS idx_ledger_kind ON ledger_entries(kind)",
)

#: Le registre unifié (`MERGE_PLAN.md` §4.3) : une **vue**, pas une table.
#:
#: Elle croise les trois domaines dans un schéma unique et honnête :
#:
#: * ``tokens_paid``  = tokens **réellement** envoyés à l'amont par ce domaine ;
#: * ``tokens_saved`` = tokens **évités** grâce à IcyLoom (jamais estimés).
#:
#: Pour chaque ligne, ``tokens_paid + tokens_saved`` reconstitue le coût
#: qu'aurait payé la même opération sans IcyLoom : c'est la base mesurable du
#: critère « ≥ 35 % de tokens en moins ».
#:
#: La vue est ``CREATE VIEW IF NOT EXISTS`` : elle n'écrit rien, elle ne peut
#: donc jamais perdre ni altérer la donnée d'origine.
VIEWS: tuple[str, ...] = (
    """
    CREATE VIEW IF NOT EXISTS v_token_ledger AS
    -- 1. CACHE LLM : un « hit » n'envoie rien à l'amont (tokens évités),
    --    un « miss » paie les tokens réels remontés par l'amont.
    SELECT 'cache'                      AS domain,
           ts                            AS ts,
           CASE WHEN kind = 'miss'
                THEN COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)
                ELSE 0 END               AS tokens_paid,
           CASE WHEN kind = 'hit'
                THEN COALESCE(saved_prompt, 0) + COALESCE(saved_completion, 0)
                ELSE 0 END               AS tokens_saved,
           COALESCE(latency_ms, 0)       AS latency_ms,
           0                             AS saved_latency_ms
    FROM llm_events
    UNION ALL
    -- 2. COMPRESSION : on a payé l'ancre, on a évité le reste.
    SELECT 'compression'                AS domain,
           datetime(created_at, 'unixepoch') AS ts,
           COALESCE(anchor_tokens, 0)    AS tokens_paid,
           COALESCE(tokens_before, 0) - COALESCE(anchor_tokens, 0) AS tokens_saved,
           0                             AS latency_ms,
           0                             AS saved_latency_ms
    FROM context_sources
    UNION ALL
    -- 3. TAX D'OUTILS : le cache d'outils évite de rejouer l'appel (latence).
    SELECT 'tools'                      AS domain,
           ts                            AS ts,
           0                             AS tokens_paid,
           0                             AS tokens_saved,
           COALESCE(latency_ms, 0)       AS latency_ms,
           COALESCE(saved_latency_ms, 0) AS saved_latency_ms
    FROM tool_events
    WHERE kind = 'tool_hit'
    """,
)

#: Colonnes ajoutées après coup (migration additive, jamais de DROP).
ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("llm_events", "client", "TEXT"),
    ("llm_events", "detail", "TEXT"),
    ("llm_events", "cache_key", "TEXT"),
    ("cache_entries", "raw_hash", "TEXT"),
    ("cache_entries", "prompt_tokens", "INTEGER"),
    ("cache_entries", "completion_tokens", "INTEGER"),
    ("tool_events", "args_hash", "TEXT"),
    ("tool_cache", "canon_key", "TEXT"),
)

#: Index qui dépendent de colonnes additives (exécutés après ADDITIVE_COLUMNS).
POST_MIGRATION_INDEXES: tuple[str, ...] = (
    # clé canonique : sert les appels « presque identiques »
    "CREATE INDEX IF NOT EXISTS idx_tool_cache_canon ON tool_cache(canon_key)",
    # Index d'expression : les agrégats du dashboard groupent par jour et par
    # heure. Sans ces index, SQLite construit une table temporaire à chaque
    # GROUP BY (≈ 0,5 s sur 70 k lignes) ; avec eux, le groupement est
    # satisfait par l'index trié. `ts` est inclus pour que le filtre de
    # fenêtre temporelle reste dans l'index (lecture couverte, pas de retour
    # à la table).
    "CREATE INDEX IF NOT EXISTS idx_llm_events_day_hour"
    " ON llm_events(substr(ts, 1, 10), substr(ts, 12, 2),"
    " ts, kind, saved_prompt, saved_completion, prompt_tokens, completion_tokens)",
    "CREATE INDEX IF NOT EXISTS idx_ledger_entries_day"
    " ON ledger_entries(substr(ts, 1, 10), ts, kind, tokens_in, tokens_out,"
    " tokens_saved, tool_tokens_saved)",
    "CREATE INDEX IF NOT EXISTS idx_tool_events_day_hour"
    " ON tool_events(substr(ts, 1, 10), substr(ts, 12, 2), ts, kind, latency_ms,"
    " saved_latency_ms)",
)


class Database:
    """Accès à la base unique : schéma garanti, transactions, fermeture sure."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.path: Path = config.db_path()
        self._lock = threading.RLock()

    # -- connexion --------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        """Nouvelle connexion configurée (à fermer impérativement)."""
        conn = sqlite3.connect(str(self.path), timeout=self.config.database.busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self.config.database.busy_timeout_ms}")
        conn.execute(f"PRAGMA journal_mode={self.config.database.journal_mode}")
        conn.execute(f"PRAGMA synchronous={self.config.database.synchronous}")
        conn.execute("PRAGMA foreign_keys=ON")
        # Les agrégats du dashboard matérialisent la vue unifiée en table
        # temporaire : en mémoire, c'est ordre de grandeur plus rapide.
        conn.execute("PRAGMA temp_store=MEMORY")
        return conn

    @contextmanager
    def cursor(self, commit: bool = True) -> Iterator[sqlite3.Cursor]:
        """Curseur sur une connexion fermée à la sortie (même en cas d'erreur)."""
        conn = self.connect()
        try:
            cur = conn.cursor()
            yield cur
            if commit:
                conn.commit()
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        """Transaction explicite : commit à la sortie, rollback sur exception."""
        conn = self.connect()
        try:
            cur = conn.cursor()
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- schéma ------------------------------------------------------------
    def ensure_schema(self) -> None:
        """Crée le schéma si besoin et ajoute les colonnes manquantes."""
        with self._lock, self.cursor() as cur:
            for statement in SCHEMA:
                cur.execute(statement)
            for statement in VIEWS:
                cur.execute(statement)
            for table, column, decl in ADDITIVE_COLUMNS:
                existing = {r[1] for r in cur.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            # Index sur colonnes additives : créés APRES la migration, sinon une
            # base neuve plante (« no such column: canon_key »).
            for statement in POST_MIGRATION_INDEXES:
                cur.execute(statement)
            cur.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    def table_names(self) -> list[str]:
        with self.cursor(commit=False) as cur:
            rows = cur.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        return [r[0] for r in rows]

    def has_table(self, name: str) -> bool:
        with self.cursor(commit=False) as cur:
            row = cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
        return row is not None

    def journal_mode(self) -> str:
        with self.cursor(commit=False) as cur:
            return str(cur.execute("PRAGMA journal_mode").fetchone()[0])

    def busy_timeout_ms(self) -> int:
        with self.cursor(commit=False) as cur:
            return int(cur.execute("PRAGMA busy_timeout").fetchone()[0])

    def count(self, table: str, where: str = "", params: Sequence[Any] = ()) -> int:
        if not self.has_table(table):
            return 0
        clause = f" WHERE {where}" if where else ""
        with self.cursor(commit=False) as cur:
            return int(cur.execute(f"SELECT COUNT(*) FROM {table}{clause}", tuple(params)).fetchone()[0])

    def __enter__(self) -> "Database":
        self.ensure_schema()
        return self

    def __exit__(self, *exc: object) -> None:
        return None
