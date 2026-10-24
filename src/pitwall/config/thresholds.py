"""Numeric threshold lookup shared by the model, strategy and learning modules."""

from __future__ import annotations

import math
from collections.abc import Mapping


def threshold(th: Mapping[str, object], name: str, default: float) -> float:
    v = th.get(name, default)
    return float(v) if isinstance(v, int | float) else default


def finite_number(value: object) -> float | None:
    """Return a finite float for an int or float value, else None. Bools are not numbers."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None
