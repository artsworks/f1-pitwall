"""Safe expression compiler for rule predicates.

AST-whitelisted eval-mode expressions only; anything else is rejected at load
time. Names resolve against the snapshot attributes plus `th` (thresholds),
`mode` (active mindset vector), and a small function library.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from typing import Any

_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.IfExp,
    ast.Compare,
    ast.Name,
    ast.Attribute,
    ast.Constant,
    ast.Subscript,
    ast.Call,
    ast.Load,
    # operators
    ast.And,
    ast.Or,
    ast.Not,
    ast.UAdd,
    ast.USub,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
)

_ALLOWED_FUNCS = {"abs", "min", "max", "round", "fresh"}


class ExprError(ValueError):
    pass


def _check(tree: ast.AST, source: str) -> None:
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ExprError(
                f"rule expression uses forbidden node {type(node).__name__}: {source!r}"
            )
        if isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Name) and node.func.id in _ALLOWED_FUNCS):
                raise ExprError(f"rule expression calls forbidden function: {source!r}")


def compile_expr(source: str) -> Any:
    """Compile a predicate. Raises ExprError for anything off the whitelist."""
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"bad rule expression {source!r}: {e}") from e
    _check(tree, source)
    return compile(tree, "<rule>", "eval")


class AttrView(Mapping[str, Any]):
    """Attribute access over a mapping, so `mode.x` and `th.y` work in rules."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = data

    def __getattr__(self, name: str) -> Any:
        try:
            v = self._data[name]
        except KeyError as e:
            raise AttributeError(name) from e
        return AttrView(v) if isinstance(v, Mapping) else v

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


class TrackedNamespace(Mapping[str, Any]):
    """Locals mapping for eval that records every name the predicate read.
    Not a dict subclass: CPython bypasses __getitem__ overrides on dicts."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self._data = data
        self.accessed: list[str] = []

    def __getitem__(self, key: Any) -> Any:
        self.accessed.append(str(key))
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


class Predicate:
    def __init__(self, source: str) -> None:
        self.source = source
        self._code = compile_expr(source)

    def __call__(self, ns: Mapping[str, Any]) -> Any:
        return eval(self._code, {"__builtins__": {}}, ns)  # noqa: S307


def make_namespace(
    snapshot: Any,
    *,
    thresholds: Mapping[str, Any],
    mode: Mapping[str, Any],
    staleness_age: Any,
    staleness_limit: Any,
) -> TrackedNamespace:
    """Locals for one predicate evaluation. `fresh(name)` checks a source
    packet's age against its staleness limit."""

    def fresh(name: str) -> bool:
        return bool(staleness_age(name) < staleness_limit(name))

    data: dict[str, Any] = {
        name: getattr(snapshot, name) for name in dir(snapshot) if not name.startswith("_")
    }
    data.update(
        {
            "th": AttrView(thresholds),
            "mode": AttrView(mode),
            "abs": abs,
            "min": min,
            "max": max,
            "round": round,
            "fresh": fresh,
        }
    )
    return TrackedNamespace(data)
