"""Fuel-short intervention: the stateful lift-and-coast guard (docs/18).

The model latches `fuel_saving_active` when the fuel budget projects a
deficit. It clears only when this session's measured burn covers the
remaining laps, so an mfd/prior margin uptick never announces a clear.
While armed, fuel calls own the finish target and push calls stay quiet.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Dispatcher
from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.config.models import PolicySettings
from pitwall.engine import build_engine
from pitwall.engine.model import SessionModel
from pitwall.model.budget import FuelBudget
from pitwall.rules.engine import EvalResult, RuleEngine
from pitwall.state.model_view import ModelView
from pitwall.state.session import SessionState, Snapshot

from .factories import make_snapshot as _make_snapshot


def _model(tmp_path: Path) -> SessionModel:
    engine = build_engine(
        isolated=True,
        decision_log_path=tmp_path / "decisions.jsonl",
        sinks=[],
    )
    return engine.model


def _budget(margin: float, source: str = "mfd") -> FuelBudget:
    return FuelBudget(
        laps_remaining=10,
        fuel_laps=10.0 + margin,
        margin_laps=margin,
        per_lap_kg=2.0,
        source=source,
    )


def test_intervention_arms_on_deficit_and_clears_only_on_measured(tmp_path: Path) -> None:
    model = _model(tmp_path)
    update = model._update_fuel_intervention

    update(_budget(0.5, "prior"))
    assert model._fuel_intervention is False

    update(_budget(-0.4, "mfd"))
    assert model._fuel_intervention is True

    update(None)  # no budget: the latch holds
    assert model._fuel_intervention is True

    update(_budget(0.3, "mfd"))  # estimate recovers: not evidence, stays armed
    assert model._fuel_intervention is True

    update(_budget(-0.1, "session"))  # measured but still short: stays armed
    assert model._fuel_intervention is True

    update(_budget(0.2, "session"))  # measured margin: clears
    assert model._fuel_intervention is False


def test_intervention_resets_with_the_model(tmp_path: Path) -> None:
    model = _model(tmp_path)
    model._update_fuel_intervention(_budget(-0.4))
    assert model._fuel_intervention is True
    model.reset()
    assert model._fuel_intervention is False


def test_snapshot_carries_fuel_saving_active() -> None:
    state = SessionState()
    state.set_model(ModelView(fuel_saving_active=True))
    assert state.snapshot(0.0).fuel_saving_active is True
    state.set_model(ModelView())
    assert state.snapshot(1.0).fuel_saving_active is False


def _engine() -> RuleEngine:
    store = ConfigStore()
    settings = store.current()
    return RuleEngine(
        list(settings.rules),
        thresholds=settings.thresholds,
        mode=settings.resolved_mindset(),
        staleness_s=settings.engine.staleness_s,
    )


def _fuel_snapshot(now: float = 0.0, **overrides: object) -> Snapshot:
    values: dict[str, object] = {
        "session_kind": "race",
        "session_type": 15,
        "phase": "racing",
        "lap_num": 5,
        "total_laps": 15,
        "laps_remaining": 10,
        "fuel_per_lap_kg": 2.0,
        "fuel_margin_laps": -0.8,
        "fuel_saving_active": True,
        "fuel_source": "session",
        "sector": 1,
        "_ages": {"car_status": 0.1, "lap_data": 0.1},
    }
    values.update(overrides)
    return _make_snapshot(now, **values)


def _ids(result: EvalResult) -> set[str]:
    return {candidate.rule.id for candidate in result.candidates}


def test_fuel_short_waits_for_the_intervention_flag() -> None:
    engine = _engine()
    off = _fuel_snapshot(0.0, fuel_saving_active=False)
    assert "fuel_short" not in _ids(engine.evaluate(off))
    on = _fuel_snapshot(1.0, fuel_saving_active=True)
    assert "fuel_short" in _ids(engine.evaluate(on))


def test_fuel_short_clear_is_owned_by_the_flag() -> None:
    """An unconfirmed margin uptick is not a clear: the rule stays silent until
    the measured burn confirms the saving, then re-arms for the next deficit."""
    engine = _engine()
    assert "fuel_short" in _ids(engine.evaluate(_fuel_snapshot(0.0)))

    recovered = _fuel_snapshot(1.0, fuel_margin_laps=0.1, fuel_saving_active=True)
    assert "fuel_short" not in _ids(engine.evaluate(recovered))

    back = _fuel_snapshot(2.0, fuel_margin_laps=-0.7, fuel_saving_active=True)
    assert "fuel_short" in _ids(engine.evaluate(back))

    cleared = _fuel_snapshot(3.0, fuel_margin_laps=-0.9, fuel_saving_active=False)
    assert "fuel_short" not in _ids(engine.evaluate(cleared))
    assert "fuel_short" in _ids(engine.evaluate(_fuel_snapshot(4.0)))


def _dispatcher() -> tuple[Dispatcher, io.StringIO]:
    buf = io.StringIO()
    dispatcher = Dispatcher(
        PolicySettings.model_validate({"min_gap_s": 0.0}),
        VirtualClock(),
        decision_log=DecisionLog(fp=buf),
        sinks=[],
    )
    return dispatcher, buf


def _log(buf: io.StringIO) -> list[dict]:
    buf.seek(0)
    return [json.loads(line) for line in buf if line.strip()]


def _queued(dispatcher: Dispatcher, rule_id: str) -> list:
    return [item.call for item in dispatcher._queue if item.call.rule_id == rule_id]


def test_queued_fuel_short_is_re_emitted_when_the_target_moves() -> None:
    engine = _engine()
    dispatcher, buf = _dispatcher()

    snap_a = _fuel_snapshot(0.0, fuel_margin_laps=-0.8)
    dispatcher.submit(engine.evaluate(snap_a).candidates, snap_a)
    queued = _queued(dispatcher, "fuel_short")
    assert len(queued) == 1 and "0.8" in queued[0].text

    # The deficit moved enough to change the spoken tenth: drop the stale
    # queued call and queue the fresh target instead of merging.
    snap_b = _fuel_snapshot(130.0, fuel_margin_laps=-0.7)
    result_b = engine.evaluate(snap_b)
    assert "fuel_short" in _ids(result_b)
    dispatcher.submit(result_b.candidates, snap_b)

    queued = _queued(dispatcher, "fuel_short")
    assert len(queued) == 1 and "0.7" in queued[0].text
    assert any(row.get("suppressed_by") == "revalidation" for row in _log(buf))


def test_fuel_short_does_not_re_emit_inside_the_spoken_precision() -> None:
    """Raw margins drift every tick; the rounded spoken values do not. A move
    inside the tenth keeps the queued call and produces no new candidate."""
    engine = _engine()
    dispatcher, buf = _dispatcher()

    snap_a = _fuel_snapshot(0.0, fuel_margin_laps=-0.8)
    dispatcher.submit(engine.evaluate(snap_a).candidates, snap_a)
    assert len(_queued(dispatcher, "fuel_short")) == 1

    snap_b = _fuel_snapshot(130.0, fuel_margin_laps=-0.84)
    result_b = engine.evaluate(snap_b)
    assert "fuel_short" not in _ids(result_b)
    dispatcher.submit(result_b.candidates, snap_b)

    queued = _queued(dispatcher, "fuel_short")
    assert len(queued) == 1 and "0.8" in queued[0].text
    assert not any(row.get("suppressed_by") == "revalidation" for row in _log(buf))


_PUSH_CASES = {
    "battle_catching": {"battle_mode": "catching", "battle_catch_laps": 3.0},
    "battle_attack": {"battle_mode": "attacking", "battle_attack_pass_prob": 0.9},
    "battle_under_threat": {"battle_mode": "under_threat", "battle_threat_laps": 3.0},
    "overtake_mode": {
        "energy_mode": "attack_ok",
        "overtake_active": True,
        "overtake_available": True,
        "aero_zone_ahead": True,
        "gap_ahead_s": 0.5,
    },
    "overtake_earned": {
        "overtake_available": True,
        "overtake_active": False,
        "gap_ahead_s": 0.8,
    },
    "overtake_lost": {
        "overtake_active": True,
        "overtake_available": False,
        "gap_ahead_s": 0.5,
    },
    # A big estimated margin while the intervention is still armed is exactly
    # the stale-data case: the "push" call must stay suppressed.
    "fuel_spare": {"fuel_margin_laps": 1.5},
}


@pytest.mark.parametrize("rule_id", sorted(_PUSH_CASES))
def test_push_calls_are_suppressed_while_saving(rule_id: str) -> None:
    engine = _engine()
    overrides = _PUSH_CASES[rule_id]

    saving = _fuel_snapshot(0.0, fuel_saving_active=True, **overrides)
    assert rule_id not in _ids(engine.evaluate(saving))

    clear = _fuel_snapshot(1.0, fuel_saving_active=False, **overrides)
    assert rule_id in _ids(engine.evaluate(clear))
