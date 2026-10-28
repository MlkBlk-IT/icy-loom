"""Circuit breaker par amont.

Trois états : *closed* (tout passe), *open* (tout est refusé), *half-open*
(une sonde est autorisée pour vérifier le retour à la normale).

Le point important : le **réarmement est réel**. Passé le délai
``reset_seconds``, l'état *open* devient *half-open* et une seule sonde est
autorisée. Si la sonde réussit, le breaker revient *closed* et le compteur
d'échecs est remis à zéro. Sans ce réarmement effectif, un amont recoveries
resterait bloqué indéfiniment.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class BreakerStatus:
    """Photographie de l'état d'un breaker."""

    name: str
    state: BreakerState
    failures: int
    opened_at: float | None
    seconds_until_reset: float
    probes_in_flight: int = 0


@dataclass
class CircuitBreaker:
    """Disjoncteur par amont."""

    name: str
    failure_threshold: int = 3
    reset_seconds: float = 30.0
    half_open_probes: int = 1
    _failures: int = field(default=0, init=False)
    _state: BreakerState = field(default=BreakerState.CLOSED, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _probes: int = field(default=0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    # -- état -------------------------------------------------------------
    @property
    def state(self) -> BreakerState:
        with self._lock:
            self._refresh()
            return self._state

    def _refresh(self) -> None:
        """Fait expirer l'état *open* une fois le délai écoulé (appel verrouillé)."""
        if self._state is BreakerState.OPEN and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self.reset_seconds:
                self._state = BreakerState.HALF_OPEN
                self._probes = 0

    def allows(self) -> bool:
        """Le breaker autorise-t-il une requête maintenant ?"""
        with self._lock:
            self._refresh()
            if self._state is BreakerState.CLOSED:
                return True
            if self._state is BreakerState.HALF_OPEN:
                if self._probes < self.half_open_probes:
                    self._probes += 1
                    return True
                return False
            return False

    # -- feedback ---------------------------------------------------------
    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = BreakerState.CLOSED
            self._opened_at = None
            self._probes = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state is BreakerState.HALF_OPEN:
                # La sonde a échoué : on rouvre immédiatement.
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()
                self._probes = 0
            elif self._failures >= self.failure_threshold:
                self._state = BreakerState.OPEN
                self._opened_at = time.monotonic()

    def reset(self) -> None:
        """Remise à zéro manuelle."""
        with self._lock:
            self._failures = 0
            self._state = BreakerState.CLOSED
            self._opened_at = None
            self._probes = 0

    def status(self) -> BreakerStatus:
        with self._lock:
            self._refresh()
            remaining = 0.0
            if self._state is BreakerState.OPEN and self._opened_at is not None:
                remaining = max(0.0, self.reset_seconds - (time.monotonic() - self._opened_at))
            return BreakerStatus(
                name=self.name,
                state=self._state,
                failures=self._failures,
                opened_at=self._opened_at,
                seconds_until_reset=remaining,
                probes_in_flight=self._probes,
            )


class BreakerRegistry:
    """Un breaker par amont."""

    def __init__(self, failure_threshold: int = 3, reset_seconds: float = 30.0,
                 half_open_probes: int = 1) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._failure_threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._half_open_probes = half_open_probes
        self._lock = threading.Lock()

    def for_name(self, name: str) -> CircuitBreaker:
        with self._lock:
            breaker = self._breakers.get(name)
            if breaker is None:
                breaker = CircuitBreaker(
                    name=name,
                    failure_threshold=self._failure_threshold,
                    reset_seconds=self._reset_seconds,
                    half_open_probes=self._half_open_probes,
                )
                self._breakers[name] = breaker
            return breaker

    def all_status(self) -> list[BreakerStatus]:
        with self._lock:
            breakers = list(self._breakers.values())
        return [b.status() for b in breakers]

    def reset_all(self) -> None:
        with self._lock:
            breakers = list(self._breakers.values())
        for breaker in breakers:
            breaker.reset()
