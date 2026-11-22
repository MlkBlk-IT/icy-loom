"""Cache semantique (TideCache-adapte, niveau L2).

1. EmbeddingClient : embeddings via LM Studio (OpenAI-compatible).
2. SemanticIndex   : stocke les vecteurs dans semantic_cache et repond a une
   requete "meme sens, autres mots" par similarite cosinus.
CPU-only, stdlib pur (array + math), aucune dependance.
"""
from __future__ import annotations

import array
import json
import math
import threading
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class EmbeddingConfig:
    url: str = "http://127.0.0.1:1234/v1/embeddings"
    model: str = "text-embedding-qwen3-embedding-4b"
    timeout: float = 20.0
    dim: int = 0


def _to_vec(seq):
    v = array.array("f", seq)
    n = math.sqrt(sum(x * x for x in v))
    if n > 0.0:
        v = array.array("f", (x / n for x in v))
    return v


def _pack(v):
    return v.tobytes()


def _unpack(blob):
    a = array.array("f")
    a.frombytes(blob)
    return a


def _cos(a, b):
    if len(a) != len(b):
        return -1.0
    s = 0.0
    for x, y in zip(a, b):
        s += x * y
    return s  # vecteurs deja normalises -> dot == cosine


class EmbeddingClient:
    """Client minimal d embeddings OpenAI-compatible (LM Studio)."""

    def __init__(self, cfg=None):
        self.cfg = cfg or EmbeddingConfig()
        self._dim = self.cfg.dim
        self._lock = threading.Lock()

    @property
    def dim(self):
        return self._dim

    def embed(self, text):
        payload = json.dumps({"model": self.cfg.model, "input": text}).encode("utf-8")
        req = urllib.request.Request(self.cfg.url, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                body = json.loads(resp.read())
        except Exception:
            return None
        try:
            vec = body["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError):
            return None
        v = _to_vec(vec)
        with self._lock:
            self._dim = len(v)
        return v


class SemanticIndex:
    """Index de vecteurs dans SQLite (scan cosinus, stdlib)."""

    def __init__(self, db, threshold=0.95, max_scan=2000):
        self.db = db
        self.threshold = threshold
        self.max_scan = max_scan
        self._lock = threading.Lock()

    def _rows(self):
        with self.db.cursor(commit=False) as cur:
            cur.execute(
                "SELECT key, embedding, dim, provider, model, is_stream, response "
                "FROM semantic_cache WHERE embedding IS NOT NULL "
                "ORDER BY last_accessed DESC LIMIT ?",
                (self.max_scan,),
            )
            return cur.fetchall()

    def lookup(self, vec):
        rows = self._rows()
        if not rows:
            return None
        best = None
        best_score = -1.0
        for row in rows:
            blob = row[1]
            if not blob:
                continue
            cand = _unpack(blob)
            score = _cos(cand, vec)
            if score > best_score:
                best_score = score
                best = row
        if best is None or best_score < self.threshold:
            return None
        return best[0], best_score, best

    def insert(self, key, canon_key, vec, provider, model, is_stream, response, prompt_tokens=0, completion_tokens=0):
        with self._lock, self.db.cursor() as cur:
            cur.execute(
                "INSERT INTO semantic_cache(key, canon_key, provider, model, "
                "is_stream, response, embedding, dim, prompt_tokens, "
                "completion_tokens) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET response=excluded.response, "
                "embedding=excluded.embedding, dim=excluded.dim, "
                "last_accessed=datetime('now')",
                (key, canon_key, provider, model, 1 if is_stream else 0,
                 response, _pack(vec), len(vec), int(prompt_tokens), int(completion_tokens)),
            )

    def touch(self, key):
        with self._lock, self.db.cursor() as cur:
            cur.execute(
                "UPDATE semantic_cache SET last_accessed=datetime('now'), "
                "access_count=access_count+1 WHERE key=?", (key,))

    def stats(self):
        with self.db.cursor(commit=False) as cur:
            total = cur.execute("SELECT COUNT(*) FROM semantic_cache").fetchone()[0]
            hits = cur.execute("SELECT COALESCE(SUM(access_count),0) FROM semantic_cache").fetchone()[0]
        return {"entries": int(total), "reuses": int(hits)}
