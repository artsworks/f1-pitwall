"""Safety-car verdict (docs/27 phase 1): the survival model, the pick, the
run gates and the shadow decision record that lands in `calls`."""

import dataclasses
import json

import pytest

from pitwall.config.loader import ConfigStore
from pitwall.engine import build_engine
from pitwall.model.deg import DegFit
from pitwall.state.session import Damage, Snapshot, TyreSet
from pitwall.store.db import Database
from pitwall.strategy.plans import CompoundModel, PlanInputs
from pitwall.strategy.rejoin import FieldCar
from pitwall.strategy.scverdict import sc_verdict

TH = ConfigStore().current().thresholds
S, M, H = 16, 17, 18


def fit(deg: float) -> DegFit:
    return DegFit(90_000.0, deg, 30.0, 10, 100.0, 0.9, "fit")


def models(
    s_deg: float = 160.0,
    m_deg: float = 100.0,
    h_deg: float = 60.0,
    sd: float = 0.0,
) -> tuple[CompoundModel, ...]:
    return (
        CompoundModel(S, 89_600.0, s_deg, 2, deg_posterior_sd_ms_per_lap=sd),
        CompoundModel(M, 90_000.0, m_deg, 2, deg_posterior_sd_ms_per_lap=sd),
        CompoundModel(H, 90_400.0, h_deg, 2, deg_posterior_sd_ms_per_lap=sd),
    )


def inputs(**kw: object) -> PlanInputs:
    """13-lap race, SC deployed on lap 3: 11 laps remain, mediums fitted."""
    base = PlanInputs(
        lap_num=3,
        laps_remaining=11,
        tyre_age=2,
        current=M,
        cur=fit(110.0),
        cur_cliff_age=15.0,
        compounds=models(),
        used=frozenset({S, M}),
        green_loss_s=22.0,
        sc_loss_s=1.5,
    )
    return dataclasses.replace(base, **kw)  # type: ignore[arg-type]


def verdict(inp: PlanInputs, **kw: object):
    base = dict(
        fresh_sets={S: 2, M: 2, H: 2},
        usable_life={S: 20, M: 20, H: 20},
        cur_wear_pct=30.0,
        cur_wear_per_lap_pct=2.5,
        stacking=False,
        # The fixtures are calibrated to a 1500 ms cliff; the shipped default
        # multiplies it for fighting pace (docs/27 Track 16), so the unit tests
        # pin the multiplier to 1.0 and test_survive_cliff_mult covers it.
        th={**TH, "sc_survive_cliff_mult": 1.0},
    )
    return sc_verdict(inp, **{**base, **kw})  # type: ignore[arg-type]


def row(v: object, rows) -> object:
    return next(r for r in rows if r.compound == v and not r.current)


def test_verdict_picks_fastest_surviving_compound() -> None:
    # laps_needed = 10 + 0.5; softs (pace life 9.4) fail, mediums (15) and
    # hards (25) survive and the medium is the quicker stop.
    v = verdict(inputs())
    assert v.pick == M
    assert v.laps_needed == 10.5
    assert v.saving_s == pytest.approx(20.5)
    assert [r.compound for r in v.rows] == [H, M, S, M]
    assert v.rows[-1].current is True
    soft = row(S, v.rows)
    assert soft.survives is False and soft.survival == 0.0
    medium = row(M, v.rows)
    assert medium.survives is True and medium.margin_laps == pytest.approx(4.5)
    assert medium.race_time_s is not None and medium.race_time_s < row(H, v.rows).race_time_s  # type: ignore[operator]


def test_verdict_stay_out_when_no_compound_survives() -> None:
    v = verdict(inputs(), usable_life={S: 9, M: 10, H: 10})
    assert v.pick == 0
    assert v.reason == "stay out, nothing reaches the flag"
    assert all(not r.survives for r in v.rows if not r.current)
    # The fitted set can still survive: its row uses measured wear and age.
    assert v.rows[-1].survives is True


def test_usable_life_below_laps_needed_zeroes_survival() -> None:
    v = verdict(inputs(), usable_life={S: 20, M: 10, H: 20}, cur_wear_pct=60.0)
    medium = row(M, v.rows)
    assert medium.survival == 0.0 and medium.survives is False
    assert v.pick == H  # mediums are out on wear, hards take the stop


def test_missing_tyres_packet_skips_wear_gate_and_set_counts() -> None:
    v = verdict(inputs(), fresh_sets={}, usable_life={})
    assert v.pick == M
    assert all(r.sets == -1 and r.usable_life_laps == 0 for r in v.rows)
    # Softs still fail on pace life; the wear gate simply does not apply.
    assert row(S, v.rows).survival == 0.0


def test_zero_fresh_sets_blocks_the_pick() -> None:
    v = verdict(inputs(), fresh_sets={S: 2, M: 0, H: 2}, cur_wear_pct=60.0)
    assert row(M, v.rows).sets == 0 and row(M, v.rows).survives is True
    assert v.pick == H


def test_two_compound_rule_blocks_a_repeat_compound() -> None:
    only_medium = verdict(inputs(used=frozenset({M})))
    assert only_medium.pick == H
    wet_race = verdict(inputs(used=frozenset({M, 7})))
    assert wet_race.pick == M


def test_double_stack_loss_can_flip_to_stay_out() -> None:
    v = verdict(inputs(sc_loss_s=20.0), stacking=True)
    assert v.saving_s == pytest.approx(-2.0)
    assert v.pick == 0
    assert v.reason == "stay out, the stop saves nothing"


def test_s_zero_pace_survival_is_a_step() -> None:
    # cliff limit is 1500 / 10.5 = 142.857 ms/lap.
    under = row(M, verdict(inputs(compounds=models(m_deg=142.0))).rows)
    assert under.survival == 1.0 and under.survives is True
    over = row(M, verdict(inputs(compounds=models(m_deg=143.0))).rows)
    assert over.survival == 0.0 and over.survives is False
    spread = verdict(inputs(compounds=models(m_deg=143.0, sd=20.0)))
    medium = row(M, spread.rows)
    assert 0.0 < medium.survival < 1.0 and medium.survives is False


def test_survive_min_comes_from_the_mode_mapping() -> None:
    # deg 132 at sd 60 gives survival ~= 0.57: above aggressive, below balanced.
    # Softs at deg 200 fail in both modes.
    inp = inputs(compounds=models(s_deg=200.0, m_deg=132.0, sd=60.0))
    balanced = verdict(inp, cur_wear_pct=60.0)
    assert row(M, balanced.rows).survives is False and balanced.pick == H
    aggressive = verdict(
        inp,
        cur_wear_pct=60.0,
        th={**TH, "sc_survive_min": 0.45, "sc_survive_cliff_mult": 1.0},
    )
    assert row(M, aggressive.rows).survives is True and aggressive.pick == M


def test_current_lap_weight_moves_the_wear_gate_boundary() -> None:
    kw = dict(usable_life={S: 20, M: 10, H: 20}, cur_wear_pct=60.0)
    half = verdict(inputs(), **kw)
    assert row(M, half.rows).survives is False and half.pick == H
    free = verdict(
        inputs(),
        **kw,
        th={**TH, "sc_verdict_current_lap_weight": 0.0, "sc_survive_cliff_mult": 1.0},
    )
    # needed drops to 10.0, so a 10-lap usable life just survives.
    assert row(M, free.rows).survives is True and free.pick == M


def test_no_pick_when_current_set_survives_and_is_not_slower() -> None:
    v = verdict(inputs(cur=fit(100.0)))
    current = v.rows[-1]
    assert current.current is True and current.survives is True
    assert current.race_time_s <= row(M, v.rows).race_time_s  # type: ignore[operator]
    assert v.pick == 0
    assert v.reason == "stay out, the current set is faster"


def test_pick_stays_when_current_set_does_not_survive() -> None:
    v = verdict(inputs(cur=fit(100.0)), cur_wear_pct=60.0)
    assert v.rows[-1].survives is False
    assert v.pick == M


def test_pick_stays_when_a_second_compound_is_owed() -> None:
    v = verdict(inputs(cur=fit(100.0), used=frozenset({M})))
    assert v.pick == H


def test_survivor_pick_sequence_starts_with_current() -> None:
    v = verdict(inputs())
    assert v.sequence == (M, M)
    assert v.to_flag is True


def test_stay_out_has_an_empty_sequence() -> None:
    v = verdict(inputs(), usable_life={S: 9, M: 10, H: 10})
    assert v.pick == 0
    assert v.sequence == ()
    assert v.to_flag is False


def test_two_stop_now_beats_waiting_when_nothing_reaches_the_flag() -> None:
    # A stop now costs 1.5 s under SC. The worn set makes a later stop pay
    # more than a stop now plus a second one.
    inp = inputs(
        cur=fit(700.0),
        cur_cliff_age=2.0,
        compounds=models(s_deg=100.0, m_deg=100.0, h_deg=100.0),
    )
    v = verdict(inp, usable_life={S: 9, M: 10, H: 10}, cur_wear_pct=60.0)
    assert len(v.sequence) == 3 and v.sequence[0] == M
    assert v.pick == v.sequence[1]
    assert v.to_flag is False
    assert "two stops" in v.reason


def test_two_stop_slower_than_waiting_stays_out() -> None:
    v = verdict(inputs(), usable_life={S: 9, M: 10, H: 10})
    assert v.pick == 0
    assert v.reason == "stay out, nothing reaches the flag"


def capped(life: int, **kw: float) -> tuple[CompoundModel, ...]:
    return tuple(dataclasses.replace(m, usable_life_laps=life) for m in models(**kw))


def test_two_stop_stints_must_fit_usable_life() -> None:
    # 11 laps on 3-lap sets: no split of two fresh stints covers the race.
    inp = inputs(
        cur=fit(700.0),
        cur_cliff_age=2.0,
        compounds=capped(3, s_deg=100.0, m_deg=100.0, h_deg=100.0),
    )
    v = verdict(inp, usable_life={S: 3, M: 3, H: 3}, cur_wear_pct=60.0)
    assert v.pick == 0
    assert v.sequence == ()
    assert v.reason == "stay out, nothing reaches the flag"


def test_two_stop_fits_when_stints_fit_usable_life() -> None:
    inp = inputs(
        cur=fit(700.0),
        cur_cliff_age=2.0,
        compounds=capped(6, s_deg=100.0, m_deg=100.0, h_deg=100.0),
    )
    v = verdict(inp, usable_life={S: 6, M: 6, H: 6}, cur_wear_pct=60.0)
    assert len(v.sequence) == 3 and "two stops" in v.reason


def test_two_stop_runs_when_only_survivor_has_no_fresh_sets() -> None:
    # Hards survive to the flag but none are fresh; two soft sets each fall
    # short of the flag yet together cover the race faster than waiting.
    compounds = (
        CompoundModel(S, 89_600.0, 100.0, 2, usable_life_laps=6),
        CompoundModel(M, 90_000.0, 100.0, 0, usable_life_laps=6),
        CompoundModel(H, 90_400.0, 60.0, 0, usable_life_laps=20),
    )
    inp = inputs(cur=fit(700.0), cur_cliff_age=2.0, compounds=compounds)
    v = verdict(
        inp,
        fresh_sets={S: 2, M: 0, H: 0},
        usable_life={S: 6, M: 6, H: 20},
        cur_wear_pct=60.0,
    )
    assert v.sequence == (M, S, S)
    assert "two stops" in v.reason


def test_two_stop_needs_a_fresh_set_for_each_stint() -> None:
    # The hards survive to the flag on paper but have no fresh set; the
    # softs fall short alone yet two soft stints would cover the race.
    # Only one soft set is fresh, so the second stint would run worn.
    compounds = (
        CompoundModel(S, 89_600.0, 100.0, 2, usable_life_laps=12),
        CompoundModel(M, 90_000.0, 100.0, 0, usable_life_laps=12),
        CompoundModel(H, 90_400.0, 60.0, 1, usable_life_laps=23),
    )
    inp = inputs(laps_remaining=20, cur=fit(700.0), cur_cliff_age=2.0, compounds=compounds)
    kw = dict(
        usable_life={S: 12, M: 12, H: 23},
        cur_wear_pct=60.0,
    )
    v = verdict(inp, fresh_sets={S: 1, M: 0, H: 0}, **kw)
    assert v.pick == 0
    assert v.sequence == ()
    assert v.reason == "stay out, survivors have no legal stop"
    # Two fresh soft sets make the same two-stop legal again.
    stocked = verdict(inp, fresh_sets={S: 2, M: 0, H: 0}, **kw)
    assert stocked.sequence == (M, S, S)


def test_two_stop_not_vetoed_by_a_green_plan_on_a_worn_set() -> None:
    # ~18 laps left: one fresh soft and one fresh medium, only a worn hard.
    # The fastest green plan stops once for the hard at fresh pace, but no
    # fresh hard exists; the fresh two-stop must still be called.
    compounds = (
        CompoundModel(S, 89_600.0, 160.0, 1, usable_life_laps=9),
        CompoundModel(M, 90_000.0, 100.0, 1, usable_life_laps=9),
        CompoundModel(H, 89_000.0, 40.0, 1, usable_life_laps=20),
    )
    inp = inputs(
        laps_remaining=18,
        cur=fit(700.0),
        cur_cliff_age=2.0,
        compounds=compounds,
    )
    v = verdict(
        inp,
        fresh_sets={S: 1, M: 1, H: 0},
        usable_life={S: 9, M: 9, H: 20},
        cur_wear_pct=60.0,
    )
    assert len(v.sequence) == 3 and v.sequence[0] == M
    assert v.pick == v.sequence[1]
    assert v.to_flag is False
    assert "two stops" in v.reason


def test_two_stop_race_time_counts_the_stack_delay() -> None:
    # Unstacked the two-stop beats waiting for green by ~0.7 s; the 4 s
    # double-stack delay turns that into a ~3 s loss, so the stop comes off.
    inp = inputs(
        cur=fit(700.0),
        cur_cliff_age=2.0,
        compounds=models(s_deg=100.0, m_deg=100.0, h_deg=100.0),
    )
    kw = dict(usable_life={S: 9, M: 10, H: 10}, cur_wear_pct=60.0)
    unstacked = verdict(inp, **kw)
    assert len(unstacked.sequence) == 3 and "two stops" in unstacked.reason
    stacked = verdict(inp, stacking=True, **kw)
    assert stacked.pick == 0
    assert stacked.sequence == ()
    assert stacked.reason == "stay out, nothing reaches the flag"


def _engine(tmp_path, db=None):
    return build_engine(
        isolated=True,
        decision_log_path=tmp_path / "decisions.jsonl",
        sinks=[],
        db=db,
    )


def _race_fit() -> DegFit:
    return DegFit(90_000.0, 100.0, 30.0, 20, 0.0, 0.9, "prior")


def _sc_snap(**kw: object) -> Snapshot:
    base = dict(
        now=1.0,
        session_kind="race",
        phase="sc",
        race_phase="sc",
        safety_car_status=1,
        lap_num=3,
        laps_remaining=11,
        tyre_age_laps=2,
        tyre_visual=17,
        wear_mean_pct=30.0,
        wear_max_pct=30.0,
        wear_per_lap_pct=2.5,
        laps_of_pace=15.0,
        pit_loss_s=1.5,
        pit_loss_source="session",
        tyre_sets=(
            TyreSet(visual=16, available=1, life_span=14, usable_life=14),
            TyreSet(visual=17, available=1, life_span=16, usable_life=16),
            TyreSet(visual=18, available=1, life_span=22, usable_life=22),
        ),
        fresh_sets_soft=1,
        fresh_sets_medium=1,
        fresh_sets_hard=1,
        fresh_sets_current=1,
        _ages={"lap_data": 0.1, "car_status": 0.1},
    )
    return Snapshot(**{**base, **kw})  # type: ignore[arg-type]


def _sc_engine(tmp_path, db):
    engine = _engine(tmp_path, db)
    engine.ctx.state.session_uid = 42
    fit = _race_fit()
    engine.deg_fit = fit
    engine.ctx.fresh_prior = fit
    engine.ctx.green_pit_loss_s = 22.0
    engine.ctx.sc_pit_loss_s = 1.5
    return engine


def _verdict_calls(db: Database) -> list[dict]:
    return [c for c in db.calls_for_session(42) if c["rule_id"] == "sc_verdict"]


def test_sc_verdict_writes_one_suppressed_call_with_the_ladder(tmp_path) -> None:
    db = Database(":memory:")
    engine = _sc_engine(tmp_path, db)

    engine.planner._plan(_sc_snap())

    calls = _verdict_calls(db)
    assert len(calls) == 1
    call = calls[0]
    assert call["outcome"] == "suppressed"
    assert call["lap"] == 3
    inputs = json.loads(call["inputs"])
    # Uniform deg models: every compound survives the fighting-pace cliff and
    # the soft is the quickest stop, so it takes the pick (docs/27 Track 16).
    assert inputs["pick"] == S
    assert inputs["pick_compound"] == "soft"
    assert inputs["survive_min"] == pytest.approx(0.60)
    ladder = inputs["ladder"]
    assert [r["compound"] for r in ladder] == [H, M, S, M]
    assert ladder[-1]["current"] is True
    for r in ladder:
        for key in (
            "compound",
            "current",
            "sets",
            "pace_life_laps",
            "usable_life_laps",
            "laps_needed",
            "margin_laps",
            "survival",
            "survives",
            "race_time_s",
        ):
            assert key in r


def test_sc_verdict_logs_once_per_lap(tmp_path) -> None:
    db = Database(":memory:")
    engine = _sc_engine(tmp_path, db)

    engine.planner._plan(_sc_snap())
    engine.planner._plan(_sc_snap())
    engine.planner._plan(_sc_snap(lap_num=4, laps_remaining=10, tyre_age_laps=3))

    assert [c["lap"] for c in _verdict_calls(db)] == [3, 4]


@pytest.mark.parametrize(
    "kw",
    [
        {"is_sprint": True},
        {"short_race": True},
        {"race_phase": "vsc", "phase": "vsc", "safety_car_status": 2, "sc_ending": True},
        {"laps_remaining": 3},
        {"race_phase": "racing", "phase": "racing", "safety_car_status": 0},
        {"tyre_visual": 7},
    ],
    ids=["sprint", "short-race", "vsc-ending", "laps-left", "green", "wet"],
)
def test_sc_verdict_run_gates(tmp_path, kw) -> None:
    db = Database(":memory:")
    engine = _sc_engine(tmp_path, db)

    engine.planner._plan(_sc_snap(**kw))

    assert _verdict_calls(db) == []


def test_sc_verdict_runs_under_full_sc_that_is_ending(tmp_path) -> None:
    db = Database(":memory:")
    engine = _sc_engine(tmp_path, db)
    snap = _sc_snap(sc_ending=True)

    engine.planner._plan(snap)

    assert len(_verdict_calls(db)) == 1


@pytest.mark.parametrize(
    ("offset", "logged"),
    [(-0.05, 0.55), (-0.30, 0.45), (0.50, 0.85)],
    ids=["learned", "floor", "ceiling"],
)
def test_sc_verdict_applies_learned_survive_offset_within_bounds(tmp_path, offset, logged) -> None:
    db = Database(":memory:")
    db.set_param(-1, -1, "sc_survive_offset", offset, 1.0)
    engine = _sc_engine(tmp_path, db)

    engine.planner._plan(_sc_snap())

    (call,) = _verdict_calls(db)
    assert json.loads(call["inputs"])["survive_min"] == pytest.approx(logged)


def test_sc_verdict_rides_the_snapshot_until_green(tmp_path) -> None:
    engine = _sc_engine(tmp_path, Database(":memory:"))
    planner = engine.planner

    under_sc = planner._plan(_sc_snap())
    assert under_sc.sc_verdict is not None
    assert len(under_sc.sc_verdict.rows) == 4
    assert planner._plan(_sc_snap()).sc_verdict is under_sc.sc_verdict

    green = planner._plan(
        _sc_snap(race_phase="racing", phase="racing", safety_car_status=0, lap_num=4)
    )
    assert green.sc_verdict is None
    pit = planner._plan(_sc_snap(race_phase="pit", phase="pit", lap_num=4))
    assert pit.sc_verdict is None


def test_sc_verdict_pick_boxes_through_plan_c(tmp_path) -> None:
    engine = _sc_engine(tmp_path, Database(":memory:"))

    planned = engine.planner._plan(_sc_snap())

    assert planned.pit_plan == "cheap_stop"
    assert planned.active_plan == "C"
    assert planned.plan_next_compound == "soft"
    assert planned.sc_call == "box"
    fired = {c.rule.id for c in engine.rule_engine.evaluate(planned).candidates}
    assert fired & {"box_now", "plan_sc_box"}
    assert not fired & {"plan_switch", "plan_switch_no_stop", "plan_invalid"}


def test_sc_verdict_stay_replaces_the_wear_gate(tmp_path) -> None:
    engine = _sc_engine(tmp_path, Database(":memory:"))
    worn = dict(
        wear_mean_pct=60.0,
        wear_max_pct=60.0,
        tyre_sets=tuple(
            TyreSet(visual=v, available=1, life_span=7, usable_life=7) for v in (16, 17, 18)
        ),
    )
    stays = 0
    for lap in (3, 4, 5):
        snap = _sc_snap(
            lap_num=lap,
            laps_remaining=11 - (lap - 3),
            tyre_age_laps=2 + (lap - 3),
            **worn,
        )
        planned = engine.planner._plan(snap)
        assert planned.pit_plan == "stay"
        assert planned.sc_call == "stay"
        fired = {c.rule.id for c in engine.rule_engine.evaluate(planned).candidates}
        assert not fired & {"box_now", "plan_sc_box"}
        stays += "sc_stay_out" in fired
    assert stays == 1

    green = engine.planner._plan(
        _sc_snap(
            race_phase="racing",
            phase="racing",
            safety_car_status=0,
            lap_num=6,
            laps_remaining=8,
            tyre_age_laps=5,
            **worn,
        )
    )
    assert green.sc_call == ""
    assert "plan_invalid" not in {c.rule.id for c in engine.rule_engine.evaluate(green).candidates}


def test_sc_verdict_runs_without_a_deg_fit(tmp_path) -> None:
    engine = _sc_engine(tmp_path, Database(":memory:"))
    engine.ctx.deg_fit = None

    planned = engine.planner._plan(_sc_snap())

    assert planned.sc_call != ""


def test_stop_under_sc_counts_toward_plan_c(tmp_path) -> None:
    engine = _sc_engine(tmp_path, Database(":memory:"))
    planner = engine.planner
    assert planner._plan(_sc_snap()).active_plan == "C"
    planner._plan(_sc_snap(pit_status=1))
    planner._plan(_sc_snap(pit_status=2, num_pit_stops=1, tyre_visual=S))
    planner._plan(_sc_snap(pit_status=0, num_pit_stops=1, tyre_visual=S))

    green = planner._plan(
        _sc_snap(
            race_phase="racing",
            phase="racing",
            safety_car_status=0,
            lap_num=4,
            laps_remaining=10,
            tyre_visual=S,
            num_pit_stops=1,
        )
    )

    assert planner._stops_done == 1
    assert green.plan_switch_reason != "invalid"
    assert "plan_invalid" not in {c.rule.id for c in engine.rule_engine.evaluate(green).candidates}


def test_sc_stop_not_double_counted_when_sc_ends_in_pit_lane(tmp_path) -> None:
    engine = _sc_engine(tmp_path, Database(":memory:"))
    planner = engine.planner
    planner._plan(_sc_snap())
    planner._plan(_sc_snap(pit_status=1))
    planner._plan(_sc_snap(pit_status=2, num_pit_stops=1, tyre_visual=H))
    for phase in ("in_lap", "out_lap", "racing"):
        planner._plan(
            _sc_snap(
                race_phase=phase,
                phase=phase,
                safety_car_status=0,
                lap_num=4,
                laps_remaining=10,
                pit_status=1 if phase == "in_lap" else 0,
                num_pit_stops=1,
                tyre_visual=H,
            )
        )

    assert planner._stops_done == 1


def test_green_flag_stop_counts_once(tmp_path) -> None:
    engine = _sc_engine(tmp_path, Database(":memory:"))
    planner = engine.planner
    base = dict(
        race_phase="racing",
        phase="racing",
        safety_car_status=0,
        lap_num=4,
        laps_remaining=10,
        tyre_visual=M,
    )

    def snap(**kw: object) -> Snapshot:
        return _sc_snap(**{**base, **kw})

    planner._plan(snap())
    planner._plan(snap(race_phase="in_lap", phase="in_lap", pit_status=1))
    planner._plan(
        snap(race_phase="in_lap", phase="in_lap", pit_status=2, num_pit_stops=1, tyre_visual=H)
    )
    planner._plan(snap(race_phase="out_lap", phase="out_lap", num_pit_stops=1, tyre_visual=H))
    planner._plan(snap(lap_num=5, laps_remaining=9, num_pit_stops=1, tyre_visual=H))

    assert planner._stops_done == 1


def test_rain_due_holds_instead_of_picking() -> None:
    picked = verdict(inputs())
    assert picked.pick == M and picked.hold is False
    held = verdict(inputs(), rain_due=True)
    assert held.pick == 0 and held.sequence == ()
    assert held.to_flag is False and held.hold is True
    assert held.reason == "hold, rain is due"
    assert len(held.rows) == len(picked.rows)


def test_penalty_adds_to_stop_race_times_not_the_current_set() -> None:
    base = verdict(inputs())
    penalised = verdict(inputs(), penalty_s=5.0)
    assert penalised.saving_s == pytest.approx(base.saving_s - 5.0, abs=0.01)
    for b, p in zip(base.rows, penalised.rows, strict=True):
        if b.current:
            assert p.race_time_s == pytest.approx(b.race_time_s, abs=0.01)
        else:
            assert p.race_time_s == pytest.approx(b.race_time_s + 5.0, abs=0.01)


def test_penalty_larger_than_the_saving_stays_out() -> None:
    v = verdict(inputs(), penalty_s=25.0)
    assert v.pick == 0
    assert v.reason == "stay out, the stop saves nothing"


def test_rain_crossover_silences_the_stay_call(tmp_path) -> None:
    engine = _sc_engine(tmp_path, Database(":memory:"))
    worn = dict(
        wear_mean_pct=60.0,
        wear_max_pct=60.0,
        tyre_sets=tuple(
            TyreSet(visual=v, available=1, life_span=7, usable_life=7) for v in (16, 17, 18)
        ),
    )
    planned = engine.planner._plan(_sc_snap(weather_crossover="to_inter", **worn))

    assert planned.sc_call == ""
    fired = {c.rule.id for c in engine.rule_engine.evaluate(planned).candidates}
    assert not fired & {"sc_stay_out", "box_now", "plan_sc_box"}


def test_large_penalty_turns_the_box_into_a_stay(tmp_path) -> None:
    picked = _sc_engine(tmp_path, Database(":memory:")).planner._plan(_sc_snap())
    assert picked.sc_call == "box"

    engine = _sc_engine(tmp_path, Database(":memory:"))
    planned = engine.planner._plan(_sc_snap(penalty_s=60))

    assert planned.sc_call == "stay"
    fired = {c.rule.id for c in engine.rule_engine.evaluate(planned).candidates}
    assert not fired & {"plan_sc_box", "box_now"}


def test_survive_cliff_mult_widens_the_fighting_pace_bound() -> None:
    # Track 16 (docs/27): at the raw cliff the softs' 9.4-lap pace life fails
    # a 10.5-lap need; the fighting-pace multiplier lets them survive and the
    # quicker compound takes the stop.
    plain = verdict(inputs(), th={**TH, "sc_survive_cliff_mult": 1.0})
    assert row(S, plain.rows).survives is False and plain.pick == M
    wide = verdict(inputs(), th={**TH, "sc_survive_cliff_mult": 1.5})
    assert row(S, wide.rows).survives is True and wide.pick == S


def test_position_guard_vetoes_a_stop_that_loses_places() -> None:
    # The medium stop regains ~1.4 places on pace at a 2 s gap; losing two
    # places to the stop is a net loss, so the verdict stays out.
    v = verdict(inputs(), positions_lost=2.0, gap_ahead_s=2.0)
    assert v.pick == 0
    assert v.reason == "stay out, the stop loses positions"
    assert v.net_positions is not None and v.net_positions < 0
    # Without the field projection the same pace inputs keep the pick.
    plain = verdict(inputs())
    assert plain.pick == M and plain.net_positions is None
    # A closer car ahead flips the regain positive and the stop stands.
    close = verdict(inputs(), positions_lost=2.0, gap_ahead_s=0.5)
    assert close.pick == M
    assert close.net_positions is not None and close.net_positions > 0


def test_position_guard_needs_the_current_set_to_survive() -> None:
    # A current set that fails its own survival row leaves the pick alone:
    # the stop is mandatory on tyres, not a position trade.
    v = verdict(inputs(cur=fit(100.0)), cur_wear_pct=60.0, positions_lost=5.0)
    assert v.pick == M


def test_positions_lost_counts_the_penalty_once(tmp_path) -> None:
    # P1 serving a 3 s penalty: the field's penalty-included gap to the car
    # behind reads 10 s, its road gap 13 s. The stop costs 12 s (9 s SC loss
    # + 3 s service) plus the 0.8 s warm-up, so the lead survives on road
    # gaps; penalty-adjusted gaps would count the penalty twice and report a
    # lost place.
    engine = _engine(tmp_path, Database(":memory:"))
    snap = _sc_snap(
        position=1,
        penalty_s=3,
        field_cars=(FieldCar("RIVAL", 10.0, 90_000, 100.0, False),),
        field_cars_road=(FieldCar("RIVAL", 13.0, 90_000, 100.0, False),),
    )
    inp = dataclasses.replace(inputs(), sc_loss_s=9.0)
    assert engine.planner._positions_lost(snap, inp, 3.0, 0.0) == 0.0
    # The same projection on penalty-adjusted gaps does lose the place, which
    # is the double count the road gaps remove.
    double = dataclasses.replace(snap, field_cars_road=snap.field_cars)
    assert engine.planner._positions_lost(double, inp, 3.0, 0.0) == 1.0


def test_damage_stop_beats_the_faster_current_set() -> None:
    # The worn current set survives and is quicker on paper, but damage pace
    # sits outside the tyre model: the stop still goes ahead (docs/27 clash 12).
    v = verdict(inputs(cur=fit(100.0), damage_pace_ms=800.0))
    assert v.pick == M
    assert v.reason.startswith("damage,")


def test_pit_decision_folds_the_verdict_and_cancels_before_commit(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _sc_engine(tmp_path, Database(":memory:"))
    planner = engine.planner
    # The box call reached the radio, so its reversal can be cancelled.
    monkeypatch.setattr(planner, "_box_call_spoken", lambda snap: True)

    box = planner._plan(_sc_snap())
    assert box.pit_decision is not None
    assert box.pit_decision.action == "box"
    assert box.pit_decision_key == "box:16"
    assert box.pit_cancel == ""

    # A 60 s penalty turns the verdict into a stay on the same lap: the
    # decision key changes and the reversal is still actionable, so the
    # cancel line arms and the pit_cancel_box rule fires.
    stay = planner._plan(_sc_snap(now=4.0, penalty_s=60.0))
    assert stay.sc_call == "stay"
    assert stay.pit_decision is not None and stay.pit_decision.action == "stay"
    assert stay.pit_decision_key == "stay"
    assert stay.pit_cancel == "Stay out"
    fired = {c.rule.id for c in engine.rule_engine.evaluate(stay).candidates}
    assert "pit_cancel_box" in fired
    assert "sc_stay_out" in fired

    # Past the commitment point (pit entry already passed) the same flip arms
    # no cancel: the driver can no longer act, so it is guidance only.
    committed = planner._plan(_sc_snap(now=4.0, penalty_s=60.0, pit_entry_passed=True))
    assert committed.pit_cancel == ""


def test_rain_hold_keeps_an_announced_damage_stop(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Rain due under SC holds the tyre verdict, but damage pace overrides it
    # in optimise(): the final plan still says box for the repair. The shared
    # decision must follow that plan, so no 'Hold for rain' cancel reaches
    # the driver for a stop the strategy still recommends.
    engine = _sc_engine(tmp_path, Database(":memory:"))
    planner = engine.planner
    monkeypatch.setattr(planner, "_box_call_spoken", lambda snap: True)
    wing = Damage(front_left_wing=40, front_right_wing=40)

    box = planner._plan(_sc_snap(damage=wing))
    assert box.pit_decision is not None and box.pit_decision.action == "box"

    held = planner._plan(
        _sc_snap(
            now=4.0,
            lap_num=4,
            laps_remaining=10,
            tyre_age_laps=3,
            damage=wing,
            weather_crossover="to_inter",
        )
    )
    assert held.sc_verdict is not None and held.sc_verdict.hold
    assert held.pit_plan == "box_now"
    assert held.pit_decision is not None and held.pit_decision.action == "box"
    assert held.pit_cancel == ""
    fired = {c.rule.id for c in engine.rule_engine.evaluate(held).candidates}
    assert "pit_cancel_box" not in fired
    assert "box_now" in fired


def test_pit_cancel_needs_the_box_call_to_have_been_out(tmp_path) -> None:
    # A 'box' decision whose call never reached the radio (still queued or
    # gated out when the plan flipped) earns no cancel line.
    engine = _sc_engine(tmp_path, Database(":memory:"))
    planner = engine.planner

    planner._plan(_sc_snap())
    flipped = planner._plan(_sc_snap(penalty_s=60.0))

    assert flipped.pit_cancel == ""
    fired = {c.rule.id for c in engine.rule_engine.evaluate(flipped).candidates}
    assert "pit_cancel_box" not in fired


def test_worst_corner_wear_life_bounds_the_current_row() -> None:
    # The blended rate says the mediums have 16 laps of wear life, but the
    # worst corner reaches the cliff in 8: under the 10.5-lap need the
    # current set does not survive and the stop stands on tyres.
    v = verdict(inputs(), cur_wear_life_laps=8.0)
    cur = v.rows[-1]
    assert cur.current is True
    assert cur.survives is False and cur.survival == 0.0
    assert cur.margin_laps == pytest.approx(8.0 - v.laps_needed)
    # The estimate alone would have kept the row alive.
    plain = verdict(inputs())
    assert plain.rows[-1].survives is True
