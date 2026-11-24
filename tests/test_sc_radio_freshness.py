"""Safety-car radio freshness: a revised Plan C compound must re-announce on
the radio and drop queued calls carrying the old compound, and a mid-lap
verdict change (rain gate / penalty) must refresh the verdict so a queued
stop cannot survive the invalidation."""

from __future__ import annotations

import dataclasses

import pytest

import pitwall.engine.planning as planning
from pitwall.config.loader import ConfigStore
from pitwall.config.models import RuleDefModel
from pitwall.engine import build_engine
from pitwall.model.deg import DegFit
from pitwall.rules.engine import RuleEngine
from pitwall.state.session import Snapshot, TyreSet
from pitwall.store.db import Database
from pitwall.strategy.plans import (
    CompoundModel,
    PlanInputs,
    PlanTracker,
    view_fields,
)
from pitwall.strategy.scverdict import ScVerdict

from .factories import make_snapshot

TH = ConfigStore().current().thresholds
S, M, H = 16, 17, 18


def fit(deg: float) -> DegFit:
    return DegFit(90_000.0, deg, 30.0, 10, 100.0, 0.9, "fit")


def models(
    s_deg: float = 160.0, m_deg: float = 100.0, h_deg: float = 60.0, sets: int = 2
) -> tuple[CompoundModel, ...]:
    return (
        CompoundModel(S, 89_600.0, s_deg, sets),
        CompoundModel(M, 90_000.0, m_deg, sets),
        CompoundModel(H, 90_400.0, h_deg, sets),
    )


def inputs(**kw: object) -> PlanInputs:
    base = PlanInputs(
        lap_num=1,
        laps_remaining=50,
        tyre_age=0,
        current=M,
        cur=fit(100.0),
        cur_cliff_age=40.0,
        compounds=models(),
        used=frozenset({M}),
        green_loss_s=22.0,
        sc_loss_s=11.0,
    )
    return dataclasses.replace(base, **kw)  # type: ignore[arg-type]


_SC_KW = dict(stops_done=0, neutralised=True, cheap_stop=True)


def test_pending_plan_c_compound_revision_refreshes_switch_lap() -> None:
    """A new verdict compound on a pending Plan C is a new box instruction:
    switch_lap moves so plan_sc_box re-fires; a same-compound reprice does not."""
    tr = PlanTracker()
    tr.update(inputs(), TH, stops_done=0, neutralised=False, cheap_stop=False)
    tr.drain()
    st = tr.update(
        inputs(lap_num=15, laps_remaining=36, tyre_age=14),
        TH,
        sc_sequence=(M, H),
        **_SC_KW,
    )
    assert st.active == "C"
    assert view_fields(st, 15).plan_switch_lap == 15
    tr.drain()

    st = tr.update(
        inputs(lap_num=16, laps_remaining=35, tyre_age=15),
        TH,
        sc_sequence=(M, S),
        **_SC_KW,
    )
    assert st.active == "C"
    fields = view_fields(st, 16)
    assert fields.plan_next_compound == "soft"
    assert fields.plan_sc_compound == "soft"
    assert fields.plan_switch_lap == 16

    st = tr.update(
        inputs(lap_num=17, laps_remaining=34, tyre_age=16),
        TH,
        sc_sequence=(M, S),
        **_SC_KW,
    )
    fields = view_fields(st, 17)
    assert fields.plan_sc_compound == "soft"
    assert fields.plan_switch_lap == 16  # pure reprice: no re-announce


def test_plan_sc_compound_is_empty_off_plan_c() -> None:
    tr = PlanTracker()
    st = tr.update(inputs(), TH, stops_done=0, neutralised=False, cheap_stop=False)
    fields = view_fields(st, 1)
    assert fields.plan_next_compound == "hard"
    assert fields.plan_sc_compound == ""


def _engine(tmp_path, db=None):
    engine = build_engine(
        isolated=True,
        decision_log_path=tmp_path / "decisions.jsonl",
        sinks=[],
        db=db,
    )
    engine.ctx.state.session_uid = 42
    fit = DegFit(90_000.0, 100.0, 30.0, 20, 0.0, 0.9, "prior")
    engine.deg_fit = fit
    engine.ctx.fresh_prior = fit
    engine.ctx.green_pit_loss_s = 22.0
    engine.ctx.sc_pit_loss_s = 1.5
    return engine


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


def _verdict_calls(db: Database) -> list[dict]:
    return [c for c in db.calls_for_session(42) if c["rule_id"] == "sc_verdict"]


def test_mid_lap_verdict_change_refreshes_without_extra_log(tmp_path) -> None:
    """Rain gate and penalty move mid-lap: the verdict recomputes, Plan C
    retires on a stay sequence, and the suppressed record stays once per lap."""
    db = Database(":memory:")
    engine = _engine(tmp_path, db)
    planner = engine.planner

    first = planner._plan(_sc_snap())
    assert first.sc_verdict is not None
    assert first.sc_verdict.hold is False and first.sc_verdict.pick == H

    rain = planner._plan(_sc_snap(weather_crossover="to_inter"))
    assert rain.sc_verdict is not first.sc_verdict
    assert rain.sc_verdict.hold is True and rain.sc_verdict.sequence == ()
    assert rain.sc_call == ""
    assert rain.active_plan != "C"
    fired = {c.rule.id for c in engine.rule_engine.evaluate(rain).candidates}
    assert "plan_sc_box" not in fired and "box_now" not in fired

    back = planner._plan(_sc_snap(weather_crossover="", penalty_s=0))
    assert back.sc_verdict is not rain.sc_verdict
    assert back.sc_verdict.hold is False and back.sc_verdict.pick == H

    penalised = planner._plan(_sc_snap(weather_crossover="", penalty_s=30))
    assert penalised.sc_verdict.pick == 0
    assert "stop saves nothing" in penalised.sc_verdict.reason

    assert [c["lap"] for c in _verdict_calls(db)] == [3]


def _sc_verdict(pick: int, sequence: tuple[int, ...]) -> ScVerdict:
    return ScVerdict(
        pick=pick,
        reason="test verdict",
        laps_needed=1.0,
        saving_s=5.0,
        survive_min=0.6,
        rows=(),
        sequence=sequence,
        to_flag=False,
    )


def _patch_verdicts(monkeypatch: pytest.MonkeyPatch, sequences: list[tuple[int, ...]]) -> None:
    verdicts = iter(_sc_verdict(seq[-1] if seq else 0, seq) for seq in sequences)
    monkeypatch.setattr(planning, "sc_verdict", lambda *a, **kw: next(verdicts))


def _fired(engine, planned: Snapshot) -> set[str]:
    return {c.rule.id for c in engine.rule_engine.evaluate(planned).candidates}


# usable_life must cover the remaining laps or the verdict sequence is
# infeasible and Plan C never forms.
_LONG_LIFE_SETS = tuple(
    TyreSet(visual=v, available=1, life_span=30, usable_life=30) for v in (S, M, H)
)


def test_engine_revised_compound_reannounces_plan_sc_box(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real tick order: _plan bumps switch_lap before the revision lap's first
    evaluate, so clear_when alone can never re-arm plan_sc_box there. The
    compound change itself must re-arm it."""
    engine = _engine(tmp_path)
    _patch_verdicts(monkeypatch, [(M, H), (M, S)])
    planner = engine.planner

    planned = planner._plan(
        _sc_snap(lap_num=15, laps_remaining=30, tyre_age_laps=14, tyre_sets=_LONG_LIFE_SETS)
    )
    assert planned.active_plan == "C" and planned.plan_next_compound == "hard"
    assert planned.plan_sc_compound == "hard"
    assert "plan_sc_box" in _fired(engine, planned)

    revised = planner._plan(
        _sc_snap(lap_num=16, laps_remaining=29, tyre_age_laps=15, tyre_sets=_LONG_LIFE_SETS)
    )
    assert revised.plan_next_compound == "soft"
    assert revised.plan_sc_compound == "soft"
    assert revised.plan_switch_lap == 16
    assert "plan_sc_box" in _fired(engine, revised)


def test_engine_consecutive_revisions_fire_and_same_compound_stays_quiet(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Laps 15-18: switch on hards, two back-to-back revisions (softs, hards)
    that must each re-fire, then a same-compound lap that must not."""
    engine = _engine(tmp_path)
    _patch_verdicts(monkeypatch, [(M, H), (M, S), (M, H), (M, H)])
    planner = engine.planner

    def fired_on(lap: int, remaining: int, age: int) -> set[str]:
        planned = planner._plan(
            _sc_snap(
                lap_num=lap,
                laps_remaining=remaining,
                tyre_age_laps=age,
                tyre_sets=_LONG_LIFE_SETS,
            )
        )
        return _fired(engine, planned)

    assert "plan_sc_box" in fired_on(15, 30, 14)
    assert "plan_sc_box" in fired_on(16, 29, 15)
    assert "plan_sc_box" in fired_on(17, 28, 16)
    assert "plan_sc_box" not in fired_on(18, 27, 17)


def test_rearm_on_change_rearms_between_evaluations() -> None:
    """A rule-level field change re-arms a fired rule whose clear_when never
    ran; the first sighting only records the value."""
    rule = RuleDefModel.model_validate(
        {
            "id": "r1",
            "sessions": ["race"],
            "priority": 2,
            "when": "lap_num == 5",
            "clear_when": "lap_num != 5",
            "rearm_on_change": ["plan_sc_compound"],
            "say": "box",
        }
    )
    eng = RuleEngine([rule], thresholds={}, mode={})

    def snap(compound: str, lap: int = 5) -> Snapshot:
        return make_snapshot(
            1.0,
            session_kind="race",
            session_type=15,
            lap_num=lap,
            phase="racing",
            plan_sc_compound=compound,
            _ages={"lap_data": 0.1},
        )

    assert len(eng.evaluate(snap("hard")).candidates) == 1
    assert not eng.evaluate(snap("hard")).candidates
    assert len(eng.evaluate(snap("soft")).candidates) == 1
    assert not eng.evaluate(snap("soft", lap=6)).candidates


def _stop_snap(**kw: object) -> Snapshot:
    base = dict(
        now=1.0,
        session_kind="race",
        phase="sc",
        race_phase="sc",
        lap_num=15,
        active_plan="C",
        plan_switch_reason="sc",
        plan_switch_lap=15,
        plan_next_compound="hard",
        plan_sc_compound="hard",
        pit_plan="cheap_stop",
        pit_plan_reason="Hards to the flag",
        laps_remaining=20,
        _ages={"lap_data": 0.1, "car_status": 0.1, "session": 0.1},
    )
    return Snapshot(**{**base, **kw})  # type: ignore[arg-type]


@pytest.mark.parametrize("rule_id", ["box_now", "plan_sc_box"])
def test_queued_stop_call_dies_when_plan_c_compound_revises(tmp_path, rule_id: str) -> None:
    """A queued call carrying the old compound is dropped at submit and the
    revised call queues with the new compound text."""
    engine = build_engine(isolated=True, decision_log_path=tmp_path / "decisions.jsonl", sinks=[])
    disp = engine.ctx.dispatcher
    snap_hard = _stop_snap()
    disp.submit([engine.rule_engine.candidate_for(rule_id, snap_hard)], snap_hard)
    assert [q.call.rule_id for q in disp._queue] == [rule_id]

    snap_soft = dataclasses.replace(
        snap_hard,
        now=80.0,
        lap_num=16,
        plan_switch_lap=16,
        plan_next_compound="soft",
        plan_sc_compound="soft",
        pit_plan_reason="Softs to the flag",
    )
    disp.submit([engine.rule_engine.candidate_for(rule_id, snap_soft)], snap_soft)

    queued = [q.call for q in disp._queue]
    assert [c.rule_id for c in queued] == [rule_id]
    assert queued[0].count == 1 and "soft" in queued[0].text.lower()
    emitted = disp.drain(now=80.0)
    assert [c.rule_id for c in emitted] == [rule_id]
    assert "soft" in emitted[0].text.lower()


@pytest.mark.parametrize("rule_id", ["box_now", "plan_sc_box"])
def test_queued_stop_call_survives_same_compound_reprice(tmp_path, rule_id: str) -> None:
    """Same compound on a later lap: the queued call merges, no re-fire."""
    engine = build_engine(isolated=True, decision_log_path=tmp_path / "decisions.jsonl", sinks=[])
    disp = engine.ctx.dispatcher
    snap_hard = _stop_snap()
    disp.submit([engine.rule_engine.candidate_for(rule_id, snap_hard)], snap_hard)

    snap_same = dataclasses.replace(snap_hard, now=80.0, lap_num=16)
    disp.submit([engine.rule_engine.candidate_for(rule_id, snap_same)], snap_same)

    queued = [q.call for q in disp._queue]
    assert [c.rule_id for c in queued] == [rule_id]
    assert queued[0].count == 2
