"""Validation d'un résultat structuré contre l'`outputSchema` d'un outil.

La spécification MCP impose au serveur descendant de renvoyer un résultat
structuré conforme à l'`outputSchema` qu'il déclare, et recommande au client
de le vérifier. Sans cette vérification, une rupture silencieuse du serveur
passe inaperçue : l'agent consomme un résultat qui ne contient plus ce qu'il
attend.

Le sous-ensemble implémenté ici est volontairement restreint (``type``,
``required``, ``properties``, ``additionalProperties``, ``enum``, ``const``,
``items``, ``allOf``/``anyOf``/``oneOf``) et **sans dépendance externe** : le
reste du schéma ne produit pas d'erreur, il est ignoré.
"""

from __future__ import annotations

from typing import Any

#: Types JSON et leurs classes Python. ``bool`` est traité à part : en JSON,
#: un booléen n'est pas un nombre.
_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list, tuple),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "null": (type(None),),
}


def _type_ok(value: Any, expected: str) -> bool:
    types = _TYPES.get(expected)
    if types is None:
        return True          # type inconnu : on ne bloque pas la validation
    if expected == "boolean":
        return isinstance(value, bool)
    if expected in ("number", "integer") and isinstance(value, bool):
        return False
    if expected == "integer" and isinstance(value, float):
        return value.is_integer()
    return isinstance(value, types)


def validate(value: Any, schema: Any, path: str = "$") -> list[str]:
    """Renvoie les écarts entre ``value`` et ``schema`` (liste vide = conforme)."""
    errors: list[str] = []
    if not isinstance(schema, dict) or not schema:
        return errors

    expected = schema.get("type")
    if isinstance(expected, str):
        expected_types = [expected]
    elif isinstance(expected, list):
        expected_types = [str(item) for item in expected]
    else:
        expected_types = []
    if expected_types and not any(_type_ok(value, kind) for kind in expected_types):
        errors.append(f"{path}: type attendu {expected_types}, "
                      f"reçu {type(value).__name__}")
        return errors

    if isinstance(schema.get("enum"), list) and value not in schema["enum"]:
        errors.append(f"{path}: valeur absente de l'enum")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: valeur différente de const")

    for sub in schema.get("allOf") or []:
        errors.extend(validate(value, sub, path))
    branches = list(schema.get("anyOf") or []) + list(schema.get("oneOf") or [])
    if branches and not any(not validate(value, sub, path) for sub in branches):
        errors.append(f"{path}: aucune branche anyOf/oneOf ne correspond")

    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        for name in schema.get("required") or []:
            if name not in value:
                errors.append(f"{path}.{name}: propriété requise absente")
        for name, sub in properties.items():
            if name in value:
                errors.extend(validate(value[name], sub, f"{path}.{name}"))
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                errors.append(f"{path}: propriétés inattendues {extra}")

    if isinstance(value, (list, tuple)):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                errors.extend(validate(item, items, f"{path}[{index}]"))

    return errors
