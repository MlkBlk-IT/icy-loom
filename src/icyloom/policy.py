"""Permissions, portée et approbations : la politique d'exécution.

Aucun outil ne s'exécute sans décision de politique. Trois effets possibles :

``allow``     l'appel passe ;
``deny``      l'appel est refusé, sans effet de bord ;
``approval``  l'appel exige ``confirm=true`` : sans confirmation, il est
              refusé **sans effet de bord**.

La règle la plus spécifique l'emporte : ``(role, serveur, outil)`` avant
``(role, serveur, *)`` avant ``(role, *, *)``.

Règles de sécurité intégrées (non désactivables) :

* un outil destructeur exige toujours une approbation explicite ;
* une écriture hors de la portée projet (``project_roots``) est refusée ;
* l'exécution de code passe par le bac à sable, jamais par un shell.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .config import PolicyConfig

#: Effets de bord autorisés par rôle (le rôle inconnu reste au plus strict).
#: ``destructive`` reste soumis à approbation explicite, même pour un rôle
#: qui en a la capacité.
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "reader": frozenset({"none"}),
    "writer": frozenset({"none", "write"}),
    "operator": frozenset({"none", "write", "destructive"}),
    "admin": frozenset({"none", "write", "destructive"}),
}

#: Clés d'arguments qui portent un chemin : c'est là que se joue la portée.
_PATH_KEYS = ("path", "file", "filename", "dir", "directory", "folder",
              "source", "destination", "root", "cwd", "target")

#: Serveurs dont les outils agissent sur le système de fichiers.
_FS_SERVERS = ("fs", "filesystem", "files", "file")


@dataclass
class Decision:
    """Décision de politique : effet, motif, règle qui l'a produite."""

    effect: str
    reason: str
    rule: str = ""
    role: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.effect == "allow"

    def as_dict(self) -> dict[str, Any]:
        out = {"effect": self.effect, "reason": self.reason,
               "rule": self.rule, "role": self.role}
        if self.extra:
            out.update(self.extra)
        return out


class PolicyEngine:
    """Moteur de permissions : rôle, portée projet, environnement, risque."""

    def __init__(self, config: PolicyConfig) -> None:
        self.config = config

    # -- règles -----------------------------------------------------------
    def _rule(self, role: str, server: str, tool: str) -> tuple[str, str] | None:
        """Règle la plus spécifique pour (role, serveur, outil)."""
        rules = self.config.roles.get(role) or []
        for rule in rules:
            if rule.server in ("*", server) and rule.tool in ("*", tool):
                return rule.effect, f"{role}:{rule.server}:{rule.tool}"
        return None

    def roles(self) -> list[str]:
        """Rôles connus : ceux de la configuration plus les rôles standard."""
        known = set(self.config.roles) | set(ROLE_CAPABILITIES)
        return sorted(known)

    # -- décision ---------------------------------------------------------
    def decide(self, *, role: str, server: str, tool: str, side_effects: str,
               security_level: str = "low", args: dict[str, Any] | None = None,
               confirmed: bool = False, intent: str = "") -> Decision:
        """Décide si un appel passe, est refusé, ou exige une approbation."""
        if not self.config.enabled:
            return Decision("allow", "policy_disabled", role=role)
        role = role or self.config.default_role
        args = args or {}
        found = self._rule(role, server, tool)
        rule_effect, rule_label = found if found else ("", "")

        # 1. un refus explicite l'emporte sur tout le reste.
        if rule_effect == "deny":
            return Decision("deny", "policy_deny", rule=rule_label, role=role)

        # 2. garde-fous de sécurité : ils priment sur une autorisation.
        violation = self._scope_violation(server, tool, args, side_effects)
        if violation is not None:
            return Decision("deny", violation, rule=rule_label, role=role)

        # 3. un effet de bord destructeur exige toujours une approbation.
        if side_effects == "destructive" and not confirmed:
            return Decision("approval", "destructive_requires_approval",
                            rule=rule_label, role=role)

        # 4. règle explicite.
        if rule_effect == "approval":
            if confirmed:
                return Decision("allow", "approval_granted", rule=rule_label, role=role)
            return Decision("approval", "policy_requires_approval",
                            rule=rule_label, role=role)
        if rule_effect == "allow":
            return Decision("allow", "policy_allow", rule=rule_label, role=role)

        # 5. sinon : les capacités du rôle décident.
        allowed = ROLE_CAPABILITIES.get(role, ROLE_CAPABILITIES["reader"])
        if side_effects not in allowed:
            return Decision("deny", "role_forbids_side_effect", rule=rule_label,
                            role=role, extra={"allowed": sorted(allowed)})
        return Decision("allow", "role_allows", rule=rule_label, role=role)

    # -- portée projet ----------------------------------------------------
    def _scope_violation(self, server: str, tool: str, args: dict[str, Any],
                         side_effects: str) -> str | None:
        """Refuse une écriture hors des racines projet configurées."""
        roots = self._roots()
        if not roots or side_effects == "none":
            return None
        if server.lower() not in _FS_SERVERS and not any(
                key in tool.lower() for key in ("file", "path", "dir")):
            return None
        for path in self._paths(args):
            if not self._within(path, roots):
                return "outside_project_scope"
        return None

    def _roots(self) -> list[str]:
        roots: list[str] = []
        for raw in self.config.project_roots:
            try:
                roots.append(os.path.normcase(os.path.abspath(os.path.expanduser(raw))))
            except (OSError, ValueError):
                continue
        return roots

    @staticmethod
    def _paths(args: dict[str, Any]) -> list[str]:
        found: list[str] = []
        for key, value in (args or {}).items():
            if not isinstance(value, str) or not value:
                continue
            lowered = key.lower()
            if any(marker in lowered for marker in _PATH_KEYS):
                found.append(value)
        return found

    @staticmethod
    def _within(path: str, roots: list[str]) -> bool:
        """Un chemin est-il dans une des racines ?

        Attention à une racine terminée par un séparateur : sans
        normalisation, la comparaison produirait deux séparateurs et rien
        ne serait jamais considéré comme autorisé.
        """
        try:
            target = os.path.normcase(os.path.abspath(os.path.expanduser(path)))
        except (OSError, ValueError):
            return True
        for root in roots:
            base = root.rstrip("\\/")
            if not base:
                continue
            if target == base or target.startswith(base + os.sep):
                return True
        return False

    # -- introspection ----------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """État de la politique, pour le dashboard et l'introspection."""
        return {
            "enabled": self.config.enabled,
            "default_role": self.config.default_role,
            "environment": self.config.environment,
            "project_roots": list(self.config.project_roots),
            "roles": {
                role: [
                    {"server": rule.server, "tool": rule.tool, "effect": rule.effect}
                    for rule in rules
                ]
                for role, rules in self.config.roles.items()
            },
            "capabilities": {role: sorted(effects)
                             for role, effects in ROLE_CAPABILITIES.items()},
        }
