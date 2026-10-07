from __future__ import annotations

import pytest

from pitwall.audio.dispatcher import Dispatcher
from pitwall.clock import VirtualClock
from pitwall.config.models import PolicySettings, RuleDefModel
from pitwall.protocol.layouts import Corners
from pitwall.rules.engine import RuleEngine
from pitwall.rules.expr import ExprError, Predicate
from pitwall.state.session import Snapshot


def _corners(v: float) -> Corners:
    return Corners(v, v, v, v)


def _snap(**kw: object) -> Snapshot:
    base = {
        "now": 0.0,
        "session_kind": "race",
        "session_type": 15,
        "lap_num": 5,
        "phase": "out_lap",
        "_ages": {"car_telemetry": 0.1, "lap_data": 0.1},
    }
    base.update(kw)
    return Snapshot(**base)  # type: ignore[arg-type]


def _rule(**kw: object) -> RuleDefModel:
    base = {
        "id": "r1",
        "sessions": ["race"],
        "priority": 2,
        "when": "phase == 'out_lap' and tyre_inner_ema_fast.FL < th.t",
        "say": "FL {tyre_inner_ema_fast.FL:.0f}",
    }
    base.update(kw)
    return RuleDefModel.model_validate(base)


def _engine(
    rules: list[RuleDefModel], th: dict | None = None, mode: dict | None = None
) -> RuleEngine:
    return RuleEngine(
        rules,
        thresholds=th or {"t": 80.0},
        mode=mode or {"thermal_warn_offset_c": 0},
        staleness_s={"car_telemetry": 0.5, "lap_data": 0.5},
    )


def test_expr_whitelist() -> None:
    Predicate("a > 1 and b or not c")  # valid parses
    for bad in [
        "__import__('os')",
        "open('x')",
        "x = 1",
        "f'{x}'",
        "lambda: 1",
        "[x for x in y]",
    ]:
        with pytest.raises(ExprError):
            Predicate(bad)


def test_fires_on_condition() -> None:
    eng = _engine([_rule()])
    res = eng.evaluate(_snap(tyre_inner_ema_fast=_corners(60.0)))
    assert len(res.candidates) == 1
    assert res.candidates[0].text == "FL 60"
    assert res.candidates[0].inputs["phase"] == "out_lap"
    assert "tyre_inner_ema_fast" in res.candidates[0].inputs


def test_no_fire_when_warm_or_wrong_phase() -> None:
    eng = _engine([_rule()])
    assert not eng.evaluate(_snap(tyre_inner_ema_fast=_corners(90.0))).candidates
    assert not eng.evaluate(_snap(phase="flying", tyre_inner_ema_fast=_corners(60.0))).candidates


def test_hysteresis_no_refire() -> None:
    eng = _engine([_rule(clear_when="tyre_inner_ema_fast.FL > th.t + 5")])
    cold = _snap(tyre_inner_ema_fast=_corners(79.0))
    res = eng.evaluate(cold)
    assert len(res.candidates) == 1
    # oscillating between 78 and 82 while still below clear threshold (85):
    for v in (78.0, 82.0, 78.0):
        assert not eng.evaluate(_snap(tyre_inner_ema_fast=_corners(v))).candidates
    # cross clear threshold -> re-arm -> fires again on next cold tick
    eng.evaluate(_snap(tyre_inner_ema_fast=_corners(90.0)))
    assert eng.evaluate(cold).candidates


def test_requires_stale_skips() -> None:
    eng = _engine([_rule(requires=["car_telemetry", "lap_data"])])
    res = eng.evaluate(
        _snap(tyre_inner_ema_fast=_corners(60.0), _ages={"car_telemetry": 9.0, "lap_data": 0.1})
    )
    assert not res.candidates
    assert res.suppressed[0].reason == "stale"


def test_sessions_filter() -> None:
    eng = _engine([_rule(sessions=["qualifying"])])
    assert not eng.evaluate(_snap(tyre_inner_ema_fast=_corners(60.0))).candidates


def test_mode_and_still_true() -> None:
    eng = _engine(
        [
            _rule(
                when="tyre_inner_ema_fast.FL < th.t + mode.thermal_warn_offset_c",
                still_true="tyre_inner_ema_fast.FL < th.t + 8",
            )
        ],
        mode={"thermal_warn_offset_c": 3},
    )
    res = eng.evaluate(_snap(tyre_inner_ema_fast=_corners(82.0)))  # 80+3 catches it
    assert len(res.candidates) == 1
    cand = res.candidates[0]
    assert cand.still_true is not None
    assert cand.still_true(_snap(tyre_inner_ema_fast=_corners(87.0)))
    assert not cand.still_true(_snap(tyre_inner_ema_fast=_corners(89.0)))


def test_min_lap() -> None:
    eng = _engine([_rule(min_lap=3)])
    res = eng.evaluate(_snap(lap_num=1, tyre_inner_ema_fast=_corners(60.0)))
    assert res.suppressed[0].reason == "min_lap"


def _default_rule_engine() -> RuleEngine:
    from pitwall.config.loader import ConfigStore

    store = ConfigStore()
    s = store.current()
    return RuleEngine(
        list(s.rules),
        thresholds=s.thresholds,
        mode=store.current().resolved_mindset(),
        staleness_s=s.engine.staleness_s,
    )


@pytest.mark.parametrize("laps_left_offset", [-1, 0, 1])
def test_rival_ahead_pitted_rule_requires_time_to_stop(laps_left_offset: int) -> None:
    from pitwall.config.loader import ConfigStore

    min_left = int(ConfigStore().current().thresholds["pit_min_laps_left"])
    result = _default_rule_engine().evaluate(
        _snap(
            phase="racing",
            laps_remaining=min_left + laps_left_offset,
            rival_ahead_in_pit_lane=True,
            rival_ahead_pitted=True,
            rival_ahead_name="NORRIS",
            gap_ahead_s=1.0,
            pit_plan="no_stop",
        )
    )
    ids = {candidate.rule.defn.id for candidate in result.candidates}
    assert ("rival_ahead_pitted" in ids) == (laps_left_offset > 0)


def test_front_wing_damage_rule_fires() -> None:
    from pitwall.state.session import Damage

    engine = _default_rule_engine()
    snap = _snap(
        phase="on_track",
        damage=Damage(front_left_wing=30),
        _ages={"car_damage": 0.1},
    )
    result = engine.evaluate(snap)
    ids = [c.rule.defn.id for c in result.candidates]
    assert "front_wing_damage" in ids

    # below threshold: nothing fires
    engine2 = _default_rule_engine()
    snap2 = _snap(
        phase="on_track",
        damage=Damage(front_left_wing=5),
        _ages={"car_damage": 0.1},
    )
    result2 = engine2.evaluate(snap2)
    assert "front_wing_damage" not in [c.rule.defn.id for c in result2.candidates]


def test_quali_safe_margin_and_pressure_rules() -> None:
    from pitwall.protocol.layouts import Corners
    from pitwall.state.pressure import PressureCall

    ages = {"car_telemetry": 0.1, "lap_data": 0.1, "session_history": 0.1}
    base = {"session_kind": "qualifying", "session_type": 5, "phase": "in_lap", "_ages": ages}
    ids = [
        c.rule.defn.id
        for c in _default_rule_engine()
        .evaluate(_snap(**base, quali_margin_ms=1_200, quali_margin_s=1.2, quali_margin_kind="cut"))
        .candidates
    ]
    assert "quali_safe_cut" in ids and "quali_safe_pole" not in ids
    ids = [
        c.rule.defn.id
        for c in _default_rule_engine()
        .evaluate(_snap(**base, quali_margin_ms=800, quali_margin_kind="pole"))
        .candidates
    ]
    assert "quali_safe_pole" not in ids

    call = PressureCall("fr", "front right", 110.0, "medium", -0.4, 23.8)
    result = _default_rule_engine().evaluate(
        _snap(
            **base,
            run_flying_s=90.0,
            run_tyre_inner_avg=Corners(95.0, 95.0, 95.0, 110.0),
            pressure_advice=(call,),
            pressure_advice_text="front right down 0.4",
        )
    )
    texts = {c.rule.defn.id: c.text for c in result.candidates}
    assert texts["tyre_pressure_advice"] == "Pressures: front right down 0.4"
    assert "tyre_pressure_ok" not in texts
    # Not enough flying time on the run: no advice either way
    result = _default_rule_engine().evaluate(
        _snap(**base, run_flying_s=10.0, pressure_advice_text="")
    )
    assert not {"tyre_pressure_advice", "tyre_pressure_ok"} & {
        c.rule.defn.id for c in result.candidates
    }


def test_fastest_lap_level_copy() -> None:
    from pitwall.config.loader import ConfigStore

    th = ConfigStore().current().thresholds
    base = {
        "phase": "racing",
        "lap_num": 10,
        "total_laps": 11,
        "laps_remaining": 2,
        "fastest_lap_mine": False,
        "fastest_lap_age_s": float(th["fastest_lap_settle_s"]),
        "fastest_lap_name": "RUSSELL",
        "fastest_lap_spoken": "1 minute 51.3 seconds",
    }

    def text(gap: float) -> str:
        res = _default_rule_engine().evaluate(_snap(**base, fastest_lap_gap_s=gap))
        return next(c.text for c in res.candidates if c.rule.defn.id == "fastest_lap_taken")

    assert "level" in text(0.001) and "0.0" not in text(0.001)
    assert "0.3" in text(0.3)


def test_last_lap_rules_share_one_cooldown() -> None:
    from pitwall.config.loader import ConfigStore

    defs = {r.id: r for r in ConfigStore().current().rules}
    ids = ("last_lap", "last_lap_defend", "last_lap_attack")
    assert {defs[i].cooldown_group for i in ids} == {"last_lap"}
    assert all(defs[i].cooldown_s >= 60 for i in ids)


def test_penalty_covered_then_penalty_cost_both_speak() -> None:
    engine = _default_rule_engine()
    dispatcher = Dispatcher(
        PolicySettings(min_gap_s=0.0, p3_straight_only=False), VirtualClock(), sinks=[]
    )
    covered = _snap(
        now=0.0,
        phase="racing",
        laps_remaining=2,
        position=2,
        penalty_s=5,
        penalty_position=2,
    )
    result = engine.evaluate(covered)
    dispatcher.submit(result.candidates, covered)
    first = dispatcher.drain(0.0)
    assert [call.rule_id for call in first] == ["penalty_covered"]

    penalty = _snap(
        now=30.0,
        phase="racing",
        laps_remaining=2,
        position=2,
        penalty_s=5,
        penalty_position=4,
        penalty_margin_s=-1.2,
        penalty_threat_name="NORRIS",
    )
    result = engine.evaluate(penalty)
    dispatcher.submit(result.candidates, penalty)
    second = dispatcher.drain(30.0)
    assert [call.rule_id for call in second] == ["penalty_cost"]


def test_last_lap_uses_penalty_severity_copy() -> None:
    snap = _snap(
        phase="racing",
        total_laps=10,
        laps_remaining=1,
        sector=0,
        position=4,
        penalty_position=5,
        penalty_margin_s=-1.4,
        penalty_threat_name="NORRIS",
    )
    result = _default_rule_engine().evaluate(snap)
    call = next(c for c in result.candidates if c.rule.defn.id == "last_lap")
    assert "P4 on the road, P5 with the penalty" in call.text or ("Penalty puts us P5" in call.text)


def test_plan_no_stop_only_speaks_before_a_pit_stop() -> None:
    base = {
        "phase": "racing",
        "lap_num": 5,
        "active_plan": "A",
        "plan_switch_count": 0,
        "plan_stops_left": 0,
    }
    no_stop = _default_rule_engine().evaluate(_snap(**base, num_pit_stops=0))
    assert "plan_announce_no_stop" in {c.rule.defn.id for c in no_stop.candidates}
    after_stop = _default_rule_engine().evaluate(_snap(**base, num_pit_stops=1))
    assert "plan_announce_no_stop" not in {c.rule.defn.id for c in after_stop.candidates}


def test_race_pit_exit_traffic_is_gated_and_shares_cooldown() -> None:
    engine = _default_rule_engine()
    late = _snap(
        phase="out_lap",
        pit_exit_s=13.0,
        rival_pit_exit_name="LAWSON",
        pit_exit_rival_gap_s=-3.98,
        traffic_behind_s=2.0,
    )
    late_ids = {c.rule.defn.id for c in engine.evaluate(late).candidates}
    assert "pit_exit_traffic_race" not in late_ids

    at_exit = _snap(
        now=1.0,
        phase="out_lap",
        pit_exit_s=1.0,
        rival_pit_exit_name="LAWSON",
        pit_exit_rival_gap_s=-2.0,
        traffic_behind_s=2.0,
    )
    result = engine.evaluate(at_exit)
    ids = {c.rule.defn.id for c in result.candidates}
    assert {"pit_exit_traffic", "pit_exit_traffic_race"} <= ids
    dispatcher = Dispatcher(PolicySettings(min_gap_s=0.0), VirtualClock(), sinks=[])
    dispatcher.submit(result.candidates, at_exit)
    calls = dispatcher.drain(1.0)
    assert len(calls) == 1
    assert calls[0].rule_id == "pit_exit_traffic"


def test_one_decimal_call_copy() -> None:
    qualifying = _default_rule_engine().evaluate(
        _snap(
            session_kind="qualifying",
            phase="out_lap",
            hot_car_behind_s=0.33,
        )
    )
    behind = next(c for c in qualifying.candidates if c.rule.defn.id == "cool_car_behind")
    assert "0.3 seconds" in behind.text

    catching = _default_rule_engine().evaluate(
        _snap(
            phase="racing",
            sector=1,
            battle_mode="catching",
            battle_catch_laps=1.2,
            laps_remaining=5,
            rival_ahead_name="NORRIS",
            battle_pace_ahead="faster",
        )
    )
    catch = next(c for c in catching.candidates if c.rule.defn.id == "battle_catching")
    assert "1.2 laps" in catch.text

    threat = _default_rule_engine().evaluate(
        _snap(
            phase="racing",
            sector=1,
            battle_mode="under_threat",
            battle_threat_laps=0.4,
            laps_remaining=5,
            rival_behind_name="LECLERC",
        )
    )
    under_threat = next(c for c in threat.candidates if c.rule.defn.id == "battle_under_threat")
    assert "0.4 laps" in under_threat.text


@pytest.mark.parametrize(("lop", "plural"), [(1.03, False), (3.2, True)])
def test_tyre_life_one_lap_is_singular(lop: float, plural: bool) -> None:
    """Silverstone race, lap 3: laps_of_pace 1.03 read "About 1 laps left"."""
    result = _default_rule_engine().evaluate(
        _snap(
            phase="racing",
            sector=1,
            lap_num=6,
            laps_remaining=10,
            laps_of_pace=lop,
            _ages={"lap_data": 0.1, "car_damage": 0.1},
        )
    )
    text = next(c.text for c in result.candidates if c.rule.defn.id == "tyre_life")
    assert ("laps" in text) == plural
    assert "1 lap" not in text


def test_battle_catching_never_says_zero_laps() -> None:
    """Silverstone sprint, lap 2: battle_catch_laps 0.0 read "On him in 0.0 laps"."""
    result = _default_rule_engine().evaluate(
        _snap(
            phase="racing",
            sector=1,
            lap_num=2,
            laps_remaining=3,
            battle_mode="catching",
            battle_catch_laps=0.0,
            rival_ahead_name="HAMILTON",
            battle_pace_ahead="1.5 seconds faster",
            gap_ahead_s=1.2,
        )
    )
    text = next(c.text for c in result.candidates if c.rule.defn.id == "battle_catching")
    assert "0.0 laps" not in text
    assert "HAMILTON" in text


def test_qualifying_rules_run_in_sprint_shootout() -> None:
    base = {
        "session_kind": "qualifying",
        "session_type": 10,
        "phase": "in_lap",
        "_ages": {"lap_data": 0.1, "session_history": 0.1},
    }
    ids = {
        c.rule.defn.id
        for c in _default_rule_engine()
        .evaluate(_snap(**base, quali_margin_ms=1_200, quali_margin_s=1.2, quali_margin_kind="cut"))
        .candidates
    }
    assert "quali_safe_cut" in ids


def test_grid_penalty_rule_fires_in_qualifying() -> None:
    result = _default_rule_engine().evaluate(
        _snap(
            session_kind="qualifying",
            session_type=10,
            grid_penalty_recent=True,
            grid_penalty_places=5,
            _ages={"event": 0.1},
        )
    )
    texts = {c.rule.defn.id: c.text for c in result.candidates}
    assert "5" in texts["grid_penalty"]
    clear = _default_rule_engine().evaluate(
        _snap(session_kind="qualifying", session_type=10, _ages={"event": 0.1})
    )
    assert "grid_penalty" not in {c.rule.defn.id for c in clear.candidates}


def test_rival_pace_drops_steep_gap_trend() -> None:
    """Silverstone race, L3 205 s: PÉREZ last lap 96.324 s vs ours 94.284 s (over
    the 1.5 s lap-time band), gap trend -7.23 s read "7.2 seconds slower"."""
    from pitwall.engine import _rival_pace
    from pitwall.state.session import CarLap

    snap = _snap(cars=(CarLap(last_lap_time_ms=96_324),), player_last_lap_ms=94_284)
    assert _rival_pace(snap, 0, -7.23) == ""
    assert _rival_pace(snap, 0, -0.6) == "six tenths slower"


@pytest.mark.parametrize(
    ("mode", "rule", "extra"),
    [
        ("defending", "battle_defend", {"sector": 0, "battle_closing_behind_s": -7.23}),
        ("defending", "battle_defend", {"sector": 0, "battle_closing_behind_s": 2.0}),
        ("under_threat", "battle_under_threat", {"sector": 1, "battle_threat_laps": 5.0}),
    ],
)
def test_battle_behind_without_pace_words(mode: str, rule: str, extra: dict[str, object]) -> None:
    result = _default_rule_engine().evaluate(
        _snap(
            phase="racing",
            lap_num=3,
            laps_remaining=10,
            battle_mode=mode,
            battle_pace_behind="",
            rival_behind_name="PÉREZ",
            gap_behind_s=0.533,
            **extra,
        )
    )
    text = next(c.text for c in result.candidates if c.rule.defn.id == rule)
    assert "slower" not in text and "faster" not in text
    assert ", ." not in text and not text.endswith(", ")


def test_release_calls_wait_for_the_garage_settle() -> None:
    """Silverstone Q1: the car went from flying (lap 4) to the garage at 495.9 s
    and release_hold fired at 496.2 s, 0.3 s after the return."""
    base: dict[str, object] = {
        "session_kind": "qualifying",
        "session_type": 5,
        "phase": "garage",
        "release_clean": False,
        "release_wait_s": 8.0,
        "time_for_out_lap": True,
        "session_time_left": 600.0,
        "_ages": {"lap_data": 0.1, "session": 0.1, "car_telemetry": 0.1},
    }
    ids = {
        c.rule.defn.id
        for c in _default_rule_engine().evaluate(_snap(**base, garage_s=0.3)).candidates
    }
    assert "release_hold" not in ids
    ids = {
        c.rule.defn.id
        for c in _default_rule_engine().evaluate(_snap(**base, garage_s=10.0)).candidates
    }
    assert "release_hold" in ids


def test_garage_s_counts_engine_time_since_garage_entry() -> None:
    from pitwall.protocol.enums import DriverStatus
    from pitwall.state.session import SessionState

    st = SessionState()
    st.session_type = 5
    st.driver_status = DriverStatus.IN_GARAGE
    assert st.snapshot(100.0).garage_s == 0.0
    assert st.snapshot(110.5).garage_s == pytest.approx(10.5)
    st.driver_status = DriverStatus.OUT_LAP
    assert st.snapshot(111.0).garage_s == 0.0
