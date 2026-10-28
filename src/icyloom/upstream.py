"""Routage vers les.amonts LLM.

Ordre de résolution (le plus prioritaire gagne) :

1. **préfixe d'URL** : ``/{nom_amont}/v1/...`` ;
2. **en-tête** : ``X-IcyLoom-Upstream`` ;
3. **corps** : champ ``upstream`` ;
4. **défaut** : amont activé le plus prioritaire.

Deux garanties importantes :

* **aucune mutation d'état** : la priorité n'est jamais modifiée pendant un
  appel. L'ordre des candidats est recalculé à chaque requête, donc un
  échec temporaire ne peut pas dégrader durablement le routage.
* **bascule sur échec** : si l'amont choisi échoue, le relais passe au
  candidat suivant plutôt que de renvoyer une erreur.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .config import UpstreamConfig


@dataclass
class RoutedUpstream:
    """Un amont retenu pour une requête, avec la raison du choix."""

    upstream: UpstreamConfig
    source: str          # prefix | header | body | default | fallback
    sticky: bool = False


@dataclass
class UpstreamRegistry:
    """Catalogue des amonts et calcul de l'ordre de tentative."""

    upstreams: list[UpstreamConfig] = field(default_factory=list)
    #: Amont « collant » mémorisé par client (clé = identifiant de session).
    _sticky: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, configs: Iterable[UpstreamConfig]) -> "UpstreamRegistry":
        return cls(upstreams=list(configs))

    # -- catalogue --------------------------------------------------------
    def get(self, name: str) -> UpstreamConfig | None:
        for upstream in self.upstreams:
            if upstream.name == name:
                return upstream
        return None

    def enabled(self) -> list[UpstreamConfig]:
        return [u for u in self.upstreams if u.enabled]

    def default(self) -> UpstreamConfig | None:
        active = self.enabled()
        if not active:
            return None
        return sorted(active, key=lambda u: (u.priority, u.name))[0]

    def names(self) -> list[str]:
        return [u.name for u in self.upstreams]

    # -- routage ----------------------------------------------------------
    def resolve(
        self,
        prefix: str | None = None,
        header: str | None = None,
        body: Mapping[str, object] | None = None,
        client_key: str | None = None,
    ) -> RoutedUpstream:
        """Détermine l'amont principal et l'ordre des candidats de repli."""
        candidates: list[UpstreamConfig] = []

        for source, name in (
            ("prefix", prefix),
            ("header", header),
            ("body", str(body.get("upstream")) if body and body.get("upstream") else None),
        ):
            if not name:
                continue
            found = self.get(name)
            if found is None or not found.enabled:
                continue
            candidates.append(found)
            break

        chosen_source = ("prefix" if prefix and self.get(prefix) else
                         "header" if header and self.get(header) else
                         "body" if body and body.get("upstream") and
                         self.get(str(body.get("upstream"))) else "default")

        # Un amont « collant » reprend la main s'il est encore disponible et
        # si aucun choix explicite n'a été fait.
        if chosen_source == "default" and client_key:
            sticky_name = self._sticky.get(client_key)
            sticky = self.get(sticky_name) if sticky_name else None
            if sticky is not None and sticky.enabled:
                candidates.insert(0, sticky)
                chosen_source = "sticky"

        if not candidates:
            fallback = self.default()
            if fallback is None:
                raise LookupError("aucun amont disponible")
            candidates = [fallback]
            chosen_source = chosen_source if chosen_source != "default" else "default"

        ordered = self._order(candidates)
        primary = ordered[0]
        if primary.sticky and client_key:
            self._sticky[client_key] = primary.name
        return RoutedUpstream(upstream=primary, source=chosen_source,
                              sticky=bool(primary.sticky))

    def _order(self, candidates: list[UpstreamConfig]) -> list[UpstreamConfig]:
        """Ordonne les candidats : priorité d'abord, puis le reste par ordre."""
        seen: set[str] = set()
        unique: list[UpstreamConfig] = []
        for candidate in candidates:
            if candidate.name in seen:
                continue
            seen.add(candidate.name)
            unique.append(candidate)
        head = unique[:1]
        tail = sorted(
            [u for u in self.enabled() if u.name not in {h.name for h in head}],
            key=lambda u: (u.priority, u.name),
        )
        return head + tail

    def fallback_chain(self, primary: UpstreamConfig) -> list[UpstreamConfig]:
        """Chaîne de repli après un échec de l'amont principal."""
        ordered = self._order([primary])
        return ordered[1:]

    def forget_sticky(self, client_key: str) -> None:
        self._sticky.pop(client_key, None)
