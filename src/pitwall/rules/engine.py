"""Rule engine: evaluate declarative rules against a snapshot.

Per-rule hysteresis: fires on the `when` rising edge, re-arms on `clear_when`
(or on `when` going false if no clear_when). `requires` packets must be fresh.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pitwall.config.models import RuleDefModel
from pitwall.rules.expr import (
    Predicate,
    TrackedNamespace,
    make_namespace,
    namespace_data,
    public_names,
)
from pitwall.rules.phrases import PhraseBook
from pitwall.state.session import Snapshot

STALENESS_DEFAULT_S = 1.0


def _jsonable(value: Any) -> Any:
    """Decision-log inputs must survive json.dumps (JSONL and SQLite mirrors)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


class _WithRepeat(dict[str, Any]):
    """Format mapping: the predicate namespace plus `repeat`, the number of
    times this call triggered inside its repeat window (1 = first time)."""

    def __init__(self, ns: Mapping[str, Any], repeat: int) -> None:
        super().__init__(repeat=repeat)
        self._ns = ns

    def __missing__(self, key: str) -> Any:
        value = self._ns[key]
        return _OneDecimal(value) if type(value) is float else value


class _OneDecimal(float):
    """Float that renders to one decimal when a template gives no format spec."""

    def __format__(self, spec: str) -> str:
        if spec:
            return format(float(self), spec)
        text = f"{float(self):.1f}"
        return text[:-2] if text.endswith(".0") else text


@dataclass(slots=True)
class Candidate:
    rule: Rule
    text: str
    priority: int
    tags: list[str]
    still_true: Callable[[Snapshot], bool] | None
    inputs: dict[str, Any]
    trigger_t: float
    screen_only: bool = False
    current: Callable[[Snapshot], bool] | None = None
    outcome_score: float | None = None
    obvious: bool = False
    provisional_silent: bool = False
    brief: str = ""


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
        self.phrases = PhraseBook(defn)
        self.severity = [Predicate(t.when) for t in defn.severity]
        self._outcome_score = Predicate(defn.outcome_score) if defn.outcome_score else None
        self._obvious = Predicate(defn.obvious_when) if defn.obvious_when else None
        self._worst_case = Predicate(defn.worst_case_when) if defn.worst_case_when else None

    def extras(self, ns: TrackedNamespace) -> tuple[float | None, bool, bool]:
        try:
            score = self._outcome_score(ns) if self._outcome_score is not None else None
            if isinstance(score, bool) or not isinstance(score, int | float):
                score = None
            else:
                score = float(score)
                if not math.isfinite(score):
                    score = None
            obvious = bool(self._obvious(ns)) if self._obvious is not None else False
            worst_case = bool(self._worst_case(ns)) if self._worst_case is not None else True
            provisional_silent = self.defn.provisional and not (
                self.defn.worst_case_urgent and worst_case
            )
            return score, obvious, provisional_silent
        except Exception:
            return None, False, False

    def still_true(self, snapshot: Snapshot, ns_kwargs: dict[str, Any]) -> bool:
        if self._still_true is None:
            return True
        ns = make_namespace(snapshot, **ns_kwargs)
        return bool(self._still_true(ns))

    def holds(self, snapshot: Snapshot, ns_kwargs: dict[str, Any]) -> bool:
        ns = make_namespace(snapshot, **ns_kwargs)
        return bool(self.when(ns))


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

    def candidate_for(self, rule_id: str, snapshot: Snapshot, text: str | None = None) -> Candidate:
        """Build a rule candidate for replay without changing its trigger state."""
        self._snapshot = snapshot
        rule = next(item for item in self.rules if item.id == rule_id)
        data = namespace_data(snapshot, **self._ns_kwargs())
        ns = TrackedNamespace(data)
        severity = 0
        for i, pred in enumerate(rule.severity, start=1):
            try:
                if pred(ns):
                    severity = i
                    break
            except Exception:
                continue
        tier_priority = rule.defn.severity[severity - 1].priority if severity else None
        priority = tier_priority or rule.defn.priority
        template = text
        if template is None:
            _, pool = rule.phrases.pool(1, severity)
            template = pool[0] if pool else ""
        try:
            rendered = template.format_map(_WithRepeat(ns, 1))
        except Exception:
            rendered = template
        outcome_score, obvious, provisional_silent = rule.extras(ns)
        snap_attrs = frozenset(public_names(snapshot))
        inputs = {
            name: _jsonable(ns.get(name))
            for name in dict.fromkeys(ns.accessed)
            if name in snap_attrs
        }
        ns_kwargs = self._ns_kwargs()
        still_true = None
        if rule._still_true is not None:

            def still_true(s: Snapshot, r: Rule = rule, k: dict[str, Any] = ns_kwargs) -> bool:
                return r.still_true(s, k)

        current = None
        d = rule.defn
        if d.conflict_group or d.supersedes or d.rotate_with:
            if still_true is not None:
                current = still_true
            else:

                def current(s: Snapshot, r: Rule = rule, k: dict[str, Any] = ns_kwargs) -> bool:
                    return r.holds(s, k)

        brief = ""
        if d.brief:
            try:
                brief = d.brief.format_map(_WithRepeat(ns, 1))
            except Exception:
                brief = d.brief
        return Candidate(
            rule=rule,
            text=rendered,
            priority=priority,
            tags=list(d.tags),
            still_true=still_true,
            inputs=inputs,
            trigger_t=snapshot.now,
            screen_only=d.screen_only,
            current=current,
            outcome_score=outcome_score,
            obvious=obvious,
            provisional_silent=provisional_silent,
            brief=brief,
        )

    def evaluate(self, snapshot: Snapshot) -> EvalResult:
        self._snapshot = snapshot
        result = EvalResult()
        data = namespace_data(snapshot, **self._ns_kwargs())
        snap_attrs = frozenset(public_names(snapshot))
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
            ns = TrackedNamespace(data)
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
            repeat = rule.phrases.trigger(snapshot.now)
            severity = 0
            for i, pred in enumerate(rule.severity, start=1):
                try:
                    if pred(ns):
                        severity = i
                        break
                except Exception:
                    continue
            template = rule.phrases.pick(repeat, severity)
            tier_priority = d.severity[severity - 1].priority if severity else None
            priority = tier_priority or d.priority
            try:
                text = template.format_map(_WithRepeat(ns, repeat))
            except Exception:
                text = template
            outcome_score, obvious, provisional_silent = rule.extras(ns)
            brief = ""
            if d.brief:
                try:
                    brief = d.brief.format_map(_WithRepeat(ns, repeat))
                except Exception:
                    brief = d.brief
            inputs = {
                name: _jsonable(ns.get(name))
                for name in dict.fromkeys(ns.accessed)
                if name in snap_attrs
            }
            still_true = None
            current = None
            ns_kwargs = self._ns_kwargs()
            if rule._still_true is not None:

                def still_true(s: Snapshot, r: Rule = rule, k: dict[str, Any] = ns_kwargs) -> bool:
                    return r.still_true(s, k)

            if d.conflict_group or d.supersedes or d.rotate_with:
                if rule._still_true is not None:
                    current = still_true
                else:

                    def current(s: Snapshot, r: Rule = rule, k: dict[str, Any] = ns_kwargs) -> bool:
                        return r.holds(s, k)

            result.candidates.append(
                Candidate(
                    rule=rule,
                    text=text,
                    priority=priority,
                    tags=list(d.tags),
                    still_true=still_true,
                    inputs=inputs,
                    trigger_t=snapshot.now,
                    screen_only=d.screen_only,
                    current=current,
                    outcome_score=outcome_score,
                    obvious=obvious,
                    provisional_silent=provisional_silent,
                    brief=brief,
                )
            )
        return result
