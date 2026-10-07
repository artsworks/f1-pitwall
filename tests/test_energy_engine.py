from __future__ import annotations

import io

import pytest

from pitwall.audio.dispatcher import Call
from pitwall.clock import VirtualClock
from pitwall.engine import build_engine
from pitwall.protocol.header import PacketId

from .synth import pack_packet


def _energy_engine(total_laps: int = 8):
    engine = build_engine(
        clock=VirtualClock(),
        overrides={"policy": {"min_gap_s": 0.0, "p3_straight_only": False}},
        decision_log_fp=io.StringIO(),
        sinks=[],
    )

    def send(
        t: float,
        packet_id: int,
        data: dict[str, object],
        *,
        frame: int,
        packet_time: float | None = None,
    ) -> list[Call]:
        packet = pack_packet(
            packet_id,
            data,
            session_time=t if packet_time is None else packet_time,
            frame=frame,
        )
        engine.ingest.on_datagram(packet, t)
        return engine.tick(t)

    def race_lap(
        t: float,
        lap: int,
        sector: int,
        distance: float,
        *,
        frame: int,
        packet_time: float | None = None,
    ) -> list[Call]:
        return send(
            t,
            PacketId.LAP_DATA,
            {
                "cars": {
                    0: {
                        "current_lap_num": lap,
                        "car_position": 2,
                        "result_status": 2,
                        "driver_status": 4,
                        "sector": sector,
                        "lap_distance": distance,
                    }
                }
            },
            frame=frame,
            packet_time=packet_time,
        )

    def status(
        t: float,
        store: float,
        deployed: float,
        harvested_mguk: float,
        *,
        frame: int,
        harvested_mguh: float = 0.0,
        packet_time: float | None = None,
    ) -> list[Call]:
        return send(
            t,
            PacketId.CAR_STATUS,
            {
                "cars": {
                    0: {
                        "ers_store_energy": store,
                        "ers_deployed_this_lap": deployed,
                        "ers_harvested_this_lap_mguk": harvested_mguk,
                        "ers_harvested_this_lap_mguh": harvested_mguh,
                    }
                }
            },
            frame=frame,
            packet_time=packet_time,
        )

    send(
        0.0,
        PacketId.SESSION,
        {"session_type": 15, "track_id": 7, "total_laps": total_laps, "track_length": 5000},
        frame=1,
    )
    send(0.1, PacketId.EVENT, {"event_string_code": b"LGOT"}, frame=2)
    return engine, race_lap, status


def _at_lap3(start_frame: int = 100):
    engine, race_lap, status = _energy_engine()
    status(0.2, 4_000_000.0, 0.0, 0.0, frame=start_frame - 3)
    race_lap(0.3, 2, 0, 0.0, frame=start_frame - 2)
    race_lap(0.4, 3, 0, 0.0, frame=start_frame - 1)
    return engine, race_lap, status


def test_ers_lap_attribution_waits_for_status_after_lap_data() -> None:
    engine, race_lap, status = _at_lap3()
    status(0.5, 2_200_000.0, 1_800_000.0, 0.0, frame=100)

    race_lap(0.6, 4, 0, 0.0, frame=101)
    assert engine.energy_budget is not None
    assert engine.energy_budget.deployed_this_lap_j == 0.0
    assert engine.state.snapshot(0.6).energy_prev_lap_mode == ""

    status(0.7, 2_200_000.0, 300_000.0, 0.0, frame=101)
    assert engine.energy_prev_lap is None
    status(1.11, 2_200_000.0, 300_000.0, 0.0, frame=102)

    assert engine.energy_prev_lap is not None
    assert engine.energy_prev_lap.deployed_this_lap_j == pytest.approx(1_800_000.0)
    assert engine.energy_budget is not None
    assert engine.energy_budget.deployed_this_lap_j == pytest.approx(300_000.0)


def test_ers_lap_attribution_waits_until_settle_window() -> None:
    engine, race_lap, status = _at_lap3()
    status(0.5, 2_200_000.0, 1_800_000.0, 0.0, frame=100)
    race_lap(0.6, 4, 0, 0.0, frame=101)

    status(0.9, 2_200_000.0, 300_000.0, 0.0, frame=102)
    assert engine.energy_prev_lap is None

    status(1.11, 2_200_000.0, 300_000.0, 0.0, frame=103)
    assert engine.energy_prev_lap is not None
    assert engine.energy_prev_lap.deployed_this_lap_j == pytest.approx(1_800_000.0)


def test_ers_late_pre_boundary_status_does_not_replace_live_counters() -> None:
    engine, race_lap, status = _at_lap3()
    status(0.5, 2_200_000.0, 0.0, 0.0, frame=99)
    race_lap(0.6, 4, 0, 0.0, frame=101)
    status(0.8, 2_200_000.0, 0.0, 0.0, frame=102)
    status(0.9, 2_200_000.0, 1_800_000.0, 0.0, frame=100, packet_time=0.75)

    assert engine.energy_budget is not None
    assert engine.energy_budget.deployed_this_lap_j == 0.0
    assert engine.energy_prev_lap is None

    status(1.11, 2_200_000.0, 300_000.0, 0.0, frame=103)

    assert engine.energy_prev_lap is not None
    assert engine.energy_prev_lap.deployed_this_lap_j == pytest.approx(1_800_000.0)
    assert engine.energy_budget is not None
    assert engine.energy_budget.deployed_this_lap_j == pytest.approx(300_000.0)


def test_ers_history_keeps_pre_boundary_status_after_delayed_lap_data() -> None:
    engine, race_lap, status = _at_lap3()
    status(0.5, 2_200_000.0, 1_800_000.0, 0.0, frame=100)
    for frame in range(101, 111):
        session_time = 0.55 + (frame - 101) * 0.05
        status(session_time, 2_200_000.0, 100_000.0, 0.0, frame=frame)

    race_lap(1.01, 4, 0, 0.0, frame=101, packet_time=0.6)
    assert engine.energy_prev_lap is None
    status(1.11, 2_200_000.0, 200_000.0, 0.0, frame=111)

    assert engine.energy_prev_lap is not None
    assert engine.energy_prev_lap.deployed_this_lap_j == pytest.approx(1_800_000.0)
    assert engine.energy_budget is not None
    assert engine.energy_budget.deployed_this_lap_j == pytest.approx(200_000.0)


def test_ers_lap_attribution_handles_status_before_lap_data() -> None:
    engine, race_lap, status = _at_lap3()
    status(0.5, 2_200_000.0, 1_800_000.0, 0.0, frame=100)
    status(0.6, 2_200_000.0, 200_000.0, 0.0, frame=101)

    race_lap(0.61, 4, 0, 0.0, frame=101)
    status(1.12, 2_200_000.0, 200_000.0, 0.0, frame=102)

    assert engine.energy_prev_lap is not None
    assert engine.energy_prev_lap.deployed_this_lap_j == pytest.approx(1_800_000.0)
    assert engine.energy_budget is not None
    assert engine.energy_budget.deployed_this_lap_j == pytest.approx(200_000.0)


def test_ers_attribution_uses_prior_lap_when_new_counter_is_higher() -> None:
    engine, race_lap, status = _at_lap3()
    status(0.5, 3_600_000.0, 400_000.0, 0.0, frame=100)
    race_lap(0.6, 4, 0, 0.0, frame=101)
    status(0.7, 3_100_000.0, 500_000.0, 0.0, frame=105)
    status(1.11, 3_100_000.0, 500_000.0, 0.0, frame=106)

    assert engine.energy_prev_lap is not None
    assert engine.energy_prev_lap.deployed_this_lap_j == pytest.approx(400_000.0)
    assert engine.energy_budget is not None
    assert engine.energy_budget.deployed_this_lap_j == pytest.approx(500_000.0)


def test_ers_attribution_waits_for_late_pre_boundary_status() -> None:
    engine, race_lap, status = _at_lap3(start_frame=50)
    status(0.5, 4_000_000.0, 0.0, 0.0, frame=50)
    race_lap(0.6, 4, 0, 0.0, frame=101)
    status(0.7, 3_500_000.0, 500_000.0, 0.0, frame=100)
    assert engine.state.snapshot(0.7).energy_prev_lap_mode == ""

    status(0.8, 3_500_000.0, 0.0, 0.0, frame=101)
    assert engine.energy_prev_lap is None
    status(1.11, 3_500_000.0, 0.0, 0.0, frame=102)

    assert engine.energy_prev_lap is not None
    assert engine.energy_prev_lap.deployed_this_lap_j == pytest.approx(500_000.0)
    assert engine.energy_prev_lap.mode != "under"


def test_ers_attribution_grades_a_genuine_zero_use_lap() -> None:
    engine, race_lap, status = _at_lap3()
    status(0.5, 4_000_000.0, 0.0, 0.0, frame=100)
    race_lap(0.6, 4, 0, 0.0, frame=101)
    assert engine.state.snapshot(0.6).energy_prev_lap_mode == ""

    status(0.7, 4_000_000.0, 0.0, 0.0, frame=101)
    assert engine.energy_prev_lap is None
    status(1.11, 4_000_000.0, 0.0, 0.0, frame=102)

    assert engine.energy_prev_lap is not None
    assert engine.energy_prev_lap.deployed_this_lap_j == 0.0


def test_energy_budget_uses_full_lap_counters_and_reports_under_lap() -> None:
    engine, race_lap, status = _energy_engine(total_laps=6)
    status(0.2, 3_000_000.0, 0.0, 0.0, frame=3)
    race_lap(0.2, 1, 0, 0.0, frame=4)

    status(10.0, 1_500_000.0, 100_000.0, 500_000.0, frame=5)
    race_lap(10.0, 1, 1, 2_000.0, frame=6)
    status(20.0, 500_000.0, 910_000.0, 500_000.0, frame=7)
    race_lap(20.0, 1, 2, 4_000.0, frame=8)
    assert engine.state.snapshot(20.0).energy_mode != "under"

    status(29.0, 100_000.0, 1_800_000.0, 500_000.0, frame=9)
    race_lap(29.0, 1, 2, 4_900.0, frame=10)
    status(29.5, 100_000.0, 0.0, 0.0, frame=11)
    race_lap(30.0, 2, 0, 0.0, frame=11)
    status(30.6, 100_000.0, 0.0, 0.0, frame=12)
    assert engine.state.snapshot(30.6).energy_prev_lap_mode == "over"

    status(50.0, 100_000.0, 0.0, 0.0, frame=13)
    race_lap(50.0, 2, 2, 4_900.0, frame=14)
    status(59.5, 400_000.0, 0.0, 0.0, frame=15)
    race_lap(60.0, 3, 0, 0.0, frame=15)

    status(80.0, 400_000.0, 0.0, 300_000.0, frame=16)
    race_lap(80.0, 3, 2, 4_900.0, frame=17)
    status(89.9, 700_000.0, 0.0, 300_000.0, frame=17)
    status(89.95, 700_000.0, 0.0, 0.0, frame=18)
    race_lap(90.0, 4, 0, 0.0, frame=18)
    calls = status(90.6, 700_000.0, 0.0, 0.0, frame=19)

    snapshot = engine.state.snapshot(90.6)
    assert snapshot.energy_prev_lap_mode == "under"
    assert snapshot.energy_prev_under_mj > 0.2
    assert any(call.rule_id == "energy_under" for call in calls)


def test_energy_tracker_resets_after_same_lap_flashback() -> None:
    engine, race_lap, status = _energy_engine()
    status(0.2, 2_000_000.0, 0.0, 0.0, frame=47)
    race_lap(0.3, 5, 0, 0.0, frame=48)
    status(10.0, 2_000_000.0, 2_000_000.0, 0.0, frame=50)
    race_lap(10.0, 5, 1, 2_000.0, frame=51)
    assert engine.energy_budget is not None
    assert engine.energy_budget.deployed_this_lap_j == pytest.approx(2_000_000.0)

    status(11.0, 2_000_000.0, 500_000.0, 0.0, frame=52, packet_time=5.0)
    assert engine.state.rewinds == 1
    assert engine.energy_budget is not None
    assert engine.energy_budget.deployed_this_lap_j == pytest.approx(500_000.0)

    race_lap(12.0, 6, 0, 0.0, frame=53, packet_time=6.0)
    assert engine.state.snapshot(12.0).energy_prev_lap_mode == ""
