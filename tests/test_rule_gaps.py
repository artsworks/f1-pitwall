"""Rule-level checks for packaged rules that no other test names."""

from __future__ import annotations

from typing import Any

import pytest

from pitwall.config.loader import ConfigStore
from pitwall.rules.engine import RuleEngine
from pitwall.state.session import Damage, Snapshot

SETTINGS = ConfigStore(isolated=True).current()
TH: dict[str, Any] = SETTINGS.thresholds
MODE = SETTINGS.resolved_mindset()
PACKETS = ("lap_data", "car_damage", "participants", "session_history", "car_setups", "session")
RACE: dict[str, object] = {
    "session_kind": "race",
    "session_type": 15,
    "phase": "racing",
    "lap_num": 6,
    "laps_remaining": 5,
}
QUALI: dict[str, object] = {"session_kind": "qualifying", "session_type": 5, "phase": "out_lap"}
TOW: dict[str, object] = {
    "dist_to_line_m": TH["prep_gap_m"],
    "traffic_ahead_kind": "flying",
    "traffic_ahead_s": TH["tow_max_s"],
    "traffic_ahead_teammate": True,
}


def _fires(rule_id: str, **fields: object) -> bool:
    engine = RuleEngine(list(SETTINGS.rules), thresholds=TH, mode=MODE)
    snap = Snapshot(now=0.0, _ages={k: 0.1 for k in PACKETS}, **fields)  # type: ignore[arg-type]
    result = engine.evaluate(snap)
    assert not [s for s in result.suppressed if s.rule.defn.id == rule_id]
    return rule_id in {c.rule.defn.id for c in result.candidates}


@pytest.mark.parametrize(
    ("rule_id", "holds", "flipped"),
    [
        ("ers_fault", RACE | {"damage": Damage(ers_fault=1)}, {"damage": Damage()}),
        (
            "floor_damage",
            RACE | {"damage": Damage(diffuser=TH["damage_warn_pct"])},
            {"damage": Damage(diffuser=TH["damage_warn_pct"] - 1)},
        ),
        (
            "gearbox_damage",
            RACE | {"damage": Damage(gearbox=TH["powertrain_damage_warn_pct"])},
            {"damage": Damage(gearbox=TH["powertrain_damage_warn_pct"] - 1)},
        ),
        ("prep_tow_teammate", QUALI | TOW, {"traffic_ahead_teammate": False}),
        (
            "overcut_stay_out",
            RACE | {"pit_plan": "overcut", "pit_plan_confidence": MODE["pit_confidence_min"]},
            {"pit_plan_confidence": MODE["pit_confidence_min"] - 0.01},
        ),
        (
            "plan_off",
            RACE | {"active_plan": "one_stop", "on_plan": False, "plan_off_s": TH["plan_off_s"]},
            {"plan_off_s": TH["plan_off_s"] - 0.1},
        ),
        (
            "plan_target_lap",
            RACE | {"plan_stops_left": 1, "plan_target_lap": 6},
            {"plan_target_lap": 7},
        ),
        ("setup_on_throttle", RACE | {"setup_call_param": "on_throttle"}, {"setup_call_param": ""}),
        ("setup_stop_wing", RACE | {"setup_stop_wing_to": 5.0}, {"setup_stop_wing_to": 0.0}),
        ("off_track_recovered", RACE | {"off_track_recovered": True}, {"phase": "sc"}),
        ("rain_stay_out", RACE | {"rain_onset_min": 5}, {"rain_box_lap": 8}),
    ],
)
def test_rule_fires_only_when_its_condition_holds(
    rule_id: str, holds: dict[str, object], flipped: dict[str, object]
) -> None:
    assert _fires(rule_id, **holds)
    assert not _fires(rule_id, **(holds | flipped))
