"""Pure setup recommendation evaluation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from pitwall.rules.expr import Predicate, TrackedNamespace
from pitwall.setup.rules import Candidate, SetupRules, Symptom
from pitwall.setup.signals import RunSignals

CONFIDENCE_LEVEL = {"low": 0, "medium": 1, "high": 2}
RACE_PARAMS = frozenset({"brake_bias", "on_throttle"})
PRESSURE_RIGHT_FIELDS = {
    "front_pressure": "front_right_tyre_pressure",
    "rear_pressure": "rear_right_tyre_pressure",
}


@dataclass(frozen=True, slots=True)
class Recommendation:
    rec_id: str
    rule_id: str
    mode: str
    tier: str
    param: str
    from_value: float
    delta: float
    to_value: float
    conf: str
    expect: str
    tradeoff: str
    evidence: dict[str, Any]
    setup_state_id: int | None
    session_type: int
    parc_ferme: int
    suppressed: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _Proposal:
    symptom: Symptom
    candidate: Candidate
    from_value: float
    delta: float
    to_value: float
    evidence: dict[str, Any]
    context_tier: str


def _session_kind(session_type: int) -> str | None:
    if 1 <= session_type <= 4:
        return "practice"
    if 5 <= session_type <= 14:
        return "quali"
    if 15 <= session_type <= 17:
        return "race"
    return None


def _predicate_result(
    predicate: Predicate, environment: Mapping[str, Any]
) -> tuple[bool, tuple[str, ...]]:
    namespace = TrackedNamespace(environment)
    try:
        result = bool(predicate(namespace))
    except (TypeError, KeyError, AttributeError):
        result = False
    return result, tuple(namespace.accessed)


def _context_for(mode: str, kind: str) -> str:
    if mode == "garage":
        return f"{kind}_garage"
    if mode == "race":
        return "race_track"
    if kind == "practice":
        return "practice_garage"
    if kind == "quali":
        return "race_garage"
    return "next_visit"


def _field_value(param: str, setup_field: str, setup: Mapping[str, float]) -> float | None:
    if param in PRESSURE_RIGHT_FIELDS:
        right_field = PRESSURE_RIGHT_FIELDS[param]
        if setup_field not in setup or right_field not in setup:
            return None
        return (float(setup[setup_field]) + float(setup[right_field])) / 2.0
    if setup_field not in setup:
        return None
    return float(setup[setup_field])


def _magnitude_steps(candidate: Candidate, rules: SetupRules, signals: RunSignals) -> float:
    if candidate.magnitude != "by_z":
        return float(candidate.magnitude)
    z_value = signals.z_rear
    steps = 1
    if z_value is not None:
        for threshold, threshold_steps in rules.by_z:
            if abs(z_value) >= threshold:
                steps = threshold_steps
    return float(steps)


def _setup_context_allowed(
    param_contexts: tuple[str, ...],
    context: str,
    parc_ferme: int,
) -> bool:
    if context in param_contexts:
        return True
    return context.endswith("_garage") and parc_ferme == 0 and "practice_garage" in param_contexts


def _candidate_context_tier(
    *,
    mode: str,
    kind: str,
    context: str,
    param_contexts: tuple[str, ...],
    parc_ferme: int,
) -> str | None:
    if _setup_context_allowed(param_contexts, context, parc_ferme):
        if mode == "debrief" and kind == "race":
            return "next_visit"
        return ""
    if mode == "debrief" and kind == "quali" and "next_visit" in param_contexts:
        return "next_visit"
    return None


def _evaluate(
    signals: RunSignals,
    setup: Mapping[str, float],
    *,
    mode: str,
    parc_ferme: int,
    rules: SetupRules,
    thresholds: Mapping[str, Any],
) -> tuple[list[Recommendation], list[dict[str, str]]]:
    if mode not in {"debrief", "garage", "race"}:
        raise ValueError("mode must be debrief, garage, or race")
    kind = _session_kind(signals.session_type)
    if kind is None or (mode == "race" and kind != "race"):
        return [], []

    context = _context_for(mode, kind)
    environment: dict[str, Any] = {
        field.name: getattr(signals, field.name) for field in fields(RunSignals)
    }
    environment.update(thresholds)

    symptom_evidence: dict[str, dict[str, Any]] = {}
    fired: list[Symptom] = []
    suppressed: list[dict[str, str]] = []
    local_suppressed: dict[str, list[dict[str, str]]] = defaultdict(list)
    for symptom in rules.symptoms:
        active, accessed = _predicate_result(symptom.when, environment)
        if not active:
            continue
        evidence = {name: environment[name] for name in accessed if name in environment}
        evidence.update(
            run_laps=signals.run_laps,
            event_laps=signals.event_laps,
            compound=signals.compound,
            setup_state_id=signals.setup_state_id,
        )
        symptom_evidence[symptom.rule_id] = evidence
        contradicted = any(
            _predicate_result(predicate, environment)[0] for predicate in symptom.contra
        )
        if contradicted:
            item = {"rule_id": symptom.rule_id, "reason": "contra"}
            suppressed.append(item)
            local_suppressed[symptom.rule_id].append({"param": "*", "reason": "contra"})
            continue
        fired.append(symptom)

    proposals_by_symptom: dict[str, list[_Proposal]] = {}
    signs_by_param: dict[str, set[int]] = defaultdict(set)
    for symptom in fired:
        proposals: list[_Proposal] = []
        for candidate in symptom.candidates:
            spec = rules.params[candidate.param]
            if mode == "race" and candidate.param not in RACE_PARAMS:
                item = {"param": candidate.param, "reason": "locked"}
                local_suppressed[symptom.rule_id].append(item)
                suppressed.append({"rule_id": symptom.rule_id, **item})
                continue

            context_tier = _candidate_context_tier(
                mode=mode,
                kind=kind,
                context=context,
                param_contexts=spec.contexts,
                parc_ferme=parc_ferme,
            )
            if context_tier is None:
                item = {"param": candidate.param, "reason": "locked"}
                local_suppressed[symptom.rule_id].append(item)
                suppressed.append({"rule_id": symptom.rule_id, **item})
                continue

            below_floor = (
                CONFIDENCE_LEVEL[candidate.confidence] < CONFIDENCE_LEVEL[rules.confidence_floor]
            )
            if below_floor and mode != "debrief":
                item = {"param": candidate.param, "reason": "below_floor"}
                local_suppressed[symptom.rule_id].append(item)
                suppressed.append({"rule_id": symptom.rule_id, **item})
                continue

            if (
                candidate.condition is not None
                and not _predicate_result(candidate.condition, environment)[0]
            ):
                continue

            from_value = _field_value(candidate.param, spec.setup_field, setup)
            if from_value is None:
                continue
            step = (
                spec.race_step
                if mode == "race" and candidate.param == "on_throttle" and spec.race_step
                else spec.step
            )
            delta = candidate.direction * _magnitude_steps(candidate, rules, signals) * step
            to_value = round(from_value + delta, 6)
            if (spec.min_value is not None and to_value < spec.min_value) or (
                spec.max_value is not None and to_value > spec.max_value
            ):
                item = {"param": candidate.param, "reason": "at_limit"}
                local_suppressed[symptom.rule_id].append(item)
                suppressed.append({"rule_id": symptom.rule_id, **item})
                continue

            proposal = _Proposal(
                symptom=symptom,
                candidate=candidate,
                from_value=from_value,
                delta=round(delta, 6),
                to_value=to_value,
                evidence=symptom_evidence[symptom.rule_id],
                context_tier=context_tier,
            )
            proposals.append(proposal)
            signs_by_param[candidate.param].add(candidate.direction)
        proposals_by_symptom[symptom.rule_id] = proposals

    conflicted = {param for param, signs in signs_by_param.items() if len(signs) > 1}
    for symptom in fired:
        proposals = proposals_by_symptom[symptom.rule_id]
        retained: list[_Proposal] = []
        for proposal in proposals:
            if proposal.candidate.param in conflicted:
                item = {"param": proposal.candidate.param, "reason": "conflict"}
                local_suppressed[symptom.rule_id].append(item)
                suppressed.append({"rule_id": symptom.rule_id, **item})
            else:
                retained.append(proposal)
        proposals_by_symptom[symptom.rule_id] = retained

    def make_rec(proposal: _Proposal, tier: str) -> Recommendation:
        candidate = proposal.candidate
        return Recommendation(
            rec_id=(
                f"{signals.session_uid}:setup:{mode}:{proposal.symptom.rule_id}:{candidate.param}"
            ),
            rule_id=proposal.symptom.rule_id,
            mode=mode,
            tier=tier,
            param=candidate.param,
            from_value=proposal.from_value,
            delta=proposal.delta,
            to_value=proposal.to_value,
            conf=candidate.confidence,
            expect=candidate.expect,
            tradeoff=candidate.tradeoff,
            evidence=dict(proposal.evidence),
            setup_state_id=signals.setup_state_id,
            session_type=signals.session_type,
            parc_ferme=parc_ferme,
            suppressed=tuple(dict(item) for item in local_suppressed[proposal.symptom.rule_id]),
        )

    normal_by_symptom: dict[str, list[_Proposal]] = {}
    experiments: list[_Proposal] = []
    next_visit: list[_Proposal] = []
    for symptom in fired:
        normal: list[_Proposal] = []
        for proposal in proposals_by_symptom[symptom.rule_id]:
            below_floor = (
                CONFIDENCE_LEVEL[proposal.candidate.confidence]
                < CONFIDENCE_LEVEL[rules.confidence_floor]
            )
            if below_floor:
                experiments.append(proposal)
            elif proposal.context_tier == "next_visit":
                next_visit.append(proposal)
            else:
                normal.append(proposal)
        normal_by_symptom[symptom.rule_id] = normal

    primary_symptom: str | None = None
    result: list[Recommendation] = []
    for symptom in fired:
        proposals = normal_by_symptom[symptom.rule_id]
        if proposals:
            primary_symptom = symptom.rule_id
            result.append(make_rec(proposals[0], "primary"))
            break

    alternatives_added = 0
    if primary_symptom is not None:
        primary_candidates = normal_by_symptom[primary_symptom]
        for proposal in primary_candidates[1:]:
            if alternatives_added >= rules.max_alternatives:
                break
            result.append(make_rec(proposal, "alternative"))
            alternatives_added += 1
        for symptom in fired:
            if symptom.rule_id == primary_symptom or alternatives_added >= rules.max_alternatives:
                continue
            proposals = normal_by_symptom[symptom.rule_id]
            if proposals:
                result.append(make_rec(proposals[0], "alternative"))
                alternatives_added += 1

    result.extend(make_rec(proposal, "next_visit") for proposal in next_visit)
    result.extend(make_rec(proposal, "experiment") for proposal in experiments)
    return result, suppressed


def evaluate(
    signals: RunSignals,
    setup: Mapping[str, float],
    *,
    mode: str,
    parc_ferme: int,
    rules: SetupRules,
    thresholds: Mapping[str, Any],
) -> list[Recommendation]:
    """Return deterministic recommendations for the supplied run and setup."""
    recommendations, _ = _evaluate(
        signals,
        setup,
        mode=mode,
        parc_ferme=parc_ferme,
        rules=rules,
        thresholds=thresholds,
    )
    return recommendations


def explain(
    signals: RunSignals,
    setup: Mapping[str, float],
    *,
    mode: str,
    parc_ferme: int,
    rules: SetupRules,
    thresholds: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Return candidate and symptom suppressions for a setup evaluation."""
    _, suppressed = _evaluate(
        signals,
        setup,
        mode=mode,
        parc_ferme=parc_ferme,
        rules=rules,
        thresholds=thresholds,
    )
    return suppressed
