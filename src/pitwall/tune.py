"""Feedback loop (docs/18 §tuning): graded review calls and `pitwall diff`
A/B results in SQLite become persisted per-rule tuning the live dispatcher
loads at start. Recomputed from the whole database each run, so it is
idempotent and never depends on in-memory state."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pitwall.store.db import Database

TUNE_TRACK = -1  # model_params namespace for rule tuning (not track-specific)
TUNE_COMPOUND = -1
COOLDOWN_PREFIX = "cooldown_mult:"
AB_PREFIX = "ab_net:"

_BAD = ("noise", "wrong", "too_late")


@dataclass(frozen=True, slots=True)
class RuleTune:
    rule_id: str
    grades: int
    good: int
    bad: int
    cooldown_mult: float
    ab_net: int  # sum over A/B runs of (only_b - only_a) fires


def _th(th: Mapping[str, Any], name: str, default: float) -> float:
    v = th.get(name, default)
    return float(v) if isinstance(v, int | float) else default


def tune_from_db(db: Database, th: Mapping[str, Any]) -> list[RuleTune]:
    """Fold every call grade and A/B row into per-rule parameters.

    cooldown_mult = 2 ** (gain * (bad - good) / n), clamped; rules with fewer
    than tune_min_grades grades stay at 1.0."""
    min_n = int(_th(th, "tune_min_grades", 3))
    gain = _th(th, "tune_gain", 2.0)
    lo = _th(th, "tune_min_cooldown_mult", 0.5)
    hi = _th(th, "tune_max_cooldown_mult", 4.0)
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for g in db.all_grades():
        counts[str(g["rule_id"])][str(g["grade"])] += 1
    ab: dict[str, int] = defaultdict(int)
    for r in db.ab_results():
        ab[str(r["rule_id"])] += int(r["only_b"] or 0) - int(r["only_a"] or 0)
    out: list[RuleTune] = []
    for rule_id in sorted(set(counts) | set(ab)):
        c = counts.get(rule_id, Counter())
        n = sum(c.values())
        good = c["good"]
        bad = sum(c[k] for k in _BAD)
        mult = 1.0
        if n >= min_n:
            mult = min(hi, max(lo, 2.0 ** (gain * (bad - good) / n)))
        mult = round(mult, 3)
        if n:
            db.set_param(TUNE_TRACK, TUNE_COMPOUND, COOLDOWN_PREFIX + rule_id, mult, float(n))
        if rule_id in ab:
            db.set_param(TUNE_TRACK, TUNE_COMPOUND, AB_PREFIX + rule_id, float(ab[rule_id]), 1.0)
        out.append(RuleTune(rule_id, n, good, bad, mult, ab.get(rule_id, 0)))
    return out


def load_cooldown_mults(db: Database | None) -> dict[str, float]:
    """Persisted per-rule cooldown multipliers for the dispatcher."""
    if db is None:
        return {}
    return {
        p.name[len(COOLDOWN_PREFIX) :]: p.value
        for p in db.params_for_track(TUNE_TRACK)
        if p.compound == TUNE_COMPOUND and p.name.startswith(COOLDOWN_PREFIX)
    }


def record_diff(db: Database, result: Mapping[str, Any], **meta: str) -> int:
    """Store `pitwall diff` per-rule A/B counts in ab_results. Returns rows."""
    n = 0
    for f in result.get("files", []):
        only_a = Counter(str(r.get("rule_id")) for r in f.get("only_a", []))
        only_b = Counter(str(r.get("rule_id")) for r in f.get("only_b", []))
        per_rule = f.get("summary", {}).get("per_rule", {})
        for rule_id, ab in per_rule.items():
            both = min(int(ab.get("a", 0)), int(ab.get("b", 0)))
            db.record_ab(
                recording=str(f.get("recording", "")),
                rule_id=str(rule_id),
                only_a=only_a.get(rule_id, 0),
                only_b=only_b.get(rule_id, 0),
                both=both,
                **meta,
            )
            n += 1
    return n


def format_tune(rows: list[RuleTune]) -> str:
    if not rows:
        return "tune: no grades or A/B results in the database yet"
    lines = [f"{'rule':<28} {'n':>3} {'good':>4} {'bad':>4} {'cooldown x':>10} {'ab net':>6}"]
    for r in rows:
        lines.append(
            f"{r.rule_id:<28} {r.grades:>3} {r.good:>4} {r.bad:>4} "
            f"{r.cooldown_mult:>10.2f} {r.ab_net:>+6d}"
        )
    return "\n".join(lines)
