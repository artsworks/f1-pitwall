"""Safety-car verdict (docs/27 phase 1): the survival model, the pick, the
run gates and the shadow decision record that lands in `calls`."""

import dataclasses
import json

import pytest

from pitwall.config.loader import ConfigStore
from pitwall.engine import build_engine
from pitwall.model.deg import DegFit
from pitwall.state.session import Snapshot, TyreSet
from pitwall.store.db import Database
from pitwall.strategy.plans import CompoundModel, PlanInputs
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
        th=TH,
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
    aggressive = verdict(inp, cur_wear_pct=60.0, th={**TH, "sc_survive_min": 0.45})
    assert row(M, aggressive.rows).survives is True and aggressive.pick == M


def test_current_lap_weight_moves_the_wear_gate_boundary() -> None:
    kw = dict(usable_life={S: 20, M: 10, H: 20}, cur_wear_pct=60.0)
    half = verdict(inputs(), **kw)
    assert row(M, half.rows).survives is False and half.pick == H
    free = verdict(inputs(), **kw, th={**TH, "sc_verdict_current_lap_weight": 0.0})
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
    # Uniform deg models tie on race time; hardest-first order picks the hard.
    assert inputs["pick"] == H
    assert inputs["pick_compound"] == "hard"
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
