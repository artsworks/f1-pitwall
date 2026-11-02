from __future__ import annotations

import asyncio
import io
from dataclasses import replace

import pytest

from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.engine import build_engine, run_replay
from pitwall.protocol.header import PacketId
from pitwall.rules.engine import RuleEngine
from pitwall.state.model_view import ModelView

from .race_synth import RaceSpec, race_stream
from .synth import pack_packet, write_packet_stream

pytestmark = pytest.mark.replay


def _rules(*ids: str) -> RuleEngine:
    store = ConfigStore(isolated=True)
    settings = store.current()
    return RuleEngine(
        [rule for rule in settings.rules if rule.id in ids],
        thresholds=settings.thresholds,
        mode=settings.resolved_mindset(),
        staleness_s=settings.engine.staleness_s,
    )


def _replay_snapshot(
    telemetry: dict,
    damage: dict,
    *,
    gap_ms: int = 3000,
    lap: int = 14,
    model: ModelView | None = None,
    session_type: int = 15,
):
    engine = build_engine(clock=VirtualClock(), sinks=[])
    packets = [
        (
            PacketId.SESSION,
            {"session_type": session_type, "total_laps": 17, "track_id": 7, "track_length": 5000},
        ),
        (PacketId.EVENT, {"event_string_code": b"LGOT"}),
        (
            PacketId.PARTICIPANTS,
            {"num_active_cars": 2, "cars": {0: {"name": b"Player"}, 1: {"name": b"Norris"}}},
        ),
        (
            PacketId.LAP_DATA,
            {
                "cars": {
                    0: {
                        "current_lap_num": lap,
                        "driver_status": 4,
                        "result_status": 2,
                        "car_position": 2,
                        "lap_distance": 1000,
                        "delta_to_car_in_front_ms_part": gap_ms,
                    },
                    1: {
                        "current_lap_num": lap,
                        "driver_status": 4,
                        "result_status": 2,
                        "car_position": 1,
                        "lap_distance": 1000 + gap_ms / 20,
                    },
                }
            },
        ),
        (PacketId.CAR_STATUS, {"cars": {0: {"fuel_in_tank": 4.0, "fuel_remaining_laps": 3.6}}}),
        (PacketId.CAR_DAMAGE, {"cars": {0: damage}}),
        (PacketId.CAR_TELEMETRY, {"cars": {0: telemetry, 1: {"engine_temperature": 200}}}),
    ]
    for frame, (packet_id, data) in enumerate(packets, 1):
        t = frame / 100
        engine.ingest.on_datagram(pack_packet(packet_id, data, session_time=t, frame=frame), t)
    if model is not None:
        engine.state.set_model(model)
    return engine.state.snapshot(len(packets) / 100)


def _call(engine: RuleEngine, snapshot, rule_id: str):
    return next((c for c in engine.evaluate(snapshot).candidates if c.rule.id == rule_id), None)


@pytest.mark.parametrize(
    "rule_id,telemetry,mid,cold,field,expected",
    [
        (
            "engine_temp_high",
            {"engine_temperature": 128},
            {"engine_temperature": 125},
            {"engine_temperature": 122},
            "engine_temp_c",
            128.0,
        ),
        (
            "brake_temp_high",
            {"brakes_temperature": (700, 800, 1050, 900)},
            {"brakes_temperature": (700, 800, 950, 800)},
            {"brakes_temperature": (700, 800, 899, 800)},
            "brake_temp_max_c",
            1050.0,
        ),
    ],
)
def test_thermal_replay_hysteresis_and_air(rule_id, telemetry, mid, cold, field, expected):
    rules = _rules(rule_id)
    hot = _replay_snapshot(telemetry, {}, gap_ms=600)
    assert getattr(hot, field) == expected
    assert hot.gap_ahead_s == 0.6 and hot.rival_ahead_name == "Norris"
    call = _call(rules, hot, rule_id)
    assert call is not None
    assert call.inputs.get(field) == expected
    assert "Norris" in call.text and "0.6" in call.text
    if rule_id == "brake_temp_high":
        assert call.inputs.get("brake_temp_corner") == "Front left"
    assert _call(rules, _replay_snapshot(mid, {}), rule_id) is None
    assert _call(rules, hot, rule_id) is None
    assert _call(rules, _replay_snapshot(cold, {}), rule_id) is None
    call = _call(rules, _replay_snapshot(telemetry, {}, gap_ms=3000), rule_id)
    assert call is not None and "Norris" not in call.text
    assert "lift" in call.text.lower()
    assert _call(_rules(rule_id), replace(hot, _ages={}), rule_id) is None
    practice = _replay_snapshot(telemetry, {}, gap_ms=600, session_type=1)
    call = _call(_rules(rule_id), practice, rule_id)
    assert call is not None and "0.6" in call.text
    assert "air" in call.text.lower() or "tow" in call.text.lower()


@pytest.mark.parametrize(
    "component,label", [("ice", "ICE"), ("mguk", "MGU-K"), ("es", "ES"), ("ce", "CE"), ("tc", "TC")]
)
def test_wear_replay_hysteresis(component, label):
    rules = _rules("pu_wear_high")
    key = f"engine_{component}_wear"
    hot = _replay_snapshot({}, {key: 62})
    call = _call(rules, hot, "pu_wear_high")
    assert call is not None and label in call.text and "62.0" in call.text
    assert call.inputs.get("pu_wear_pct") == 62.0
    assert _call(rules, _replay_snapshot({}, {key: 58}), "pu_wear_high") is None
    assert _call(rules, hot, "pu_wear_high") is None
    assert _call(rules, _replay_snapshot({}, {key: 54}), "pu_wear_high") is None
    assert _call(rules, hot, "pu_wear_high") is not None
    assert (
        _call(_rules("pu_wear_high"), replace(hot, session_kind="practice"), "pu_wear_high") is None
    )
    assert _call(_rules("pu_wear_high"), replace(hot, _ages={}), "pu_wear_high") is None


@pytest.mark.parametrize(
    "margin,lap,rule_id",
    [(-0.8, 14, "fuel_short"), (-0.1, 14, "fuel_marginal"), (-0.1, 3, "fuel_marginal")],
)
def test_fuel_target(margin, lap, rule_id):
    snapshot = _replay_snapshot(
        {},
        {},
        lap=lap,
        model=ModelView(
            fuel_margin_laps=margin,
            fuel_per_lap_kg=2.0,
            energy_mode="over",
            energy_lap_delta_mj=0.3,
        ),
    )
    call = _call(_rules(rule_id), snapshot, rule_id)
    assert call is not None
    done = snapshot.lap_distance / snapshot.track_length_m if snapshot.track_length_m else 0.0
    expected = max(0.1, -margin * 2 / max(0.1, snapshot.laps_remaining - done))
    assert call.inputs.get("fuel_save_kg_per_lap") == pytest.approx(expected)
    assert "kg a lap" in call.text
    if rule_id == "fuel_short":
        assert "+0.3 MJ" in call.text and "lift" in call.text.lower()


def test_recording_replay_dispatches_calls(tmp_path):
    spec = RaceSpec(
        laps=3,
        dt=0.25,
        gap_ahead_s=0.6,
        fuel_kg=4.6,
        extra={
            "car_telemetry": {
                "cars": {
                    0: {"engine_temperature": 128, "brakes_temperature": (700, 800, 1050, 900)}
                }
            },
            "car_damage": {"cars": {0: {"engine_ice_wear": 62}}},
        },
    )
    recording = write_packet_stream(tmp_path / "race.f1bin", race_stream(spec))
    engine = build_engine(
        clock=VirtualClock(),
        sinks=[],
        decision_log_fp=io.StringIO(),
        overrides={"policy": {"min_gap_s": 0.0}},
    )
    assert engine.rule_engine is not None
    ids = {"engine_temp_high", "brake_temp_high", "pu_wear_high", "fuel_short"}
    engine.rule_engine.rules = [rule for rule in engine.rule_engine.rules if rule.id in ids]
    _, calls = asyncio.run(run_replay(recording, engine))
    fired = {call.rule_id: call for call in calls}
    assert ids <= fired.keys()
    assert fired["engine_temp_high"].inputs.get("engine_temp_c") == 128.0
    assert fired["brake_temp_high"].inputs.get("brake_temp_corner") == "Front left"
    assert fired["pu_wear_high"].inputs.get("pu_wear_pct") == 62.0
    assert fired["fuel_short"].inputs.get("fuel_save_kg_per_lap", 0) > 0


def test_fuel_save_counts_lap_done_and_never_zero():
    base = _replay_snapshot(
        {}, {}, lap=14, model=ModelView(fuel_margin_laps=-0.6, fuel_per_lap_kg=2.0)
    )
    half = replace(base, laps_remaining=2, lap_distance=2000.0, track_length_m=4000.0)
    assert half.fuel_save_kg_per_lap == pytest.approx(0.6 * 2.0 / 1.5)
    tiny = replace(half, fuel_margin_laps=-0.01, laps_remaining=10, lap_distance=0.0)
    assert tiny.fuel_save_kg_per_lap == pytest.approx(0.1)
    assert replace(half, fuel_margin_laps=0.2).fuel_save_kg_per_lap == 0.0
