"""Rule engine: evaluate declarative rules against a snapshot.

Per-rule hysteresis: fires on the `when` rising edge, re-arms on `clear_when`
(or on `when` going false if no clear_when). `requires` packets must be fresh.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pitwall.config.models import RuleDefModel
from pitwall.rules.expr import Predicate, make_namespace
from pitwall.state.session import Snapshot

STALENESS_DEFAULT_S = 1.0


@dataclass(slots=True)
class Candidate:
    rule: Rule
    text: str
    priority: int
    tags: list[str]
    still_true: Callable[[Snapshot], bool] | None
    inputs: dict[str, Any]
    trigger_t: float


@dataclass(slots=True)
class Suppressed:
    rule: Rule
    reason: str


class Rule:
    def __init__(self, defn: RuleDefModel) -> None:
        self.defn = defn
        self.id = defn.id
        self.when = Predicate(defn.when)
        self.clear_when = Predicate(defn.clear_when) if defn.clear_when else None
        self._still_true = Predicate(defn.still_true) if defn.still_true else None
        self.armed = True
        self.fires_this_stint = 0

    def still_true(self, snapshot: Snapshot, ns_kwargs: dict[str, Any]) -> bool:
        if self._still_true is None:
            return True
        ns = make_namespace(snapshot, **ns_kwargs)
        return bool(self._still_true(ns))


@dataclass(slots=True)
class EvalResult:
    candidates: list[Candidate] = field(default_factory=list)
    suppressed: list[Suppressed] = field(default_factory=list)


class RuleEngine:
    def __init__(
        self,
        rules: list[RuleDefModel],
        *,
        thresholds: Mapping[str, Any],
        mode: Mapping[str, Any],
        staleness_s: Mapping[str, float] | None = None,
    ) -> None:
        self.rules = [Rule(r) for r in rules]
        self.thresholds = dict(thresholds)
        self.mode = dict(mode)
        self.staleness_s = dict(staleness_s or {})

    def _ns_kwargs(self) -> dict[str, Any]:
        return {
            "thresholds": self.thresholds,
            "mode": self.mode,
            "staleness_age": self._age,
            "staleness_limit": self._limit,
        }

    _snapshot: Snapshot | None = None

    def _age(self, name: str) -> float:
        assert self._snapshot is not None
        return self._snapshot.age(name)

    def _limit(self, name: str) -> float:
        return self.staleness_s.get(name, STALENESS_DEFAULT_S)

    def evaluate(self, snapshot: Snapshot) -> EvalResult:
        self._snapshot = snapshot
        result = EvalResult()
        for rule in self.rules:
            d = rule.defn
            if d.sessions and snapshot.session_kind not in d.sessions:
                continue
            if snapshot.lap_num < d.min_lap:
                result.suppressed.append(Suppressed(rule, "min_lap"))
                continue
            stale = [r for r in d.requires if snapshot.age(r) >= self._limit(r)]
            if stale:
                result.suppressed.append(Suppressed(rule, "stale"))
                continue
            ns = make_namespace(snapshot, **self._ns_kwargs())
            try:
                fired = bool(rule.when(ns))
            except Exception:
                result.suppressed.append(Suppressed(rule, "expr_error"))
                continue
            if rule.clear_when is not None:
                try:
                    if rule.clear_when(ns):
                        rule.armed = True
                except Exception:
                    pass
            elif not fired:
                rule.armed = True
            if not (fired and rule.armed):
                continue
            rule.armed = False if rule.clear_when is not None else False
            if d.max_per_stint is not None and rule.fires_this_stint >= d.max_per_stint:
                result.suppressed.append(Suppressed(rule, "max_per_stint"))
                continue
            try:
                text = d.say.format_map(ns)
            except Exception:
                text = d.say
            snap_attrs = {name for name in dir(snapshot) if not name.startswith("_")}
            inputs = {
                name: ns.get(name) for name in dict.fromkeys(ns.accessed) if name in snap_attrs
            }
            still_true = None
            if rule._still_true is not None:
                ns_kwargs = self._ns_kwargs()

                def still_true(s: Snapshot, r: Rule = rule, k: dict[str, Any] = ns_kwargs) -> bool:
                    return r.still_true(s, k)

            result.candidates.append(
                Candidate(
                    rule=rule,
                    text=text,
                    priority=d.priority,
                    tags=list(d.tags),
                    still_true=still_true,
                    inputs=inputs,
                    trigger_t=snapshot.now,
                )
            )
        return result
