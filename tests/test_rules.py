from __future__ import annotations

import pytest

from pitwall.config.models import RuleDefModel
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
