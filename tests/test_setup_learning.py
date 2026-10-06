from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from pitwall.config.loader import ConfigStore
from pitwall.digest import build_digest
from pitwall.hindsight import grade_and_store
from pitwall.setup.evaluate import Recommendation, evaluate
from pitwall.setup.learn import learned_gains
from pitwall.setup.rules import SetupRules, parse_setup_rules
from pitwall.setup.signals import RunSignals, session_signals
from pitwall.state.lap import LapSummary
from pitwall.store.db import Database
from pitwall.tune import tune_from_db


def _config() -> tuple[SetupRules, dict[str, Any]]:
    settings = ConfigStore().current()
    return parse_setup_rules(settings.setup_rules), dict(settings.thresholds)


def _fields(**updates: float) -> dict[str, float]:
    fields = {
        "front_wing": 10.0,
        "rear_wing": 8.0,
        "on_throttle": 55.0,
        "off_throttle": 50.0,
        "brake_bias": 56.0,
        "front_left_tyre_pressure": 22.0,
        "front_right_tyre_pressure": 22.2,
        "rear_left_tyre_pressure": 22.0,
        "rear_right_tyre_pressure": 22.2,
        "rear_anti_roll_bar": 5.0,
        "front_anti_roll_bar": 5.0,
        "rear_suspension_height": 30.0,
        "rear_suspension": 5.0,
        "fuel_load": 30.0,
    }
    fields.update(updates)
    return fields


def _insert_run(
    db: Database,
    uid: int,
    *,
    state_id: int,
    start_lap: int,
    start_age: int,
    count: int,
    lockups_rear: int,
    slip: float,
    lap_slope_ms: float,
) -> None:
    for offset in range(count):
        lap_num = start_lap + offset
        age = start_age + offset
        db.insert_lap(
            uid,
            0,
            LapSummary(
                lap_num=lap_num,
                lap_time_ms=90_000 + int(lap_slope_ms * offset),
                sector1_ms=30_000,
                sector2_ms=30_000,
                compound=18,
                tyre_age_laps=age,
                fuel_remaining_laps_at_end=2.0,
                valid=True,
                lockups_rear=lockups_rear,
                slip_balance_deg=slip,
                slip_samples=10,
            ),
            setup_state_id=state_id,
        )


def _recommendation(
    uid: int,
    state_id: int,
    *,
    rule_id: str = "entry_instability",
    param: str = "brake_bias",
    from_value: float = 56.0,
    delta: float = 1.0,
    tier: str = "primary",
) -> Recommendation:
    return Recommendation(
        rec_id=f"{uid}:setup:debrief:{rule_id}:{param}",
        rule_id=rule_id,
        mode="debrief",
        tier=tier,
        param=param,
        from_value=from_value,
        delta=delta,
        to_value=from_value + delta,
        conf="high" if tier != "experiment" else "low",
        expect="more front braking",
        tradeoff="fronts lock earlier",
        evidence={"lockups_rear_per10": 10.0, "event_laps": 6},
        setup_state_id=state_id,
        session_type=1,
        parc_ferme=1,
    )


def _case(
    *,
    uid: int = 801,
    after_fields: dict[str, float] | None = None,
    after_laps: int | None = 6,
    after_slope_ms: float = 0.0,
) -> tuple[Database, int]:
    db = Database(":memory:")
    before_fields = _fields()
    after_fields = after_fields or _fields(brake_bias=57.0)
    db.upsert_session(uid, track_id=7, session_type=1, started_at=float(uid), parc_ferme=1)
    before_state = db.setup_state_id(f"setup-learning-{uid}-before", before_fields)
    after_state = db.setup_state_id(f"setup-learning-{uid}-after", after_fields)
    _insert_run(
        db,
        uid,
        state_id=before_state,
        start_lap=1,
        start_age=1,
        count=6,
        lockups_rear=1,
        slip=2.0,
        lap_slope_ms=0.0,
    )
    if after_laps is not None:
        _insert_run(
            db,
            uid,
            state_id=after_state,
            start_lap=7,
            start_age=7,
            count=after_laps,
            lockups_rear=0,
            slip=4.0,
            lap_slope_ms=after_slope_ms,
        )
    rec = _recommendation(uid, before_state)
    db.insert_setup_rec(rec, track_id=7, compound=18, lap=6)
    return db, before_state


def _outcome(db: Database, uid: int, thresholds: dict[str, Any]) -> dict[str, Any]:
    outcomes = grade_and_store(db, uid, thresholds)
    return next(item.row() for item in outcomes if item.metric == "setup:entry_instability")


def test_good_brake_bias_outcome_folds_gain() -> None:
    rules, thresholds = _config()
    db, _ = _case()

    outcome = _outcome(db, 801, thresholds)

    detail = json.loads(str(outcome["detail"]))
    assert outcome["call_id"] == "801:setup:debrief:entry_instability:brake_bias"
    assert outcome["rule_id"] == "setup.entry_instability"
    assert outcome["lap"] == 6
    assert outcome["metric"] == "setup:entry_instability"
    assert outcome["predicted"] == pytest.approx(-0.03)
    assert outcome["actual"] == pytest.approx(-1.0)
    assert outcome["error"] == pytest.approx(-0.97)
    assert outcome["label"] == "good"
    assert detail["applied_sign"] == 1
    assert detail["before_session"] == 801 and detail["after_session"] == 801
    gain = db.get_param(7, 18, "setup_gain:entry_instability:brake_bias:+")
    assert gain is not None
    assert gain.value == pytest.approx(1.0)
    assert gain.weight == pytest.approx(1.0)
    assert db.setup_rec_by_id(str(outcome["call_id"]))["folded"] == 1
    assert rules.params["brake_bias"].step == 1


@pytest.mark.parametrize(
    ("case", "fields", "after_laps", "slope", "label", "status"),
    [
        (
            "confounded",
            _fields(brake_bias=57.0, rear_anti_roll_bar=6.0),
            6,
            0.0,
            "n/a",
            "confounded",
        ),
        ("ignored", _fields(brake_bias=56.0, rear_wing=9.0), 6, 0.0, "ignored", "unchanged"),
        ("opposite", _fields(brake_bias=55.0), 6, 0.0, "ignored", "opposite"),
        ("censored", _fields(brake_bias=57.0), 4, 0.0, "censored", "censored"),
        ("wrong", _fields(brake_bias=57.0), 6, 10.0, "wrong", ""),
    ],
)
def test_setup_outcome_categories(
    case: str,
    fields: dict[str, float],
    after_laps: int,
    slope: float,
    label: str,
    status: str,
) -> None:
    _, thresholds = _config()
    uid = 810 + ("confounded", "ignored", "opposite", "censored", "wrong").index(case)
    db, _ = _case(
        uid=uid,
        after_fields=fields,
        after_laps=after_laps,
        after_slope_ms=slope,
    )

    outcome = _outcome(db, uid, thresholds)
    detail = json.loads(str(outcome["detail"]))
    assert outcome["label"] == label
    assert detail["status"] == status
    if case == "confounded":
        assert db.get_param(7, 18, "setup_gain:entry_instability:brake_bias:+") is None
    elif case == "opposite":
        gain = db.get_param(7, 18, "setup_gain:entry_instability:brake_bias:-")
        assert gain is not None and gain.value == pytest.approx(1.0)
    elif case in {"ignored", "censored"}:
        assert db.get_param(7, 18, "setup_gain:entry_instability:brake_bias:+") is None
    elif case == "wrong":
        assert detail["J_rel"] >= thresholds["setup_grade_tol"]


def test_cross_session_after_run_is_graded_in_follow_up_session() -> None:
    _, thresholds = _config()
    db = Database(":memory:")
    first, second = 830, 831
    before_fields, after_fields = _fields(), _fields(brake_bias=57.0)
    db.upsert_session(first, track_id=7, session_type=1, started_at=1.0, parc_ferme=1)
    before_state = db.setup_state_id("cross-before", before_fields)
    _insert_run(
        db,
        first,
        state_id=before_state,
        start_lap=1,
        start_age=1,
        count=6,
        lockups_rear=1,
        slip=2.0,
        lap_slope_ms=0.0,
    )
    db.insert_setup_rec(
        _recommendation(first, before_state),
        track_id=7,
        compound=18,
        lap=6,
    )
    db.upsert_session(second, track_id=7, session_type=1, started_at=2.0, parc_ferme=1)
    after_state = db.setup_state_id("cross-after", after_fields)
    _insert_run(
        db,
        second,
        state_id=after_state,
        start_lap=1,
        start_age=7,
        count=6,
        lockups_rear=0,
        slip=4.0,
        lap_slope_ms=0.0,
    )

    outcomes = grade_and_store(db, second, thresholds)

    outcome = next(item for item in outcomes if item.metric == "setup:entry_instability")
    detail = json.loads(outcome.detail)
    assert outcome.label == "good"
    assert detail["before_session"] == first
    assert detail["after_session"] == second


def test_grade_and_store_folds_each_recommendation_and_baseline_once() -> None:
    _, thresholds = _config()
    db, before_state = _case(uid=840)
    rec_id = "840:setup:debrief:entry_instability:brake_bias"

    grade_and_store(db, 840, thresholds)
    gain_first = db.get_param(7, 18, "setup_gain:entry_instability:brake_bias:+")
    base_first = db.get_param(7, 18, "setup_base:slip_balance_deg")
    db.insert_setup_rec(
        _recommendation(840, before_state),
        track_id=7,
        compound=18,
        lap=6,
    )
    grade_and_store(db, 840, thresholds)
    gain_second = db.get_param(7, 18, "setup_gain:entry_instability:brake_bias:+")
    base_second = db.get_param(7, 18, "setup_base:slip_balance_deg")

    assert gain_first is not None and gain_second is not None
    assert gain_second.weight == gain_first.weight == pytest.approx(1.0)
    assert gain_second.value == gain_first.value
    assert base_first is not None and base_second is not None
    assert base_second.weight == base_first.weight == pytest.approx(2.0)
    assert base_second.value == base_first.value == pytest.approx(3.0)
    assert db.setup_rec_by_id(rec_id)["folded"] == 1
    assert db.session_row(840)["setup_folded"] == 1

    signals = session_signals(db, 840, thresholds)
    assert signals is not None
    assert signals.slip_raw == pytest.approx(4.0)
    assert signals.slip_balance == pytest.approx(1.0)


def test_learned_direction_flips_race_recommendation() -> None:
    rules, thresholds = _config()
    signals = RunSignals(
        session_uid=850,
        track_id=7,
        session_type=15,
        compound=18,
        setup_state_id=1,
        run_laps=6,
        event_laps=6,
        traction_exits_per10=30.0,
        lockups_rear_per10=0.0,
        lockups_front_per10=0.0,
        snaps_entry_per10=0.0,
        snaps_exit_per10=0.0,
        snap_phase="",
        slip_balance=None,
        wear_axle_ratio=1.0,
        z_front=None,
        z_rear=None,
    )
    setup = _fields()
    learned = {("traction_limited", "on_throttle", 1): (0.5, 3.0)}

    recommendations = evaluate(
        signals,
        setup,
        mode="race",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
        learned=learned,
    )

    rec = next(item for item in recommendations if item.param == "on_throttle")
    assert rec.delta == 10.0
    assert rec.conf == "high"
    assert rec.evidence["learned_gain"] == pytest.approx(0.5)
    assert rec.evidence["learned_weight"] == pytest.approx(3.0)
    assert rec.evidence["flipped"] is True
    unlearned = evaluate(
        replace(signals, session_type=1),
        setup,
        mode="debrief",
        parc_ferme=1,
        rules=rules,
        thresholds=thresholds,
    )
    assert any(item.param == "on_throttle" and item.tier == "experiment" for item in unlearned)

    db = Database(":memory:")
    db.set_param(7, 18, "setup_gain:traction_limited:on_throttle:+", 0.5, 3.0)
    assert learned_gains(db, 7, 18) == learned


def test_digest_counts_setup_outcomes_and_tune_lists_setup_rule() -> None:
    _, thresholds = _config()
    db, before_state = _case(uid=860)
    experiment = _recommendation(
        860,
        before_state,
        rule_id="traction_limited",
        param="on_throttle",
        from_value=55.0,
        delta=-5.0,
        tier="experiment",
    )
    db.insert_setup_rec(experiment, track_id=7, compound=18, lap=6)

    digest = build_digest(db, 860, thresholds)

    assert digest["setup"] == {
        "recs": 2,
        "graded": 1,
        "good": 1,
        "wrong": 0,
        "ignored": 1,
        "censored": 0,
        "na": 0,
        "open_experiments": 1,
    }
    tuned = {item.rule_id: item for item in tune_from_db(db, thresholds)}
    assert tuned["setup.entry_instability"].auto == 1
