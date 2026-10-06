"""Typed setup-rule configuration and compiled predicates."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from pitwall.protocol.layouts import CAR_SETUP_CAR
from pitwall.rules.expr import Predicate

CONTEXTS = frozenset(
    {
        "practice_garage",
        "practice_track",
        "quali_garage",
        "quali_track",
        "race_garage",
        "race_track",
        "race_stop",
        "next_visit",
    }
)
CONFIDENCE = ("low", "medium", "high")
PARAMETERS = frozenset(
    {
        "front_wing",
        "rear_wing",
        "on_throttle",
        "off_throttle",
        "brake_bias",
        "front_pressure",
        "rear_pressure",
        "rear_anti_roll_bar",
        "front_anti_roll_bar",
        "rear_ride_height",
        "rear_suspension",
    }
)
SETUP_FIELDS = frozenset(item.name for item in CAR_SETUP_CAR)


@dataclass(frozen=True, slots=True)
class ParamSpec:
    setup_field: str
    step: float
    contexts: tuple[str, ...]
    race_step: float | None = None
    min_value: float | None = None
    max_value: float | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    param: str
    direction: int
    magnitude: float | str
    confidence: str
    expect: str
    tradeoff: str
    condition_source: str | None = None
    condition: Predicate | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class Symptom:
    rule_id: str
    when_source: str
    when: Predicate = field(repr=False, compare=False)
    candidates: tuple[Candidate, ...]
    contra_sources: tuple[str, ...] = ()
    contra: tuple[Predicate, ...] = field(default=(), repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SetupRules:
    params: Mapping[str, ParamSpec]
    symptoms: tuple[Symptom, ...]
    by_z: tuple[tuple[float, int], ...]
    max_alternatives: int
    confidence_floor: str
    version: int = 1


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"setup_rules.{where} must be a mapping")
    return value


def _unknown_keys(value: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"setup_rules.{where}: unknown key(s): {', '.join(unknown)}")


def _compile(source: str, where: str) -> Predicate:
    try:
        return Predicate(source)
    except ValueError as exc:
        raise ValueError(f"setup_rules.{where}: {exc}") from exc


def parse_setup_rules(data: Mapping[str, Any]) -> SetupRules:
    """Validate setup-rule data and compile each expression once."""
    if "setup_rules" in data:
        data = _mapping(data["setup_rules"], "setup_rules")
    _unknown_keys(
        data,
        {"version", "defaults", "params", "symptoms", "magnitudes"},
        "",
    )

    version = int(data.get("version", 1))
    if version != 1:
        raise ValueError(f"setup_rules.version: unsupported version {version}")

    defaults = _mapping(data.get("defaults", {}), "defaults")
    _unknown_keys(
        defaults,
        {"max_alternatives", "confidence_floor", "min_run_laps", "min_runs"},
        "defaults",
    )
    max_alternatives = int(defaults.get("max_alternatives", 2))
    if max_alternatives < 0:
        raise ValueError("setup_rules.defaults.max_alternatives must be non-negative")
    confidence_floor = str(defaults.get("confidence_floor", "medium"))
    if confidence_floor not in CONFIDENCE:
        raise ValueError("setup_rules.defaults.confidence_floor must be low, medium, or high")

    param_data = _mapping(data.get("params", {}), "params")
    params: dict[str, ParamSpec] = {}
    setup_fields = set(SETUP_FIELDS)
    for name, raw_spec in param_data.items():
        if name not in PARAMETERS:
            raise ValueError(f"setup_rules.params: unknown parameter {name!r}")
        spec = _mapping(raw_spec, f"params.{name}")
        _unknown_keys(
            spec,
            {"setup_field", "step", "race_step", "min", "max", "contexts"},
            f"params.{name}",
        )
        setup_field = str(spec.get("setup_field", ""))
        if setup_field not in setup_fields:
            raise ValueError(
                f"setup_rules.params.{name}.setup_field: unknown Car Setups field {setup_field!r}"
            )
        contexts_raw = spec.get("contexts", [])
        if not isinstance(contexts_raw, list):
            raise ValueError(f"setup_rules.params.{name}.contexts must be a list")
        contexts = tuple(str(context) for context in contexts_raw)
        unknown_contexts = sorted(set(contexts) - CONTEXTS)
        if unknown_contexts:
            raise ValueError(
                f"setup_rules.params.{name}.contexts: unknown context(s): "
                f"{', '.join(unknown_contexts)}"
            )
        step = float(spec.get("step", 0.0))
        race_step = float(spec["race_step"]) if "race_step" in spec else None
        if step <= 0 or (race_step is not None and race_step <= 0):
            raise ValueError(f"setup_rules.params.{name}: step sizes must be positive")
        min_value = float(spec["min"]) if "min" in spec else None
        max_value = float(spec["max"]) if "max" in spec else None
        if min_value is not None and max_value is not None and min_value > max_value:
            raise ValueError(f"setup_rules.params.{name}: min must not exceed max")
        params[str(name)] = ParamSpec(
            setup_field=setup_field,
            step=step,
            race_step=race_step,
            min_value=min_value,
            max_value=max_value,
            contexts=contexts,
        )

    symptom_data = _mapping(data.get("symptoms", {}), "symptoms")
    symptoms: list[Symptom] = []
    for rule_id, raw_symptom in symptom_data.items():
        symptom = _mapping(raw_symptom, f"symptoms.{rule_id}")
        _unknown_keys(
            symptom,
            {"when", "candidates", "contra"},
            f"symptoms.{rule_id}",
        )
        when_source = str(symptom.get("when", ""))
        if not when_source:
            raise ValueError(f"setup_rules.symptoms.{rule_id}.when is required")
        when = _compile(when_source, f"symptoms.{rule_id}.when")
        raw_candidates = symptom.get("candidates", [])
        if not isinstance(raw_candidates, list):
            raise ValueError(f"setup_rules.symptoms.{rule_id}.candidates must be a list")
        candidates: list[Candidate] = []
        for index, raw_candidate in enumerate(raw_candidates):
            where = f"symptoms.{rule_id}.candidates[{index}]"
            candidate = _mapping(raw_candidate, where)
            _unknown_keys(
                candidate,
                {"param", "dir", "mag", "if", "conf", "expect", "tradeoff"},
                where,
            )
            param = str(candidate.get("param", ""))
            if param not in params:
                raise ValueError(f"setup_rules.{where}: unknown parameter {param!r}")
            direction = int(candidate.get("dir", 0))
            if direction not in {-1, 1}:
                raise ValueError(f"setup_rules.{where}.dir must be -1 or 1")
            magnitude_raw = candidate.get("mag", 0)
            magnitude: float | str
            if magnitude_raw == "by_z":
                magnitude = "by_z"
            else:
                magnitude = float(magnitude_raw)
                if magnitude <= 0:
                    raise ValueError(f"setup_rules.{where}.mag must be positive")
            confidence = str(candidate.get("conf", ""))
            if confidence not in CONFIDENCE:
                raise ValueError(f"setup_rules.{where}.conf must be low, medium, or high")
            condition_source = str(candidate["if"]) if "if" in candidate else None
            condition = _compile(condition_source, f"{where}.if") if condition_source else None
            candidates.append(
                Candidate(
                    param=param,
                    direction=direction,
                    magnitude=magnitude,
                    confidence=confidence,
                    expect=str(candidate.get("expect", "")),
                    tradeoff=str(candidate.get("tradeoff", "")),
                    condition_source=condition_source,
                    condition=condition,
                )
            )

        raw_contra = symptom.get("contra", [])
        if not isinstance(raw_contra, list):
            raise ValueError(f"setup_rules.symptoms.{rule_id}.contra must be a list")
        contra_sources = tuple(str(source) for source in raw_contra)
        contra = tuple(
            _compile(source, f"symptoms.{rule_id}.contra[{index}]")
            for index, source in enumerate(contra_sources)
        )
        symptoms.append(
            Symptom(
                rule_id=str(rule_id),
                when_source=when_source,
                when=when,
                candidates=tuple(candidates),
                contra_sources=contra_sources,
                contra=contra,
            )
        )

    magnitude_data = _mapping(data.get("magnitudes", {}), "magnitudes")
    _unknown_keys(magnitude_data, {"by_z"}, "magnitudes")
    raw_by_z = magnitude_data.get("by_z", [])
    if not isinstance(raw_by_z, list):
        raise ValueError("setup_rules.magnitudes.by_z must be a list")
    by_z: list[tuple[float, int]] = []
    for index, item in enumerate(raw_by_z):
        entry = _mapping(item, f"magnitudes.by_z[{index}]")
        _unknown_keys(entry, {"z", "steps"}, f"magnitudes.by_z[{index}]")
        z, steps = float(entry.get("z", 0)), int(entry.get("steps", 0))
        if z <= 0 or steps <= 0:
            raise ValueError(f"setup_rules.magnitudes.by_z[{index}] values must be positive")
        by_z.append((z, steps))

    return SetupRules(
        params=MappingProxyType(params),
        symptoms=tuple(symptoms),
        by_z=tuple(sorted(by_z)),
        max_alternatives=max_alternatives,
        confidence_floor=confidence_floor,
        version=version,
    )
