"""Compact spoken pace deltas: "3 tenths", "8 hundredths", "a second and a half"."""

from __future__ import annotations

import math

_ONES = ("", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


def amount_words(delta_s: float) -> str:
    """Magnitude of a per-lap delta in the unit a race engineer would use."""
    d = abs(delta_s)
    if not math.isfinite(d):
        return ""
    if d < 0.095:
        n = max(1, round(d * 100))
        return "a hundredth" if n == 1 else f"{n} hundredths"
    if d < 0.95:
        n = round(d * 10)
        if n == 5:
            return "half a second"
        return "a tenth" if n == 1 else f"{_ONES[n]} tenths"
    if d < 1.05:
        return "a second"
    return f"{d:.1f} seconds"


def pace_words(delta_s: float, same_band_s: float = 0.03) -> str:
    """`delta_s` + = he is faster than us per lap. '' when unknown."""
    if not math.isfinite(delta_s):
        return ""
    if abs(delta_s) < same_band_s:
        return "same pace"
    return f"{amount_words(delta_s)} {'faster' if delta_s > 0 else 'slower'}"
