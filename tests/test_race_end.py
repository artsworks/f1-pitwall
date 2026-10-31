"""Replay format-2026 datagrams through the state and declarative rules."""

from __future__ import annotations

import asyncio
import hashlib
import io
import struct
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from pitwall.audio.dispatcher import Dispatcher
from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.engine import build_engine, run_replay
from pitwall.ingest import Ingest
from pitwall.learning.bench import evaluate_checks, parse_scenario
from pitwall.protocol.header import PacketId
from pitwall.protocol.packets import parse
from pitwall.rules.engine import RuleEngine
from pitwall.state.session import SessionState

from .synth import pack_packet, write_packet_stream

pytestmark = pytest.mark.replay

FORMATION = {"formation_tyres_cold", "formation_brakes_cold", "formation_grid_slot"}
FINISH = {
    "finish_gained",
    "finish_lost",
    "finish_held",
    "finish_win",
    "finish_podium",
    "finish_penalty",
}
NEW = FORMATION | {"finish_classified", "race_cool_down"}


def _engine(ids=NEW | FINISH | {"last_lap", "last_lap_attack", "last_lap_defend"}):
    settings = ConfigStore(isolated=True).current()
    return RuleEngine(
        [r for r in settings.rules if r.id in ids],
        thresholds=settings.thresholds,
        mode=settings.resolved_mindset(),
        staleness_s=settings.engine.staleness_s,
    )


def _event(code, session_time, detail=b""):
    return pack_packet(
        PacketId.EVENT,
        {"event_string_code": code, "event_data": detail.ljust(12, b"\0")},
        session_time=session_time,
    )


def _setup():
    ingest = Ingest()
    state = SessionState()
    state.register(ingest)
    return ingest, state


def _frame(
    ingest,
    t,
    *,
    lap=0,
    sector=0,
    result=2,
    position=4,
    safety=3,
    formation=1,
    speed=100,
    temp=60,
    brake=150,
    **session,
):
    packets = [
        pack_packet(
            PacketId.SESSION,
            dict(
                session_type=15,
                formula=0,
                formation_lap=formation,
                safety_car_status=safety,
                total_laps=3,
                track_id=16,
                track_length=4309,
                **session,
            ),
            session_time=t,
        ),
        pack_packet(PacketId.PARTICIPANTS, {"num_active_cars": 5}, session_time=t),
        pack_packet(
            PacketId.LAP_DATA,
            {
                "cars": {
                    0: dict(
                        current_lap_num=lap,
                        sector=sector,
                        result_status=result,
                        driver_status=4,
                        car_position=position,
                        grid_position=6,
                        delta_to_car_in_front_ms_part=1200,
                    ),
                    1: dict(
                        current_lap_num=lap,
                        car_position=position - 1,
                        result_status=3 if result == 3 else 2,
                    ),
                    2: dict(
                        current_lap_num=lap,
                        car_position=position + 1,
                        result_status=2,
                        delta_to_car_in_front_ms_part=2400,
                    ),
                }
            },
            session_time=t,
        ),
        pack_packet(
            PacketId.CAR_TELEMETRY,
            {
                "cars": {
                    0: dict(
                        speed=speed,
                        tyres_inner_temperature=(temp,) * 4,
                        brakes_temperature=(brake,) * 4,
                    )
                }
            },
            session_time=t,
        ),
    ]
    for packet in packets:
        ingest.on_datagram(packet, t)


def _final(t=30, **player):
    cars = {
        0: dict(
            position=3,
            grid_position=6,
            num_laps=3,
            result_status=3,
            total_race_time=1000.0,
            best_lap_time_ms=80200,
        ),
        1: dict(
            position=2, num_laps=3, result_status=3, total_race_time=998.8, best_lap_time_ms=82000
        ),
        2: dict(
            position=4, num_laps=3, result_status=3, total_race_time=1002.4, best_lap_time_ms=83000
        ),
        3: dict(
            position=1, num_laps=3, result_status=3, total_race_time=990.0, best_lap_time_ms=81000
        ),
        4: dict(
            position=5, num_laps=3, result_status=3, total_race_time=1009.0, best_lap_time_ms=84000
        ),
    }
    cars[0].update(player)
    return pack_packet(PacketId.FINAL_CLASSIFICATION, {"num_cars": 5, "cars": cars}, session_time=t)


def _calls(engine, snap):
    result = engine.evaluate(snap)
    assert not any(s.reason.endswith("error") for s in result.suppressed)
    return {c.rule.id: c.text for c in result.candidates}


def test_formation_cadence_and_no_repeat():
    ingest, state = _setup()
    engine = _engine(FORMATION)
    _frame(ingest, 1)
    calls = _calls(engine, state.snapshot(1))
    assert set(calls) == {"formation_tyres_cold"}
    assert "60.0" in calls["formation_tyres_cold"]
    _frame(ingest, 2, sector=1)
    calls = _calls(engine, state.snapshot(2))
    assert set(calls) == {"formation_brakes_cold"}
    assert "150.0" in calls["formation_brakes_cold"]
    _frame(ingest, 3, sector=2)
    calls = _calls(engine, state.snapshot(3))
    assert set(calls) == {"formation_grid_slot"}
    assert "P6.0" in calls["formation_grid_slot"]
    _frame(ingest, 4, sector=2)
    assert not _calls(engine, state.snapshot(4))


@pytest.mark.parametrize(
    "updates",
    [
        {"safety": 0},
        {"safety": 1},
        {"safety": 2},
        {"formation": 0},
        {"speed": 0},
        {"formula": 1},
        {"session_type": 5},
    ],
)
def test_no_formation_guess(updates):
    ingest, state = _setup()
    # Session fields override the default frame rather than duplicate keywords.
    frame = dict(updates)
    for key in ("formula", "session_type"):
        frame.pop(key, None)
    _frame(ingest, 1, sector=2, **frame)
    snap = state.snapshot(1)
    if "formula" in updates:
        snap = replace(snap, formula=updates["formula"])
    if "session_type" in updates:
        snap = replace(snap, session_type=5, session_kind="quali")
    assert not _calls(_engine(FORMATION), snap)


def test_formation_hot_zero_stale_and_lights():
    ingest, state = _setup()
    _frame(ingest, 1, temp=100, brake=500, sector=1)
    assert not _calls(_engine(FORMATION), state.snapshot(1))
    _frame(ingest, 2, temp=0, brake=0, sector=1)
    assert not _calls(_engine(FORMATION), state.snapshot(2))
    _frame(ingest, 3, sector=2)
    snap = state.snapshot(3)
    assert not _calls(_engine(FORMATION), replace(snap, _ages={"car_telemetry": 100}))
    candidate = _engine({"formation_grid_slot"}).evaluate(snap).candidates[0]
    ingest.on_datagram(_event("STLG", detail=b"\x05", session_time=3.1), 3.1)
    assert state.snapshot(3.1).start_lights == 5
    assert not _calls(_engine(FORMATION), state.snapshot(3.1))
    assert not candidate.still_true(state.snapshot(3.1))
    ingest.on_datagram(_event("LGOT", session_time=3.2), 3.2)
    assert not _calls(_engine(FORMATION), state.snapshot(3.2))


def test_final_lap_chqf_and_player_finish():
    ingest, state = _setup()
    engine = _engine()
    ingest.on_datagram(_event("LGOT", session_time=1), 1)
    _frame(ingest, 2, lap=3, safety=0, formation=0)
    calls = _calls(engine, state.snapshot(2))
    assert "last_lap" in calls
    ingest.on_datagram(_event("CHQF", session_time=3), 3)
    assert not FINISH & _calls(engine, state.snapshot(3)).keys()
    _frame(ingest, 4, lap=3, safety=0, formation=0, result=3)
    calls = _calls(engine, state.snapshot(4))
    assert "finish_gained" in calls
    assert "1.2 to the car ahead" in calls["finish_gained"]
    assert "2.4 clear behind" in calls["finish_gained"]
    _frame(ingest, 5, lap=3, safety=0, formation=0, result=3)
    assert not FINISH & _calls(engine, state.snapshot(5)).keys()


@pytest.mark.parametrize("status", [4, 5, 6, 7])
def test_no_finish_or_cool_down_for_non_finisher(status):
    ingest, state = _setup()
    _frame(ingest, 1, lap=3, safety=0, result=status)
    snap = replace(state.snapshot(1), phase="finished")
    assert not _calls(_engine(), snap).keys() & (FINISH | {"race_cool_down"})
    ingest.on_datagram(_final(result_status=status), 30)
    assert state.snapshot(30).final_position == 0


def test_final_classification_wire_result_gaps_and_fastest():
    packet = _final()
    assert len(packet) == 1134
    assert struct.unpack_from("<I", packet, 37)[0] == 80200
    assert struct.unpack_from("<d", packet, 41)[0] == 1000.0
    decoded = parse(PacketId.FINAL_CLASSIFICATION, packet)
    assert decoded.cars[0].result_reason == 0
    assert decoded.cars[0].best_lap_time_ms == 80200
    ingest, state = _setup()
    _frame(ingest, 20, lap=3, safety=0, result=3)
    ingest.on_datagram(packet, 30)
    engine = _engine()
    calls = _calls(engine, state.snapshot(30))
    assert set(calls) == {"finish_classified"}
    text = calls["finish_classified"]
    for fragment in (
        "P3.0",
        "Up 3.0",
        "1.2 to the car ahead",
        "2.4 clear behind",
        "Fastest lap, 80.2 seconds",
    ):
        assert fragment in text
    ingest.on_datagram(_final(t=31), 31)
    assert not _calls(engine, state.snapshot(31))


@pytest.mark.parametrize(
    ("rule_id", "position", "updates"),
    [
        ("finish_penalty", 4, {"penalty_s": 3, "penalty_position": 5}),
        ("finish_gained", 4, {}),
        ("finish_lost", 4, {"grid_position": 2}),
        ("finish_held", 4, {"grid_position": 4}),
        ("finish_win", 1, {}),
        ("finish_podium", 2, {}),
    ],
)
def test_classification_replaces_road_result_waiting_for_radio(rule_id, position, updates):
    ingest, state = _setup()
    rules = _engine({rule_id, "finish_classified", "last_lap"})
    policy = ConfigStore(isolated=True).current().policy.model_copy(update={"min_gap_s": 0.0})
    dispatcher = Dispatcher(policy, VirtualClock(), sinks=[])
    _frame(ingest, 0, lap=3, safety=0, formation=0, position=position)
    ingest.on_datagram(_event("LGOT", 0), 0)
    snap = state.snapshot(0)
    dispatcher.submit(rules.evaluate(snap).candidates, snap)
    assert [c.rule_id for c in dispatcher.drain(0)] == ["last_lap"]
    speech_end = dispatcher._spoken_calls[-1][1]

    _frame(ingest, 0.5, lap=3, safety=0, formation=0, result=3, position=position)
    road = replace(state.snapshot(0.5), **updates)
    candidates = rules.evaluate(road).candidates
    assert [c.rule.id for c in candidates] == [rule_id]
    dispatcher.submit(candidates, road)
    assert dispatcher.drain(0.5) == []
    assert dispatcher.queue_status(0.5)[0] == 1

    ingest.on_datagram(_final(t=0.75), 0.75)
    classified = state.snapshot(0.75)
    assert classified.final_position != road.position
    dispatcher.submit(rules.evaluate(classified).candidates, classified)
    assert dispatcher.drain(0.75) == []
    assert dispatcher.queue_status(0.75)[0] == 1
    assert [c.rule_id for c in dispatcher.drain(speech_end)] == ["finish_classified"]
    assert dispatcher.queue_status(speech_end)[0] == 0


def test_classification_gaps_omit_lapped_cars_and_include_penalties():
    ingest, state = _setup()
    ingest.on_datagram(_final(num_laps=2), 30)
    assert "car ahead" not in state.snapshot(30).final_details
    assert "behind" not in state.snapshot(30).final_details
    ingest.on_datagram(_final(penalties_time=1), 30)
    details = state.snapshot(30).final_details
    assert "2.2 to the car ahead" in details
    assert "1.4 clear behind" in details
    ingest.on_datagram(_final(best_lap_time_ms=82000, grid_position=2), 30)
    details = state.snapshot(30).final_details
    assert "Fastest lap" not in details
    assert "Down 1.0" in details


def test_cool_down_only_after_player_finish_while_moving():
    ingest, state = _setup()
    engine = _engine({"race_cool_down"})
    _frame(ingest, 1, lap=3, safety=0, result=3)
    assert not _calls(engine, state.snapshot(1))
    _frame(ingest, 10, lap=3, safety=0, result=3, brake=450)
    snap = state.snapshot(10)
    assert not _calls(_engine({"race_cool_down"}), replace(snap, speed_kmh=0))
    assert not _calls(_engine({"race_cool_down"}), replace(snap, session_ended=True))
    assert "450.0" in _calls(engine, snap)["race_cool_down"]
    assert not _calls(engine, state.snapshot(10.1))
    ingest.on_datagram(_final(), 30)
    assert not _calls(_engine({"race_cool_down"}), state.snapshot(30))


def test_finish_state_clears_on_rewind_and_new_session():
    ingest, state = _setup()
    _frame(ingest, 20, lap=3, safety=0, result=3)
    ingest.on_datagram(_final(), 30)
    _frame(ingest, 1, lap=0)
    assert state.snapshot(1).final_position == 0
    assert state.snapshot(1).finish_details == ""
    assert state.snapshot(1).finish_elapsed_s == 0
    ingest.on_datagram(_final(t=2), 2)
    ingest.on_datagram(
        pack_packet(PacketId.SESSION, {"session_type": 15}, session_uid=0xDEAD1234, session_time=0),
        3,
    )
    assert state.snapshot(3).final_position == 0


def test_fastest_event_does_not_end_race():
    ingest, state = _setup()
    _frame(ingest, 1, lap=2, safety=0)
    ingest.on_datagram(_event("FTLP", detail=struct.pack("<Bf", 0, 80.2), session_time=2), 2)
    assert state.snapshot(2).fastest_lap_mine
    assert not _calls(_engine(NEW | FINISH), state.snapshot(2))


@pytest.mark.parametrize(
    ("ahead", "behind", "expected"),
    [(2.0, 2.0, "last_lap"), (0.5, 2.0, "last_lap_attack"), (0.5, 0.5, "last_lap_defend")],
)
def test_existing_final_lap_family_remains_exclusive(ahead, behind, expected):
    ingest, state = _setup()
    ingest.on_datagram(_event("LGOT", session_time=1), 1)
    _frame(ingest, 2, lap=3, safety=0)
    snap = replace(state.snapshot(2), gap_ahead_s=ahead, gap_behind_s=behind)
    family = {"last_lap", "last_lap_attack", "last_lap_defend"}
    assert set(_calls(_engine(family), snap)) == {expected}


def race_end_stream():
    """Source for brazil-race-end.yaml, with a fixed header in the writer below."""
    packets = []

    class Collect:
        def on_datagram(self, packet, t):
            packets.append((t, packet))

    collector = Collect()
    for step in range(1321):
        t = step / 4
        for event_t, code in ((30, "LGOT"), (290, "CHQF")):
            if t == event_t:
                packets.append((t, _event(code, t)))
        formation = t < 30
        lap = 1 if t < 120 else 2 if t < 210 else 3
        sector = int(t // 10) if formation else 2 if t >= 300 else 0
        _frame(
            collector,
            t,
            lap=lap,
            sector=sector,
            safety=3 if formation else 0,
            formation=int(formation),
            result=3 if t >= 300 else 2,
            brake=450 if t >= 300 else 150,
        )
        if t in (320, 325, 330):
            packets.append((t, _final(t=t)))
    return packets


def write_race_end_recording(path):
    with patch("pitwall.net.recording.time.time_ns", return_value=0):
        return write_packet_stream(path, race_end_stream())


def test_scenario_replay_is_deterministic_and_matches_manifest(tmp_path):
    recording = write_race_end_recording(tmp_path / "brazil-race-end.f1bin")
    scenario = parse_scenario(Path("scenarios/brazil-race-end.yaml"))
    assert hashlib.sha256(recording.read_bytes()).hexdigest() == scenario.source_sha256
    runs = []
    for speed in (None, 10.0):
        calls = _replay_scenario(recording, speed)
        checks = evaluate_checks(scenario.checks, [(c.rule_id, c.lap) for c in calls])
        assert all(c["ok"] for c in checks), checks
        runs.append([(c.rule_id, c.text, c.lap, c.t) for c in calls])
    assert runs[0] == runs[1]


def _replay_scenario(recording, speed):
    engine = build_engine(
        clock=VirtualClock(), isolated=True, decision_log_fp=io.StringIO(), sinks=[]
    )
    _, calls = asyncio.run(run_replay(recording, engine, speed))
    return calls
