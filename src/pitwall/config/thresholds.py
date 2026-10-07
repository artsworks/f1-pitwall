"""Numeric threshold lookup shared by the model, strategy and learning modules."""

from __future__ import annotations

from collections.abc import Mapping


def threshold(th: Mapping[str, object], name: str, default: float) -> float:
    v = th.get(name, default)
    return float(v) if isinstance(v, int | float) else default
