from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from pitwall.cli import main
from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.debrief import render_debrief
from pitwall.engine import build_engine
from pitwall.setup.evaluate import Recommendation, evaluate
from pitwall.setup.rules import parse_setup_rules
from pitwall.setup.signals import RunSignals, session_signals
from pitwall.state.lap import LapSummary
from pitwall.state.session import SetupChange, Snapshot
from pitwall.store.db import Database
from pitwall.strategy.pitwindow import NO_PLAN


def _seed_session(
    db: Database,
    uid: int,
    *,
    session_type: int,
    lockups_rear: int = 0,
    traction_exits: int = 0,
) -> tuple[int, dict[str, float]]:
    fields = {
        "brake_bias": 56.0,
        "on_throttle": 55.0,
        "front_wing": 10.0,
        "rear_anti_roll_bar": 5.0,
        "rear_suspension_height": 30.0,
    }
    state_id = db.setup_state_id(f"sha1:{uid}", fields)
    db.upsert_session(uid, track_id=7, session_type=session_type, parc_ferme=1)
    for lap_num in range(1, 4):
        db.insert_lap(
            uid,
            0,
            LapSummary(
                lap_num=lap_num,
                lap_time_ms=90_000,
                sector1_ms=30_000,
                sector2_ms=30_000,
                compound=17,
                tyre_age_laps=lap_num,
                fuel_remaining_laps_at_end=2.0,
                valid=True,
                lockups_rear=lockups_rear,
                traction_exits=traction_exits,
            ),
            setup_state_id=state_id,
        )
    return state_id, fields


def _seed_longest_runs(
    db: Database,
    uid: int,
    *,
    second_run_laps: int = 2,
) -> tuple[int, int, dict[str, float]]:
    fields_a = {
        "brake_bias": 56.0,
        "on_throttle": 55.0,
        "front_wing": 10.0,
        "rear_anti_roll_bar": 5.0,
        "rear_suspension_height": 30.0,
    }
    fields_b = fields_a | {"brake_bias": 60.0}
    state_a = db.setup_state_id(f"sha1:{uid}:state-a", fields_a)
    state_b = db.setup_state_id(f"sha1:{uid}:state-b", fields_b)
    db.upsert_session(uid, track_id=7, session_type=15, parc_ferme=1)
    lap_num = 0
    for state_id, count, lockups_rear in ((state_a, 9, 1), (state_b, second_run_laps, 0)):
        for _ in range(count):
            lap_num += 1
            db.insert_lap(
                uid,
                0,
                LapSummary(
                    lap_num=lap_num,
                    lap_time_ms=90_000,
                    sector1_ms=30_000,
                    sector2_ms=30_000,
                    compound=18,
                    tyre_age_laps=lap_num,
                    fuel_remaining_laps_at_end=2.0,
                    valid=True,
                    lockups_rear=lockups_rear,
                ),
                setup_state_id=state_id,
            )
    return state_a, state_b, fields_b


def _recommendation(
    *,
    mode: str = "garage",
    param: str = "brake_bias",
    rule_id: str = "entry_instability",
    from_value: float = 56.0,
    to_value: float = 57.0,
    tier: str = "primary",
    suppressed: tuple[dict[str, str], ...] = (),
) -> Recommendation:
    return Recommendation(
        rec_id="101:setup:garage:entry_instability:brake_bias",
        rule_id=rule_id,
        mode=mode,
        tier=tier,
        param=param,
        from_value=from_value,
        delta=to_value - from_value,
        to_value=to_value,
        conf="high",
        expect="more front braking",
        tradeoff="fronts lock earlier",
        evidence={"event_laps": 3},
        setup_state_id=1,
        session_type=1,
        parc_ferme=1,
        suppressed=suppressed,
    )


def test_race_setup_call_uses_live_bias_and_respects_cooldown() -> None:
    db = Database(":memory:")
    uid = 101
    _, fields = _seed_session(db, uid, session_type=15, lockups_rear=2)
    engine = build_engine(
        clock=VirtualClock(),
        sinks=[],
        db=db,
        decision_log_fp=io.StringIO(),
    )
    engine.state.session_uid = uid
    engine.state.session_type = 15
    engine.state.track_id = 7
    engine.state.lap_num = 3
    engine.state.setup = fields
    engine.state.parc_ferme_rules = 1
    engine.state.front_brake_bias = 56
    engine.state.setup_on_throttle_diff = 55

    engine._evaluate_live_setup(uid)
    assert engine._setup_call_param == "brake_bias"
    assert engine._setup_call_from == 56.0
    assert all(
        rec.param in {"brake_bias", "on_throttle"}
        for rec in engine.state.setup_advice
        if rec.mode == "race"
    )

    engine.state.lap_num = 4
    engine._evaluate_live_setup(uid)
    assert engine._setup_call_param == ""
    engine.state.lap_num = 7
    engine._evaluate_live_setup(uid)
    assert engine._setup_call_param == ""
    engine.state.lap_num = 8
    engine._evaluate_live_setup(uid)
    assert engine._setup_call_param == "brake_bias"

    engine.state.front_brake_bias = engine._setup_call_to
    engine.state.lap_num = 9
    engine._evaluate_live_setup(uid)
    assert engine._setup_call_param == ""


def test_race_on_throttle_call_uses_race_step() -> None:
    db = Database(":memory:")
    uid = 102
    _, fields = _seed_session(db, uid, session_type=15, traction_exits=10)
    engine = build_engine(
        clock=VirtualClock(),
        sinks=[],
        db=db,
        decision_log_fp=io.StringIO(),
    )
    engine.state.session_uid = uid
    engine.state.session_type = 15
    engine.state.track_id = 7
    engine.state.lap_num = 3
    engine.state.setup = fields
    engine.state.parc_ferme_rules = 1
    engine.state.front_brake_bias = 56
    engine.state.setup_on_throttle_diff = 55
    engine._setup_rules = replace(engine._setup_rules, confidence_floor="low")

    engine._evaluate_live_setup(uid)
    assert engine._setup_call_param == "on_throttle"
    assert engine._setup_call_from == 55.0
    assert engine._setup_call_to == 45.0


def test_race_stop_wing_requires_plan_and_clears_at_target() -> None:
    db = Database(":memory:")
    engine = build_engine(
        clock=VirtualClock(),
        sinks=[],
        db=db,
        decision_log_fp=io.StringIO(),
    )
    rec = _recommendation(
        mode="race_stop",
        param="front_wing",
        rule_id="understeer_balance",
        from_value=10.0,
        to_value=11.0,
    )
    engine.state.setup_advice = (rec,)
    engine.state.next_front_wing_value = 10.0
    engine.pit_plan = replace(NO_PLAN, plan="box_now")
    engine._update_setup_stop_wing()
    assert (engine._setup_stop_wing_from, engine._setup_stop_wing_to) == (10.0, 11.0)

    engine.state.next_front_wing_value = 11.0
    engine._update_setup_stop_wing()
    assert engine._setup_stop_wing_to == 0.0

    engine.state.next_front_wing_value = 10.0
    engine.pit_plan = NO_PLAN
    engine._update_setup_stop_wing()
    assert engine._setup_stop_wing_to == 0.0

    for plan in ("no_stop", "stay"):
        engine.pit_plan = replace(NO_PLAN, plan=plan)
        engine._update_setup_stop_wing()
        assert engine._setup_stop_wing_to == 0.0, plan
    engine.pit_plan = replace(NO_PLAN, plan="box_in_n")
    engine._update_setup_stop_wing()
    assert engine._setup_stop_wing_to == 11.0


def test_session_signals_keep_events_inside_the_selected_run() -> None:
    settings = ConfigStore().current()
    db = Database(":memory:")
    uid = 110
    fields = {"brake_bias": 56.0, "front_wing": 10.0}
    state_id = db.setup_state_id(f"sha1:{uid}", fields)
    db.upsert_session(uid, track_id=7, session_type=1, parc_ferme=1)
    lap_num = 0
    for compound, lockups_rear in ((18, 2), (16, 0)):
        for age in range(1, 4):
            lap_num += 1
            db.insert_lap(
                uid,
                0,
                LapSummary(
                    lap_num=lap_num,
                    lap_time_ms=90_000,
                    sector1_ms=30_000,
                    sector2_ms=30_000,
                    compound=compound,
                    tyre_age_laps=age,
                    fuel_remaining_laps_at_end=2.0,
                    valid=True,
                    lockups_rear=lockups_rear,
                ),
                setup_state_id=state_id,
            )

    latest = session_signals(db, uid, settings.thresholds)
    assert latest is not None
    assert latest.setup_state_id == state_id
    assert latest.compound == 16
    assert latest.run_laps == latest.event_laps == 3
    assert latest.run_end_lap == 6
    assert latest.lockups_rear == 0
    assert latest.lockups_rear_per10 == 0.0


def _engine_with_setup_change(uid: int, session_type: int, lockups_rear: int) -> Any:
    db = Database(":memory:")
    _, fields = _seed_session(db, uid, session_type=session_type, lockups_rear=lockups_rear)
    engine = build_engine(
        clock=VirtualClock(),
        sinks=[],
        db=db,
        decision_log_fp=io.StringIO(),
    )
    engine.state.session_uid = uid
    engine.state.session_type = session_type
    engine.state.track_id = 7
    engine.state.lap_num = 3
    engine.state.parc_ferme_rules = 1
    engine.state.setup = fields | {"brake_bias": 57.0}
    engine.state.setup_advice = (_recommendation(),)
    engine.state.setup_changes.append(
        SetupChange(
            session_time=300.0,
            lap_num=3,
            from_hash=f"sha1:{uid}",
            to_hash=f"sha1:{uid}:changed",
            fields=engine.state.setup,
        )
    )
    return engine


def test_garage_setup_change_drops_stale_advice_without_a_new_lap() -> None:
    engine = _engine_with_setup_change(111, session_type=1, lockups_rear=2)
    engine._write_laps()
    assert engine.state.setup_advice == ()
    assert len(engine.db.setup_changes_for_session(111)) == 1


def test_race_setup_change_refreshes_advice_without_a_new_lap() -> None:
    engine = _engine_with_setup_change(112, session_type=15, lockups_rear=2)
    engine._write_laps()
    assert engine.state.setup_advice
    assert all(rec.mode in ("race", "race_stop") for rec in engine.state.setup_advice)


def test_new_session_resets_parc_ferme_written() -> None:
    engine = build_engine(
        clock=VirtualClock(),
        sinks=[],
        db=Database(":memory:"),
        decision_log_fp=io.StringIO(),
    )
    engine._parc_ferme_written = 1
    engine._on_new_session(113)
    assert engine._parc_ferme_written is None


def test_delete_setup_changes_after_rewind_time() -> None:
    db = Database(":memory:")
    uid = 114
    state_a = db.setup_state_id(f"sha1:{uid}:a", {"brake_bias": 56.0})
    state_b = db.setup_state_id(f"sha1:{uid}:b", {"brake_bias": 55.0})
    db.insert_setup_change(uid, 1, 10.0, None, state_a)
    db.insert_setup_change(uid, 3, 200.0, state_a, state_b)
    db.delete_setup_changes_after(uid, 150.0)
    assert [row["to_state"] for row in db.setup_changes_for_session(uid)] == [state_a]


def test_pit_board_payload_includes_advice_locked_fields_and_checklist() -> None:
    from pitwall.server.app import pit_board_payload

    settings = ConfigStore().current()
    rec = _recommendation(suppressed=({"param": "rear_anti_roll_bar", "reason": "locked"},))
    snapshot = Snapshot(
        now=1.0,
        session_kind="practice",
        session_type=1,
        phase="garage",
        setup={"brake_bias": 56.0, "rear_anti_roll_bar": 5.0, "rear_wing": 8.0},
        setup_advice=(rec,),
        parc_ferme=1,
        weekend_structure=(1, 5),
    )
    board = pit_board_payload(snapshot, settings.thresholds, settings.setup_rules)
    assert board is not None
    assert board["setup_advice"] == [
        {
            "param": "brake_bias",
            "fields": ["brake_bias"],
            "from": 56.0,
            "to": 57.0,
            "delta": 1.0,
            "conf": "high",
            "tier": "primary",
            "reason": "rears locking on entry",
        }
    ]
    assert board["setup_locked"] == [
        {
            "param": "rear_anti_roll_bar",
            "fields": ["rear_anti_roll_bar"],
            "reason": "locked",
        }
    ]
    checklist = board["setup_lock_checklist"]
    assert checklist is not None
    assert {"field": "rear_wing", "value": 8.0} in checklist
    quali_board = pit_board_payload(
        replace(snapshot, session_type=7, weekend_structure=()),
        settings.thresholds,
        settings.setup_rules,
    )
    assert quali_board is not None
    assert quali_board["setup_locked"] == board["setup_locked"]

    assert (
        pit_board_payload(
            replace(snapshot, weekend_structure=(1, 15)),
            settings.thresholds,
            settings.setup_rules,
        )["setup_lock_checklist"]
        is None
    )
    assert (
        pit_board_payload(
            replace(snapshot, parc_ferme=0),
            settings.thresholds,
            settings.setup_rules,
        )["setup_lock_checklist"]
        is None
    )
    assert (
        pit_board_payload(
            replace(snapshot, session_type=5),
            settings.thresholds,
            settings.setup_rules,
        )["setup_lock_checklist"]
        is None
    )


def test_debrief_renders_stored_setup_recommendations_and_empty_state() -> None:
    settings = ConfigStore().current()
    rules = parse_setup_rules(settings.setup_rules)
    db = Database(":memory:")
    uid = 103
    state_id, fields = _seed_session(db, uid, session_type=1, lockups_rear=2)
    signals = session_signals(db, uid, settings.thresholds)
    assert signals is not None
    rec = next(
        item
        for item in evaluate(
            signals,
            fields,
            mode="debrief",
            parc_ferme=1,
            rules=rules,
            thresholds=settings.thresholds,
        )
        if item.param == "brake_bias"
    )
    rec = replace(rec, suppressed=({"param": "rear_anti_roll_bar", "reason": "locked"},))
    db.insert_setup_rec(rec, track_id=7, compound=17, lap=3)

    report = render_debrief(db, uid, settings)
    assert "<h3>SETUP</h3>" in report
    assert "<td>brake_bias</td>" in report
    assert "56 → 57" in report
    assert "Would suggest rear_anti_roll_bar (entry_instability), locked by parc fermé." in report
    assert "Source: setup_recs, laps, setup_states." in report

    empty_db = Database(":memory:")
    empty_uid = 104
    _seed_session(empty_db, empty_uid, session_type=1)
    empty_report = render_debrief(empty_db, empty_uid, settings)
    assert "No setup change suggested: no symptom passed its threshold." in empty_report
    assert "Run event laps: 3." in empty_report


def test_session_signals_choose_longest_run_and_later_tie() -> None:
    settings = ConfigStore().current()
    db = Database(":memory:")
    uid = 106
    state_a, state_b, _ = _seed_longest_runs(db, uid)

    latest = session_signals(db, uid, settings.thresholds)
    longest = session_signals(db, uid, settings.thresholds, run_choice="longest")

    assert latest is not None and longest is not None
    assert latest.setup_state_id == state_b
    assert latest.run_laps == 2
    assert latest.lockups_rear is None
    assert latest.lockups_rear_per10 is None
    assert longest.setup_state_id == state_a
    assert longest.run_laps == longest.event_laps == 9
    assert longest.lockups_rear == 9
    assert longest.lockups_rear_per10 == 10.0

    tie_db = Database(":memory:")
    tie_uid = 107
    _, tie_state_b, _ = _seed_longest_runs(tie_db, tie_uid, second_run_laps=9)
    tied = session_signals(tie_db, tie_uid, settings.thresholds, run_choice="longest")
    assert tied is not None
    assert tied.setup_state_id == tie_state_b


def test_post_session_advice_uses_longest_run_setup_state(tmp_path: Path, capsys: Any) -> None:
    settings = ConfigStore().current()
    db_path = tmp_path / "longest.sqlite"
    db = Database(db_path)
    uid = 108
    state_a, _, fields_b = _seed_longest_runs(db, uid)

    report = render_debrief(db, uid, settings)
    assert "56 → 57" in report
    assert "60 → 61" not in report

    assert main(["setup", str(uid), "--mode", "debrief", "--json", "--db", str(db_path)]) == 0
    recommendations = json.loads(capsys.readouterr().out)
    brake_bias = next(rec for rec in recommendations if rec["param"] == "brake_bias")
    assert brake_bias["from_value"] == 56.0
    assert brake_bias["setup_state_id"] == state_a

    assert main(["setup", str(uid), "--mode", "garage", "--json", "--db", str(db_path)]) == 0
    assert json.loads(capsys.readouterr().out) == []

    engine = build_engine(
        clock=VirtualClock(),
        sinks=[],
        db=db,
        decision_log_fp=io.StringIO(),
    )
    engine.state.session_uid = uid
    engine.state.session_type = 15
    engine.state.track_id = 7
    engine.state.lap_num = 11
    engine.state.setup = fields_b
    engine.state.parc_ferme_rules = 1
    engine._store_debrief_setup(uid)
    stored = next(
        rec
        for rec in db.setup_recs_for_session(uid)
        if rec["mode"] == "debrief" and rec["param"] == "brake_bias"
    )
    assert stored["from_value"] == 56.0
    assert stored["setup_state_id"] == state_a
    assert stored["lap"] == 9

    assert main(["setup", str(uid), "--mode", "debrief", "--store", "--db", str(db_path)]) == 0
    cli_stored = [
        rec
        for rec in db.setup_recs_for_session(uid)
        if rec["mode"] == "debrief" and rec["param"] == "brake_bias"
    ]
    assert cli_stored and all(rec["lap"] == 9 for rec in cli_stored)


def test_session_end_stores_debrief_setup_recommendations() -> None:
    db = Database(":memory:")
    uid = 105
    _, fields = _seed_session(db, uid, session_type=15, lockups_rear=2)
    engine = build_engine(
        clock=VirtualClock(),
        sinks=[],
        db=db,
        decision_log_fp=io.StringIO(),
    )
    engine.state.session_uid = uid
    engine.state.session_type = 15
    engine.state.track_id = 7
    engine.state.lap_num = 3
    engine.state.setup = fields
    engine.state.parc_ferme_rules = 1
    engine.state.session_ended = True

    engine.tick(1.0)
    stored = db.setup_recs_for_session(uid)
    assert any(item["mode"] == "debrief" for item in stored)


def test_race_rule_speaks_current_bias_and_shared_lockup_copy_is_garage_safe() -> None:
    import yaml

    from pitwall.rules.engine import RuleEngine

    settings = ConfigStore().current()
    engine = RuleEngine(
        list(settings.rules),
        thresholds=settings.thresholds,
        mode=settings.resolved_mindset(),
        staleness_s=settings.engine.staleness_s,
    )
    result = engine.evaluate(
        Snapshot(
            now=1.0,
            session_kind="race",
            session_type=15,
            lap_num=5,
            setup_call_param="brake_bias",
            setup_call_from=56.0,
            setup_call_to=57.0,
            setup_call_reason="rears locking on entry",
            _ages={"lap_data": 0.1, "car_status": 0.1},
        )
    )
    call = next(item for item in result.candidates if item.rule.id == "setup_bias")
    assert call.text == "Bias is on 56. Try 57, rears locking on entry."

    shared = yaml.safe_load(
        (Path(__file__).parents[1] / "src/pitwall/config/defaults/rules/shared.yaml").read_text()
    )
    lockup = next(rule for rule in shared["rules"] if rule["id"] == "lockup_rear")
    escalation_phrases = [phrase for tier in lockup["escalate"] for phrase in tier["say"]]
    assert all("off-throttle" not in phrase.lower() for phrase in escalation_phrases)


def test_race_stop_evaluator_result_contains_only_front_wing() -> None:
    settings = ConfigStore().current()
    signals = RunSignals(
        session_uid=106,
        track_id=7,
        session_type=15,
        compound=17,
        setup_state_id=1,
        run_laps=6,
        event_laps=6,
        traction_exits_per10=0.0,
        lockups_rear_per10=0.0,
        lockups_front_per10=0.0,
        snaps_entry_per10=0.0,
        snaps_exit_per10=0.0,
        snap_phase="",
        slip_balance=1.0,
        wear_axle_ratio=1.0,
        z_front=0.0,
        z_rear=0.0,
    )
    setup: dict[str, Any] = {
        "front_wing": 10.0,
        "rear_anti_roll_bar": 5.0,
        "brake_bias": 56.0,
        "on_throttle": 55.0,
    }
    recs = evaluate(
        signals,
        setup,
        mode="race_stop",
        parc_ferme=1,
        rules=parse_setup_rules(settings.setup_rules),
        thresholds=settings.thresholds,
    )
    assert recs
    assert {rec.param for rec in recs} == {"front_wing"}
