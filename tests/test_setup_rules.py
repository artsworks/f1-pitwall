from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from pitwall.cli import main
from pitwall.config.loader import ConfigStore
from pitwall.rules.expr import Predicate
from pitwall.setup.evaluate import Recommendation, evaluate, explain
from pitwall.setup.rules import Candidate, SetupRules, Symptom, parse_setup_rules
from pitwall.setup.signals import RunSignals, session_signals
from pitwall.store.db import Database

from .test_setup_state import _replay_practice_db


@pytest.fixture(scope="module")
def setup_config() -> tuple[SetupRules, dict[str, Any]]:
    settings = ConfigStore().current()
    return parse_setup_rules(settings.setup_rules), settings.thresholds


def _signals(**updates: Any) -> RunSignals:
    values: dict[str, Any] = {
        "session_uid": 123,
        "track_id": 7,
        "session_type": 1,
        "compound": 18,
        "setup_state_id": 2,
        "run_laps": 8,
        "event_laps": 8,
        "traction_exits_per10": 0.0,
        "lockups_rear_per10": 0.0,
        "lockups_front_per10": 0.0,
        "snaps_entry_per10": 0.0,
        "snaps_exit_per10": 0.0,
        "snap_phase": "",
        "slip_balance": 0.0,
        "wear_axle_ratio": 1.0,
        "z_front": 0.0,
        "z_rear": 0.0,
    }
    values.update(updates)
    return RunSignals(**values)


def _setup() -> dict[str, float]:
    return {
        "front_wing": 10,
        "rear_wing": 8,
        "on_throttle": 55,
        "off_throttle": 50,
        "brake_bias": 56,
        "front_left_tyre_pressure": 22.0,
        "front_right_tyre_pressure": 22.2,
        "rear_left_tyre_pressure": 22.0,
        "rear_right_tyre_pressure": 22.2,
        "rear_anti_roll_bar": 5,
        "front_anti_roll_bar": 5,
        "rear_suspension_height": 30,
        "rear_suspension": 5,
    }


@pytest.mark.parametrize(
    ("signals", "param", "delta"),
    [
        ({"lockups_rear_per10": 3.0}, "brake_bias", 1.0),
        ({"traction_exits_per10": 30.0}, "on_throttle", -5.0),
        (
            {"wear_axle_ratio": 1.2, "z_rear": 0.8},
            "rear_pressure",
            0.4,
        ),
        ({"slip_balance": 1.0}, "front_wing", 1.0),
        ({"slip_balance": -1.0}, "front_wing", -1.0),
    ],
    ids=[
        "entry-instability",
        "traction-limited",
        "rear-wear-limited",
        "understeer-balance",
        "oversteer-balance",
    ],
)
def test_each_symptom_has_a_primary_candidate(
    setup_config: tuple[SetupRules, dict[str, Any]],
    signals: dict[str, Any],
    param: str,
    delta: float,
) -> None:
    rules, thresholds = setup_config
    rules = replace(rules, confidence_floor="low")
    recommendations = evaluate(
        _signals(**signals),
        _setup(),
        mode="garage",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    primary = next(rec for rec in recommendations if rec.tier == "primary")
    assert primary.param == param
    assert primary.delta == delta


def test_race_mode_filters_parameters_and_uses_race_step(
    setup_config: tuple[SetupRules, dict[str, Any]],
) -> None:
    rules, thresholds = setup_config
    rules = replace(rules, confidence_floor="low")
    recommendations = evaluate(
        _signals(
            session_type=15,
            lockups_rear_per10=3.0,
            traction_exits_per10=30.0,
        ),
        _setup(),
        mode="race",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    assert {rec.param for rec in recommendations} <= {"brake_bias", "on_throttle"}
    throttle = next(rec for rec in recommendations if rec.param == "on_throttle")
    assert throttle.delta == -10.0


def test_quali_garage_parc_ferme_and_next_visit_contexts(
    setup_config: tuple[SetupRules, dict[str, Any]],
) -> None:
    rules, thresholds = setup_config
    rules = replace(rules, confidence_floor="low")
    signals = _signals(session_type=7, lockups_rear_per10=3.0, traction_exits_per10=30.0)
    setup = _setup() | {"brake_bias": 70.0}

    locked = evaluate(
        signals,
        setup,
        mode="garage",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    locked_items = explain(
        signals,
        setup,
        mode="garage",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    assert any(
        item["param"] == "rear_ride_height" and item["reason"] == "locked" for item in locked_items
    )
    assert any(
        item["param"] == "rear_anti_roll_bar" and item["reason"] == "locked"
        for item in locked_items
    )
    assert any(
        item["param"] == "brake_bias" and item["reason"] == "at_limit"
        for rec in locked
        for item in rec.suppressed
    )

    unlocked_entry = evaluate(
        replace(signals, traction_exits_per10=0.0),
        setup,
        mode="garage",
        parc_ferme=0,
        rules=rules,
        thresholds=thresholds,
    )
    assert "rear_ride_height" in {rec.param for rec in unlocked_entry}
    unlocked_traction = evaluate(
        replace(signals, lockups_rear_per10=0.0),
        setup | {"on_throttle": 10.0},
        mode="garage",
        parc_ferme=0,
        rules=rules,
        thresholds=thresholds,
    )
    assert "rear_anti_roll_bar" in {rec.param for rec in unlocked_traction}

    next_visit = evaluate(
        signals,
        setup,
        mode="debrief",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    next_visit_params = {rec.param for rec in next_visit if rec.tier == "next_visit"}
    assert {"rear_ride_height", "rear_anti_roll_bar"} <= next_visit_params

    unknown = explain(
        signals,
        setup,
        mode="garage",
        parc_ferme=-1,
        rules=rules,
        thresholds=thresholds,
    )
    assert any(
        item["param"] == "rear_anti_roll_bar" and item["reason"] == "locked" for item in unknown
    )


def test_race_debrief_uses_next_visit_tier(
    setup_config: tuple[SetupRules, dict[str, Any]],
) -> None:
    rules, thresholds = setup_config
    recs = evaluate(
        _signals(session_type=15, slip_balance=-1.0),
        _setup(),
        mode="debrief",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    assert recs
    assert all(rec.tier == "next_visit" for rec in recs)


def test_low_confidence_is_an_experiment_only_in_debrief(
    setup_config: tuple[SetupRules, dict[str, Any]],
) -> None:
    rules, thresholds = setup_config
    signals = _signals(traction_exits_per10=30.0)
    debrief = evaluate(
        signals,
        _setup(),
        mode="debrief",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    assert any(rec.param == "on_throttle" and rec.tier == "experiment" for rec in debrief)
    garage = evaluate(
        signals,
        _setup(),
        mode="garage",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    race = evaluate(
        replace(signals, session_type=15),
        _setup(),
        mode="race",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    assert all(rec.param != "on_throttle" for rec in garage + race)


def test_at_limit_falls_through_and_suppression_is_attached(
    setup_config: tuple[SetupRules, dict[str, Any]],
) -> None:
    rules, thresholds = setup_config
    recs = evaluate(
        _signals(lockups_rear_per10=3.0),
        _setup() | {"brake_bias": 70.0},
        mode="garage",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    primary = next(rec for rec in recs if rec.tier == "primary")
    assert primary.param == "off_throttle"
    assert {"param": "brake_bias", "reason": "at_limit"} in primary.suppressed


def test_opposite_sign_conflicts_drop_both_candidates(
    setup_config: tuple[SetupRules, dict[str, Any]],
) -> None:
    base_rules, thresholds = setup_config
    candidate_up = Candidate(
        param="front_wing",
        direction=1,
        magnitude=1,
        confidence="high",
        expect="more front grip",
        tradeoff="more drag",
    )
    candidate_down = replace(candidate_up, direction=-1)
    condition = Predicate("run_laps > 0")
    rules = SetupRules(
        params={"front_wing": base_rules.params["front_wing"]},
        symptoms=(
            Symptom("up", "run_laps > 0", condition, (candidate_up,)),
            Symptom("down", "run_laps > 0", condition, (candidate_down,)),
        ),
        by_z=(),
        max_alternatives=2,
        confidence_floor="low",
    )
    signals = _signals()
    recs = evaluate(
        signals,
        _setup(),
        mode="garage",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    assert not recs
    suppressed = explain(
        signals,
        _setup(),
        mode="garage",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    assert len([item for item in suppressed if item["reason"] == "conflict"]) == 2


def test_contra_suppression_is_exposed(
    setup_config: tuple[SetupRules, dict[str, Any]],
) -> None:
    rules, thresholds = setup_config
    signals = _signals(wear_axle_ratio=1.2, z_rear=0.8, slip_balance=1.0)
    recs = evaluate(
        signals,
        _setup(),
        mode="garage",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    assert all(rec.rule_id != "rear_wear_limited" for rec in recs)
    suppressed = explain(
        signals,
        _setup(),
        mode="garage",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    assert {"rule_id": "rear_wear_limited", "reason": "contra"} in suppressed


def test_all_none_signals_return_no_recommendations(
    setup_config: tuple[SetupRules, dict[str, Any]],
) -> None:
    rules, thresholds = setup_config
    signals = _signals(
        run_laps=0,
        event_laps=0,
        traction_exits_per10=None,
        lockups_rear_per10=None,
        lockups_front_per10=None,
        snaps_entry_per10=None,
        snaps_exit_per10=None,
        slip_balance=None,
        wear_axle_ratio=None,
        z_front=None,
        z_rear=None,
    )
    assert (
        evaluate(
            signals,
            _setup(),
            mode="garage",
            parc_ferme=1,
            rules=rules,
            thresholds=thresholds,
        )
        == []
    )


def test_repeated_evaluation_is_deterministic(
    setup_config: tuple[SetupRules, dict[str, Any]],
) -> None:
    rules, thresholds = setup_config
    args = (
        _signals(traction_exits_per10=30.0),
        _setup(),
    )
    first = evaluate(
        *args,
        mode="debrief",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    second = evaluate(
        *args,
        mode="debrief",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    assert first == second


def test_alternative_cap_applies_across_all_symptoms(
    setup_config: tuple[SetupRules, dict[str, Any]],
) -> None:
    rules, thresholds = setup_config
    signals = _signals(lockups_rear_per10=3.0, traction_exits_per10=30.0)
    recs = evaluate(
        signals,
        _setup(),
        mode="garage",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    assert len([rec for rec in recs if rec.tier == "alternative"]) == 2


def test_profile_and_rules_directory_deep_merge_setup_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "setup.yaml").write_text(
        "setup_rules:\n  params:\n    front_wing:\n      step: 2\n"
    )
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "setup_rules:\n  params:\n    front_wing:\n      contexts: [practice_garage]\n"
    )
    monkeypatch.setenv("PITWALL_PROFILE", str(profile))
    settings = ConfigStore(rules_dir=rules_dir).current()
    rules = parse_setup_rules(settings.setup_rules)
    assert rules.params["front_wing"].step == 2
    assert rules.params["front_wing"].contexts == ("practice_garage",)
    assert "next_visit" in rules.params["rear_wing"].contexts


def test_unknown_parameter_and_context_raise_clear_errors(
    setup_config: tuple[SetupRules, dict[str, Any]],
) -> None:
    del setup_config
    with pytest.raises(ValueError, match="unknown parameter"):
        parse_setup_rules({"params": {"tyre_magic": {"step": 1}}})
    settings = ConfigStore().current()
    changed = yaml.safe_load(yaml.safe_dump(settings.setup_rules))
    changed["params"]["front_wing"]["contexts"] = ["garage"]
    with pytest.raises(ValueError, match="unknown context"):
        parse_setup_rules(changed)


def test_session_signals_use_green_laps_and_slip_baselines(
    tmp_path: Path, setup_config: tuple[SetupRules, dict[str, Any]]
) -> None:
    _, thresholds = setup_config
    db_one, uid_one = _replay_practice_db(tmp_path, change_setup=False, name="single")
    one_state = session_signals(db_one, uid_one, thresholds)
    assert one_state is not None
    assert one_state.traction_exits_per10 == pytest.approx(3.75)
    assert one_state.slip_balance is None

    db_two, uid_two = _replay_practice_db(tmp_path, change_setup=True, name="double")
    two_states = session_signals(db_two, uid_two, thresholds)
    assert two_states is not None
    assert two_states.event_laps >= 3
    assert two_states.slip_balance is not None


def test_setup_cli_json_smoke_and_default_latest_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "cli.sqlite"
    db, _ = _replay_practice_db(tmp_path, db_path=db_path, name="cli")
    assert main(["setup", "--json", "--db", str(db_path)]) == 0
    output = capsys.readouterr().out
    assert isinstance(json.loads(output), list)
    assert db.latest_session_with_laps_uid() is not None


def test_setup_recommendations_round_trip_in_database() -> None:
    db = Database(":memory:")
    recommendation = Recommendation(
        rec_id="11:setup:garage:traction_limited:on_throttle",
        rule_id="traction_limited",
        mode="garage",
        tier="primary",
        param="on_throttle",
        from_value=50.0,
        delta=-5.0,
        to_value=45.0,
        conf="medium",
        expect="less wheelspin",
        tradeoff="slower exits",
        evidence={"traction_exits_per10": 30.0},
        setup_state_id=4,
        session_type=1,
        parc_ferme=1,
        suppressed=({"param": "rear_anti_roll_bar", "reason": "locked"},),
    )
    db.insert_setup_rec(recommendation, track_id=7, compound=18, lap=8)
    rows = db.setup_recs_for_session(11)
    assert len(rows) == 1
    assert rows[0]["rec_id"] == recommendation.rec_id
    assert rows[0]["evidence"]["signals"] == {"traction_exits_per10": 30.0}
    assert rows[0]["evidence"]["suppressed"] == [
        {"param": "rear_anti_roll_bar", "reason": "locked"}
    ]
