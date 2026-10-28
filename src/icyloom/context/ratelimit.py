"""Limitation de débit par session (algorithme du seau à jetons).

Un seau par identifiant (session ou adresse IP). Le seau se remplit
continuellement (débit) et se vide à chaque consommation (capacité). Quand le
seau est vide, l'appel est refusé — l'appelant reçoit alors une erreur
explicite plutôt qu'un silence.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    """Seau à jetons pour un identifiant donné."""

    capacity: float
    refill_per_second: float
    tokens: float = field(init=False)
    updated_at: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def __post_init__(self) -> None:
        self.tokens = float(self.capacity)
        self.updated_at = time.monotonic()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.updated_at)
        if elapsed:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
            self.updated_at = now

    def consume(self, amount: float = 1.0) -> bool:
        """Consomme `amount` jetons. Renvoie False si le seau est vide."""
        with self._lock:
            now = time.monotonic()
            self._refill(now)
            if self.tokens >= amount:
                self.tokens -= amount
                return True
            return False

    def available(self) -> float:
        with self._lock:
            self._refill(time.monotonic())
            return self.tokens


class RateLimiter:
    """Seaux par identifiant, avec purge des seaux inactifs."""

    def __init__(self, per_minute: int = 120, burst: int | None = None) -> None:
        self.per_minute = max(1, int(per_minute))
        self.capacity = float(burst if burst is not None else max(1, self.per_minute // 4))
        self.refill_per_second = self.per_minute / 60.0
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, amount: float = 1.0) -> bool:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(self.capacity, self.refill_per_second)
                self._buckets[key] = bucket
        return bucket.consume(amount)

    def remaining(self, key: str) -> float:
        with self._lock:
            bucket = self._buckets.get(key)
        return bucket.available() if bucket else self.capacity

    def purge(self) -> int:
        """Retire les seaux pleins (inactifs). Renvoie le nombre retiré."""
        with self._lock:
            full = [k for k, b in self._buckets.items() if b.available() >= self.capacity]
            for key in full:
                self._buckets.pop(key, None)
        return len(full)
