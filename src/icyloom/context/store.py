"""Moteur de contexte : façade unique au-dessus du tiroir et de ses briques."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config
from ..db import Database
from ..ledger import Ledger, LedgerEntry
from ..tokenizer import count_tokens, counter_name
from .anchors import Invariant
from .audit import AuditLog
from .compress import Profile, build_anchor, split_units
from .drawer import Drawer
from .ratelimit import RateLimiter
from .redact import redact_text
from .sandbox import CommandSandbox, SandboxUnavailable
from .sessions import SessionStore
from .verify import verify_anchor, verify_roundtrip

#: Profils acceptés.
PROFILE_NAMES = ("strict", "balanced", "aggressive")


@dataclass
class StoreOutcome:
    """Ce que produit un rangement de contenu."""

    session_id: str
    label: str
    anchor: str
    chunk_ids: list[int]
    source_id: int
    tokens_before: int
    anchor_tokens: int
    redactions: int
    lossless: bool
    profile: str

    def as_dict(self) -> dict[str, Any]:
        saved = self.tokens_before - self.anchor_tokens
        return {
            "session_id": self.session_id,
            "label": self.label,
            "anchor": self.anchor,
            "chunk_ids": self.chunk_ids,
            "source_id": self.source_id,
            "tokens_before": self.tokens_before,
            "anchor_tokens": self.anchor_tokens,
            "tokens_saved": saved,
            "saved_ratio": round(saved / self.tokens_before, 4) if self.tokens_before else 0.0,
            "redactions": self.redactions,
            "lossless": self.lossless,
            "profile": self.profile,
            "token_counter": counter_name(),
        }


class ContextStore:
    """Point d'entrée unique du moteur de contexte."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = Database(config)
        self.db.ensure_schema()
        self.drawer = Drawer(self.db)
        self.drawer.ensure_schema()
        self.sessions = SessionStore(self.db, ttl_days=config.context.session_ttl_days)
        self.sandbox = CommandSandbox(
            allowed_dirs=config.context.allowed_dirs,
            timeout_seconds=config.context.sandbox_timeout_seconds,
            enabled=config.context.sandbox_enabled,
        )
        self.rate_limiter = RateLimiter(per_minute=config.context.rate_limit_per_minute)
        self.audit = AuditLog(
            directory=Path(config.database.path).parent / "logs",
            retention_days=config.context.audit_retention_days,
        ) if config.context.audit_enabled else None
        self.ledger = Ledger(self.db, enabled=config.ledger.enabled)
        self._profile = Profile.resolve(config.compression.profile,
                                        config.compression.max_anchor_chars)

    # -- profil -----------------------------------------------------------
    @property
    def profile(self) -> Profile:
        return self._profile

    def set_profile(self, mode: str) -> dict[str, Any]:
        """Change le profil de compression. Refuse un mode inconnu."""
        name = (mode or "").strip().lower()
        if name not in PROFILE_NAMES:
            return {"ok": False, "error": f"profil inconnu : {name}",
                    "allowed": list(PROFILE_NAMES), "active": self._profile.name}
        self._profile = Profile.resolve(name, self.config.compression.max_anchor_chars)
        return {
            "ok": True,
            "active": self._profile.name,
            "lossy": self._profile.lossy,
            "max_anchor_chars": self._profile.max_anchor_chars,
            "warning": ("mode lossy : la compression n'est plus garantie sans perte"
                        if self._profile.lossy else "mode lossless"),
        }

    # -- rangement --------------------------------------------------------
    def store_text(self, content: str, session_id: str = "default", label: str = "",
                   profile: Profile | None = None) -> StoreOutcome:
        """Range un contenu : rédaction, ancre, fragments, invariants."""
        started = time.perf_counter()
        resolved = profile or self._profile
        if not self.rate_limiter.allow(session_id):
            raise RuntimeError(f"limite de débit atteinte pour la session '{session_id}'")

        redacted, redactions = (content, 0)
        if self.config.context.redact_enabled:
            redacted, redactions = redact_text(content)

        tokens_before = count_tokens(redacted)
        anchor, invariants = build_anchor(
            redacted, profile=resolved,
            max_invariants=self.config.compression.max_invariants,
        )
        anchor_tokens = count_tokens(anchor)
        # Seuil de rentabilite : sous ce volume, l'ancre (squelette structurel a
        # taille quasi fixe) coute PLUS CHER que le contenu qu'elle resume. La
        # garder ferait perdre des tokens a chaque appel, sans que le ledger le
        # montre (le max(0, ...) plus bas masquait la perte). On sert donc le
        # contenu brut : gain nul au pire, jamais negatif.
        # Les invariants sont CONSERVES : ce sont eux qui rendent le contenu
        # verifiable (verify()/coverage). Seul le resume-cadratin est redondant
        # ici, pas les faits extraits.
        if anchor_tokens >= tokens_before:
            anchor = redacted
            anchor_tokens = tokens_before
        units = split_units(redacted)

        # La session « default » est stable : on ne crée pas un identifiant à
        # chaque appel, sinon ctx_store / ctx_search / ctx_check ne se
        # rejoignent jamais.
        real_session = session_id if session_id and session_id != "default" else "default"
        self.sessions.create(real_session)
        source_id, chunk_ids = self.drawer.store_source(
            session_id=real_session,
            label=label,
            content=redacted,
            units=units,
            anchor=anchor,
            invariants=invariants,
            tokens_before=tokens_before,
            anchor_tokens=anchor_tokens,
            redactions=redactions,
        )
        self.sessions.touch(real_session)
        if self.audit:
            self.audit.record("ctx_store", real_session, "success",
                              (time.perf_counter() - started) * 1000,
                              tokens_before=tokens_before, anchor_tokens=anchor_tokens)
        self.ledger.record(LedgerEntry(
            kind="context_compress",
            session_id=real_session,
            # Valeur SIGNEE (pas de max(0, ...)) : une compression non rentable
            # doit apparaitre comme une perte, pas etre masquee a zero.
            tokens_saved=tokens_before - anchor_tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
            meta={"source_id": source_id, "profile": resolved.name,
                  "token_counter": counter_name(), "redactions": redactions},
        ))
        return StoreOutcome(
            session_id=real_session,
            label=label,
            anchor=anchor,
            chunk_ids=chunk_ids,
            source_id=source_id,
            tokens_before=tokens_before,
            anchor_tokens=anchor_tokens,
            redactions=redactions,
            lossless=not resolved.lossy,
            profile=resolved.name,
        )

    def store_file(self, path: str, session_id: str = "default", label: str = "") -> StoreOutcome:
        """Range le contenu d'un fichier."""
        target = Path(path).expanduser()
        if not target.is_file():
            raise FileNotFoundError(f"fichier introuvable : {target}")
        content = target.read_text(encoding="utf-8", errors="replace")
        return self.store_text(content, session_id, label or target.name)

    def store_cmd(self, cmd: str, session_id: str = "default", label: str = "") -> StoreOutcome:
        """Range la sortie d'une commande exécutée sous bac à sable."""
        result = self.sandbox.run(cmd)
        body = result.stdout
        if result.stderr:
            body = f"{body}\n[stderr]\n{result.stderr}"
        outcome = self.store_text(body, session_id, label or f"cmd: {cmd[:40]}")
        payload = outcome.as_dict()
        payload["command"] = result.as_dict()
        payload["source_id"] = outcome.source_id
        payload["chunk_ids"] = outcome.chunk_ids
        payload["anchor"] = outcome.anchor
        payload["session_id"] = outcome.session_id
        return outcome

    # -- lecture ----------------------------------------------------------
    def get_chunk(self, chunk_id: int | str) -> dict[str, Any]:
        """Relit un fragment, octet pour octet."""
        try:
            numeric = int(chunk_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": "identifiant invalide", "chunk_id": str(chunk_id)}
        chunk = self.drawer.chunk(numeric)
        if chunk is None:
            return {"ok": False, "error": "fragment introuvable", "chunk_id": numeric}
        return {
            "ok": True,
            "chunk_id": chunk.id,
            "session_id": chunk.session_id,
            "label": chunk.label,
            "content": chunk.body,
            "tokens": chunk.tokens,
            "hits": chunk.hits,
        }

    def search(self, query: str, limit: int = 5, session_id: str = "default") -> dict[str, Any]:
        """Recherche dans le tiroir."""
        results = self.drawer.search(query, session_id=None if session_id in ("", "all") else session_id,
                                     limit=limit)
        return {
            "ok": True,
            "query": query,
            "count": len(results),
            "results": results,
            "fts": self.drawer.fts_available(),
        }

    # -- vérification -----------------------------------------------------
    def verify(self, session_id: str | None = None) -> dict[str, Any]:
        """Preuve de couverture : chaque invariant se retrouve dans l'ancre."""
        reports = []
        ok = True
        for source in self.drawer.sources(session_id):
            invariants = self.drawer.invariants(source.id)
            anchor = self.drawer.anchor(source.id) or ""
            result = verify_anchor(invariants, anchor)
            ok = ok and result.ok
            reports.append({
                "source_id": source.id,
                "label": source.label,
                **result.as_dict(),
            })
        return {
            "ok": ok,
            "session_id": session_id or "all",
            "sources": len(reports),
            "coverage": round(
                sum(r["invariants_present"] for r in reports)
                / max(1, sum(r["invariants_total"] for r in reports)), 4),
            "reports": reports,
        }

    def check(self, session_id: str = "default") -> dict[str, Any]:
        """Contrôle de perte sur une session : invariants + fidélité du contenu."""
        sources = self.drawer.sources(session_id)
        missing: list[str] = []
        total = 0
        present = 0
        roundtrip_ok = True
        for source in sources:
            invariants = self.drawer.invariants(source.id)
            anchor = self.drawer.anchor(source.id) or ""
            result = verify_anchor(invariants, anchor)
            total += result.total
            present += result.present
            missing.extend(result.missing)
            chunks = [c.body for c in self.drawer.chunks_of_session(session_id)
                      if c.source_digest == source.digest]
            original = "".join(chunks)
            if not verify_roundtrip(original, chunks):
                roundtrip_ok = False
        return {
            "ok": not missing and roundtrip_ok,
            "session_id": session_id,
            "invariants_total": total,
            "invariants_present": present,
            "invariants_missing": missing,
            "roundtrip_ok": roundtrip_ok,
            "sources": len(sources),
            "profile": self._profile.name,
            "lossless": not self._profile.lossy,
        }

    # -- transfert --------------------------------------------------------
    def transfer(self, from_session: str, to_session: str, chunk_ids: list[int]) -> dict[str, Any]:
        """Transfère des fragments d'une session vers une autre."""
        if not to_session:
            return {"ok": False, "error": "session cible manquante"}
        target = self.sessions.create(to_session)
        moved: list[int] = []
        with self.db.transaction() as cur:
            for chunk_id in chunk_ids:
                row = cur.execute(
                    "SELECT source_digest, chunk_index, label, body, tokens, created_at"
                    " FROM context_chunks WHERE id=? AND session_id=?",
                    (chunk_id, from_session),
                ).fetchone()
                if row is None:
                    continue
                cur.execute(
                    "INSERT INTO context_chunks"
                    " (session_id, source_digest, chunk_index, label, body, tokens,"
                    "  created_at, hits) VALUES (?,?,?,?,?,?,?,0)",
                    (target, row["source_digest"], row["chunk_index"], row["label"],
                     row["body"], row["tokens"], row["created_at"]),
                )
                new_id = int(cur.lastrowid or 0)
                if self.drawer.fts_available():
                    cur.execute(
                        "INSERT INTO context_chunks_fts(rowid, body) VALUES (?,?)",
                        (new_id, row["body"]),
                    )
                cur.execute("DELETE FROM context_chunks WHERE id=?", (chunk_id,))
                moved.append(new_id)
        self.sessions.touch(target)
        return {"ok": True, "from_session": from_session, "to_session": target,
                "moved_chunks": moved, "count": len(moved)}

    # -- statistiques -----------------------------------------------------
    def stats(self) -> dict[str, Any]:
        drawer_stats = self.drawer.stats()
        saved = drawer_stats["tokens_before"] - drawer_stats["anchor_tokens"]
        return {
            **drawer_stats,
            "tokens_saved": saved,
            "saved_ratio": round(saved / drawer_stats["tokens_before"], 4)
            if drawer_stats["tokens_before"] else 0.0,
            "profile": self._profile.name,
            "token_counter": counter_name(),
            "sessions": self.sessions.stats(),
        }

    def health(self) -> dict[str, Any]:
        """État de santé du tiroir."""
        return {
            "ok": True,
            "db": str(self.db.path),
            "journal_mode": self.db.journal_mode(),
            "busy_timeout_ms": self.db.busy_timeout_ms(),
            "fts": self.drawer.fts_available(),
            "profile": self._profile.name,
            "token_counter": counter_name(),
        }
