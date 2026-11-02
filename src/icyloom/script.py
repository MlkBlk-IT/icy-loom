"""Exécution sandboxée de scripts d'outils MCP.

Un script est un court fragment Python écrit par un LLM : il enchaîne des
appels d'outils descendants via ``await call(server, tool, args)`` et renvoie
un résultat compact.

Le script tourne dans un espace de noms fermé : pas d'import, pas d'accès
disque, pas de processus, pas de réseau. Le seul point de sortie est
``call``. Les garde-fous, dans l'ordre d'application :

1. analyse syntaxique stricte de l'AST, sans évaluation de chaînes ;
2. liste blanche d'instructions, ``for`` bornés à ``range(...)`` ;
3. liste blanche de builtins, sans ``getattr`` ni ``__import__`` ;
4. budget d'instructions (compteur ``sys.settrace``) ;
5. plafond d'appels d'outils (``max_steps``) ;
6. délai d'exécution maximal (``timeout``).

Les tools à effets de bord doivent passer par des garde-fous.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import sys
from types import FrameType
from typing import Any, Awaitable, Callable

#: Nom de fichier du code compilé : le traceur ne compte que ces frames.
_SCRIPT_FILENAME = "<nyx-script>"

#: Builtins exposés au script. ``print`` est capturé, jamais écrit sur stdout
#: (stdout est réservé au JSON-RPC).
_ALLOWED_BUILTINS = (
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "float", "int",
    "len", "list", "max", "min", "print", "range", "reversed", "round", "set",
    "sorted", "str", "sum", "tuple", "zip",
)

#: Noms bannis dans le code source : ils ouvriraient le runtime.
_FORBIDDEN_NAMES = frozenset({
    "eval", "exec", "compile", "__import__", "open", "input", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr", "memoryview",
    "exit", "quit",
})

#: Instructions autorisées. Tout autre nœud ``stmt`` est refusé : import,
#: classe, fonction, ``while``, ``with``, ``try``, ``async for``, ``global``,
#: ``nonlocal``, ``match``.
_ALLOWED_STMTS = frozenset({
    ast.Expr, ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Return, ast.If,
    ast.For, ast.Break, ast.Continue, ast.Pass, ast.Delete, ast.Assert, ast.Raise,
})

#: Nœuds structurels tolérés (opérateurs, contextes, arguments, tranches).
_ALLOWED_NODES = (
    ast.Module, ast.expr, ast.expr_context, ast.operator, ast.unaryop,
    ast.boolop, ast.cmpop, ast.comprehension, ast.arguments, ast.arg,
    ast.keyword, ast.slice,
)

#: Expressions interdites malgré leur nature d'expression.
_FORBIDDEN_EXPRS = (ast.Yield, ast.YieldFrom)

#: Longueur maximale d'une boucle ``for``.
_MAX_RANGE = 10_000


class _ScriptError(Exception):
    """Échec du bac à sable, porteur du code d'erreur public."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _make_print(printed: list[str]) -> Callable[..., None]:
    """``print`` capturé : une ligne par appel, rien sur stdout."""

    def _print(*args: Any, **kwargs: Any) -> None:
        sep = kwargs.get("sep", " ")
        printed.append(sep.join(str(arg) for arg in args))

    return _print


def _safe_builtins(printed: list[str]) -> dict[str, Any]:
    """Builtins autorisés : ni ``open``, ni ``getattr``, ni ``__import__``."""
    table: dict[str, Any] = {}
    for name in _ALLOWED_BUILTINS:
        table[name] = _make_print(printed) if name == "print" else getattr(builtins, name)
    return table


def _check_range(node: ast.expr) -> None:
    """Vérifie qu'une boucle ``for`` boucle sur un ``range`` borné et littéral."""
    if not (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "range"):
        raise _ScriptError("forbidden_syntax",
                           "boucle for limitée à range(...) sur des bornes littérales")
    if node.keywords or len(node.args) > 3:
        raise _ScriptError("forbidden_syntax", "range() attend au plus 3 arguments positionnels")
    for arg in node.args:
        if not (isinstance(arg, ast.Constant)
                and isinstance(arg.value, int)
                and not isinstance(arg.value, bool)):
            raise _ScriptError("forbidden_syntax",
                               "bornes de range() doivent être des entiers littéraux")
    try:
        length = len(range(*[arg.value for arg in node.args]))
    except ValueError:
        raise _ScriptError("forbidden_syntax", "range() invalide") from None
    if length > _MAX_RANGE:
        raise _ScriptError("forbidden_syntax",
                           f"range() trop grand ({length} > {_MAX_RANGE})")


def _validate(body: list[ast.stmt]) -> None:
    """Applique la liste blanche AST et les règles d'accès."""
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.stmt):
            if type(node) not in _ALLOWED_STMTS:
                raise _ScriptError("forbidden_syntax",
                                   f"instruction interdite : {type(node).__name__}")
        elif not isinstance(node, _ALLOWED_NODES):
            raise _ScriptError("forbidden_syntax",
                               f"nœud interdit : {type(node).__name__}")
        if isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_NAMES:
                raise _ScriptError("forbidden_syntax", f"nom interdit : {node.id}")
            if node.id.startswith("__"):
                raise _ScriptError("forbidden_syntax", f"nom dunder interdit : {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                raise _ScriptError("forbidden_syntax", f"attribut dunder interdit : {node.attr}")
        elif isinstance(node, ast.Await):
            awaited = node.value
            if not (isinstance(awaited, ast.Call)
                    and isinstance(awaited.func, ast.Name)
                    and awaited.func.id == "call"):
                raise _ScriptError("forbidden_syntax",
                                   "await autorisé uniquement sur call(...)")
        elif isinstance(node, ast.For):
            _check_range(node.iter)
        elif isinstance(node, _FORBIDDEN_EXPRS):
            raise _ScriptError("forbidden_syntax",
                               f"expression interdite : {type(node).__name__}")


def _wrap(tree: ast.Module) -> ast.Module:
    """Enveloppe le corps du script dans ``async def __nyx_main()``.

    L'enveloppement se fait sur l'AST, pas sur le texte : les chaînes
    multilignes gardent leur contenu exact.
    """
    ast.increment_lineno(tree, 1)
    args = ast.arguments(posonlyargs=[], args=[], vararg=None, kwonlyargs=[],
                         kw_defaults=[], kwarg=None, defaults=[])
    func = ast.AsyncFunctionDef(name="__nyx_main", args=args, body=tree.body,
                                 decorator_list=[], returns=None)
    if "type_params" in ast.AsyncFunctionDef._fields:
        func.type_params = []
    module = ast.Module(body=[func], type_ignores=[])
    ast.fix_missing_locations(module)
    return module


def _make_tracer(counters: dict[str, int], limit: int) -> Callable[..., Any]:
    """Traceur d'instructions : compte les lignes du script et bloque au-delà."""

    def _tracer(frame: FrameType, event: str, arg: Any) -> Any:
        if frame.f_code.co_filename != _SCRIPT_FILENAME:
            return None
        counters["instructions"] += 1
        if counters["instructions"] > limit:
            raise _ScriptError("budget_exceeded",
                               f"budget d'instructions dépassé ({limit})")
        return _tracer

    return _tracer


async def run_script(
    source: str,
    call: Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]]],
    *,
    timeout: float = 30.0,
    max_steps: int = 20,
    max_instructions: int = 200_000,
) -> dict[str, Any]:
    """Exécute un script d'outils dans le bac à sable et rend un résultat compact.

    Le script reçoit une unique fonction ``call(server, tool, args)``,
    awaitable, qui est le seul point de sortie. ``print`` est capturé et
    renvoyé sous la clé ``printed``.

    Succès : ``{"ok": True, "result": ..., "steps": n, "instructions": n}``.
    Échec : ``{"ok": False, "error": <code>, "message": <détail>, ...}`` avec
    les codes ``empty_script``, ``syntax_error``, ``forbidden_syntax``,
    ``timeout``, ``too_many_steps``, ``step_limit``, ``budget_exceeded`` et
    ``runtime_error``.
    """
    counters: dict[str, int] = {"steps": 0, "instructions": 0}
    printed: list[str] = []

    def _fail(code: str, message: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": code,
            "message": message,
            "steps": counters["steps"],
            "instructions": counters["instructions"],
            "printed": printed,
        }

    if max_steps < 1:
        return _fail("too_many_steps", f"max_steps doit être >= 1 (reçu {max_steps})")
    if max_instructions < 1:
        return _fail("budget_exceeded",
                     f"max_instructions doit être >= 1 (reçu {max_instructions})")
    if not source or not source.strip():
        return _fail("empty_script", "script vide")

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return _fail("syntax_error", f"erreur de syntaxe ligne {exc.lineno} : {exc.msg}")
    except ValueError as exc:
        return _fail("syntax_error", f"source illisible : {exc}")
    if not tree.body:
        return _fail("empty_script", "script vide")

    try:
        _validate(tree.body)
    except _ScriptError as exc:
        return _fail(exc.code, exc.message)

    try:
        code = compile(_wrap(tree), _SCRIPT_FILENAME, "exec")
    except SyntaxError as exc:
        return _fail("syntax_error", f"erreur de syntaxe ligne {exc.lineno} : {exc.msg}")

    async def _call(server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        counters["steps"] += 1
        if counters["steps"] > max_steps:
            raise _ScriptError("step_limit", f"plafond d'appels dépassé ({max_steps})")
        if not isinstance(server, str) or not server.strip():
            raise _ScriptError("runtime_error", "serveur vide")
        if not isinstance(tool, str) or not tool.strip():
            raise _ScriptError("runtime_error", "outil vide")
        return await call(server, tool, args)

    namespace: dict[str, Any] = {
        "__builtins__": _safe_builtins(printed),
        "call": _call,
    }
    exec(code, namespace)

    async def _run() -> Any:
        try:
            return await namespace["__nyx_main"]()
        except TimeoutError as exc:
            raise _ScriptError("runtime_error", f"TimeoutError : {exc}") from exc

    previous = sys.gettrace()
    sys.settrace(_make_tracer(counters, max_instructions))
    try:
        result = await asyncio.wait_for(_run(), timeout)
    except asyncio.TimeoutError:
        return _fail("timeout", f"délai dépassé ({timeout}s)")
    except _ScriptError as exc:
        return _fail(exc.code, exc.message)
    except Exception as exc:
        return _fail("runtime_error", f"{type(exc).__name__} : {exc}")
    finally:
        sys.settrace(previous)

    return {
        "ok": True,
        "result": result,
        "steps": counters["steps"],
        "instructions": counters["instructions"],
        "printed": printed,
    }
