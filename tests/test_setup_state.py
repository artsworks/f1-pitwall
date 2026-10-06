from __future__ import annotations

import asyncio
from pathlib import Path

from pitwall.clock import VirtualClock
from pitwall.engine import build_engine, run_replay
from pitwall.protocol.header import PacketId
from pitwall.setup.states import majority_state, runs_for_session, setup_hash
from pitwall.store.db import Database, LapRow

from .synth import pack_packet, write_packet_stream


def test_setup_hash_rounding_exclusion_and_changes() -> None:
    original = {"brake_bias": 56.0, "rear_pressure": 22.1, "fuel_load": 45.0}
    noisy = {"brake_bias": 56.0001, "rear_pressure": 22.1001, "fuel_load": 65.0}
    changed = {"brake_bias": 55.0, "rear_pressure": 22.1, "fuel_load": 45.0}
    assert setup_hash(original) == setup_hash(noisy)
    assert setup_hash(original) != setup_hash(changed)
    assert setup_hash({}) == ""
    assert setup_hash({"fuel_load": 0.0, "brake_bias": 0}) == ""


def test_majority_state_uses_later_lap_for_ties() -> None:
    rows = [
        LapRow(
            id=lap_num,
            session_uid=1,
            car_idx=0,
            lap_num=lap_num,
            lap_time_ms=90_000,
            s1_ms=0,
            s2_ms=0,
            compound=17,
            tyre_age_laps=lap_num,
            fuel_remaining_laps=0.0,
            valid=1,
            invalid_reasons=(),
            wear_pct=0.0,
            fuel_kg=0.0,
            ers_deployed_j=0.0,
            sc_status=0,
            weather=0,
            setup_state_id=state_id,
        )
        for lap_num, state_id in enumerate((4, 7, 4), start=1)
    ]
    assert majority_state(rows) == 4


def _practice_setup_stream() -> list[tuple[float, bytes]]:
    packets: list[tuple[float, bytes]] = []
    t = 0.0

    def emit(packet_id: int, data: dict) -> None:
        nonlocal t
        packets.append((t, pack_packet(packet_id, data, session_time=t)))
        t += 0.033

    emit(
        PacketId.SESSION,
        {
            "session_type": 1,
            "track_id": 7,
            "track_length": 5000,
            "parc_ferme_rules": 2,
        },
    )
    emit(
        PacketId.CAR_SETUPS,
        {
            "cars": {0: {"brake_bias": 56, "front_wing": 12, "fuel_load": 45.0}},
            "next_front_wing_value": 13.0,
        },
    )

    for lap_num in range(1, 8):
        emit(
            PacketId.CAR_STATUS,
            {
                "cars": {
                    0: {
                        "actual_tyre_compound": 18,
                        "visual_tyre_compound": 18,
                        "tyres_age_laps": lap_num - 1,
                        "fuel_in_tank": 40.0,
                    }
                }
            },
        )
        emit(
            PacketId.LAP_DATA,
            {
                "cars": {
                    0: {
                        "current_lap_num": lap_num,
                        "last_lap_time_ms": 91_000,
                        "driver_status": 4,
                        "result_status": 2,
                    }
                }
            },
        )
        if lap_num == 4:
            emit(
                PacketId.CAR_SETUPS,
                {
                    "cars": {0: {"brake_bias": 55, "front_wing": 12, "fuel_load": 38.0}},
                    "next_front_wing_value": 14.0,
                },
            )

        spinning = lap_num in {2, 5}
        motion_frames = 10 if spinning else 16
        for frame in range(motion_frames + (6 if spinning else 0)):
            active_spin = spinning and frame < motion_frames
            throttle = 0.9 if active_spin else 0.5
            emit(PacketId.CAR_TELEMETRY, {"cars": {0: {"speed": 120, "throttle": throttle}}})
            emit(
                PacketId.MOTION_EX,
                {
                    "wheel_slip_ratio": (0.2, 0.2, 0.0, 0.0)
                    if active_spin
                    else (0.0, 0.0, 0.0, 0.0),
                    "wheel_slip_angle": (0.02, 0.02, 0.1, 0.1),
                    "local_velocity": (0.0, 0.0, 30.0),
                    "angular_velocity": (0.0, 0.7, 0.0),
                },
            )
    return packets


def test_practice_replay_persists_laps_and_setup_runs(tmp_path: Path) -> None:
    recording = write_packet_stream(tmp_path / "practice.f1bin", _practice_setup_stream())
    db = Database(":memory:")
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    asyncio.run(run_replay(recording, engine, None))

    uid = engine.state.session_uid
    assert uid is not None
    laps = db.laps_for(uid)
    assert laps
    assert any(row.traction_exits > 0 for row in laps if row.lap_num in {2, 5})
    assert any(row.slip_balance_deg > 0 for row in laps)
    changes = db.setup_changes_for_session(uid)
    assert len(changes) == 2
    assert db.session_row(uid)["parc_ferme"] == 2

    runs = runs_for_session(db, uid)
    assert len(runs) == 2
    assert runs[0].setup_state_id != runs[1].setup_state_id
