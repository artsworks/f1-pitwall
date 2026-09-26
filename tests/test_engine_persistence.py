"""M3 persistence hooks: synthetic race stream -> laps/stints/pit_events/
model_params rows, rival laps from Session History."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pitwall.clock import VirtualClock
from pitwall.engine import build_engine, run_replay
from pitwall.protocol.header import PacketId
from pitwall.store.db import Database

from .synth import pack_packet, write_packet_stream

BASE_MS = 90_000
SLOPE = 100.0


def _race_stream() -> list[tuple[float, bytes]]:
    """10 player laps on a 5 km race (session_type 15, track 7). Lap 8 pits
    (slow in-lap), lap 9 is the out-lap flagged after_in_lap. Rival session
    history for car 1 with two completed laps."""
    pkts: list[tuple[float, bytes]] = []
    t = 0.0

    def emit(pid: int, data: dict) -> None:
        nonlocal t
        pkts.append((t, pack_packet(pid, data, session_time=t)))
        t += 0.033

    emit(
        PacketId.SESSION,
        {"session_type": 15, "track_id": 7, "total_laps": 12, "track_length": 5000},
    )

    def lap_time(finished_lap: int) -> int:
        if finished_lap == 8:
            return BASE_MS + 15_000  # in-lap
        if finished_lap == 9:
            return BASE_MS + 8_000  # out-lap
        return int(BASE_MS + SLOPE * finished_lap)

    for lap in range(1, 11):
        driver_status = 2 if lap == 9 else 4  # lap 9 starts as IN_LAP -> after_in_lap
        pit_status = 1 if lap == 8 else 0
        for _ in range(3):
            emit(
                PacketId.CAR_STATUS,
                {
                    "cars": {
                        0: {
                            "fuel_in_tank": 110.0 - 1.7 * lap,
                            "fuel_remaining_laps": 30.0 - lap,
                            "actual_tyre_compound": 18,
                            "visual_tyre_compound": 18,
                            "tyres_age_laps": lap - 1,
                            "ers_store_energy": 3_000_000.0,
                            "ers_deployed_this_lap": 250_000.0,
                            "ers_harvested_this_lap_mguk": 100_000.0,
                            "ers_harvested_this_lap_mguh": 50_000.0,
                        }
                    }
                },
            )
            emit(
                PacketId.CAR_DAMAGE,
                {"cars": {0: {"tyres_wear": (10.0 + lap, 10.0 + lap, 10.0 + lap, 10.0 + lap)}}},
            )
            emit(
                PacketId.LAP_DATA,
                {
                    "cars": {
                        0: {
                            "current_lap_num": lap,
                            "last_lap_time_ms": lap_time(lap - 1) if lap > 1 else 0,
                            "driver_status": driver_status,
                            "pit_status": pit_status,
                            "result_status": 2,
                            "pit_lane_time_in_lane_ms": 19_500 if lap == 9 else 0,
                        }
                    }
                },
            )
        # rival session history after lap 3
        if lap == 3:
            emit(
                PacketId.SESSION_HISTORY,
                {
                    "car_idx": 1,
                    "num_laps": 3,
                    "num_tyre_stints": 1,
                    "laps": {
                        0: {
                            "lap_time_ms": 91_500,
                            "lap_valid_bit_flags": 0x01,
                            "sector1_ms_part": 30_100,
                            "sector2_ms_part": 30_200,
                        },
                        1: {"lap_time_ms": 91_800, "lap_valid_bit_flags": 0x01},
                    },
                    "tyre_stints": {
                        0: {"end_lap": 10, "tyre_actual_compound": 17, "tyre_visual_compound": 17}
                    },
                },
            )
    return pkts


def _run(tmp_path: Path) -> tuple[VirtualClock, object, Database]:
    rec = write_packet_stream(tmp_path / "race.f1bin", _race_stream())
    db = Database(":memory:")
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    asyncio.run(run_replay(rec, engine, None))
    return engine, engine.state, db


def test_player_laps_carry_wear_fuel_ers(tmp_path: Path) -> None:
    engine, state, db = _run(tmp_path)
    uid = state.session_uid
    assert uid is not None
    laps = db.laps_for(uid, 0)
    assert len(laps) >= 9
    lap2 = next(r for r in laps if r.lap_num == 2)
    assert lap2.wear_pct > 0.0
    import pytest

    assert lap2.fuel_kg == pytest.approx(110.0 - 1.7 * 3, abs=0.01)  # lap-3 tick value
    assert lap2.ers_deployed_j == 250_000.0


def test_stint_refit_writes_stints_row(tmp_path: Path) -> None:
    engine, state, db = _run(tmp_path)
    uid = state.session_uid
    assert uid is not None
    stints = db.stints_for_track(7, 18)
    assert stints, "expected a stints row after enough valid laps"
    assert abs(stints[0].deg_ms_per_lap - SLOPE) < 30.0
    assert engine.deg_fit is not None and engine.deg_fit.n >= 5


def test_pit_sequence_persists_event_and_param(tmp_path: Path) -> None:
    engine, state, db = _run(tmp_path)
    uid = state.session_uid
    assert uid is not None
    events = db.pit_events_for_session(uid)
    assert len(events) == 1
    ev = events[0]
    assert ev.lap_num == 8  # the in-lap
    assert ev.ref_pace_ms > 0
    # loss = (in - ref) + (out - ref) = 15000 + 8000 (minus slope drift)
    assert 20_000 < ev.loss_ms < 26_000
    p = db.get_param(7, 0, "pit_loss_green_ms")
    assert p is not None and p.value == float(ev.loss_ms)


def test_fuel_kg_per_lap_folded(tmp_path: Path) -> None:
    engine, state, db = _run(tmp_path)
    p = db.get_param(7, 0, "fuel_kg_per_lap")
    assert p is not None
    assert abs(p.value - 1.7) < 0.01


def test_rival_laps_persisted(tmp_path: Path) -> None:
    engine, state, db = _run(tmp_path)
    uid = state.session_uid
    assert uid is not None
    rival = db.laps_for(uid, 1)
    assert [r.lap_num for r in rival] == [1, 2]
    assert rival[0].lap_time_ms == 91_500 and rival[0].compound == 17
