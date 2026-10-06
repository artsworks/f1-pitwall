from __future__ import annotations

import io

from pitwall.audio.dispatcher import Call
from pitwall.clock import VirtualClock
from pitwall.engine import build_engine
from pitwall.protocol.header import PacketId

from .synth import pack_packet


def test_energy_budget_uses_full_lap_counters_and_reports_under_lap() -> None:
    engine = build_engine(
        clock=VirtualClock(),
        overrides={"policy": {"min_gap_s": 0.0, "p3_straight_only": False}},
        decision_log_fp=io.StringIO(),
        sinks=[],
    )

    def send(t: float, packet_id: int, data: dict[str, object]) -> list[Call]:
        packet = pack_packet(packet_id, data, session_time=t)
        engine.ingest.on_datagram(packet, t)
        return engine.tick(t)

    def race_lap(t: float, lap: int, sector: int, distance: float) -> list[Call]:
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
        )

    def status(t: float, store: float, deployed: float, harvested: float) -> list[Call]:
        return send(
            t,
            PacketId.CAR_STATUS,
            {
                "cars": {
                    0: {
                        "ers_store_energy": store,
                        "ers_deployed_this_lap": deployed,
                        "ers_harvested_this_lap_mguk": harvested,
                    }
                }
            },
        )

    send(
        0.0,
        PacketId.SESSION,
        {"session_type": 15, "track_id": 7, "total_laps": 6, "track_length": 5000},
    )
    send(0.1, PacketId.EVENT, {"event_string_code": b"LGOT"})
    status(0.2, 3_000_000.0, 0.0, 0.0)
    race_lap(0.2, 1, 0, 0.0)

    status(10.0, 1_500_000.0, 100_000.0, 500_000.0)
    race_lap(10.0, 1, 1, 2_000.0)
    status(20.0, 500_000.0, 910_000.0, 500_000.0)
    race_lap(20.0, 1, 2, 4_000.0)
    assert engine.state.snapshot(20.0).energy_mode != "under"

    status(29.0, 100_000.0, 1_800_000.0, 500_000.0)
    race_lap(29.0, 1, 2, 4_900.0)
    status(29.5, 100_000.0, 0.0, 0.0)
    race_lap(30.0, 2, 0, 0.0)
    assert engine.state.snapshot(30.0).energy_prev_lap_mode == "over"

    status(50.0, 100_000.0, 0.0, 0.0)
    race_lap(50.0, 2, 2, 4_900.0)
    status(59.5, 400_000.0, 0.0, 0.0)
    race_lap(60.0, 3, 0, 0.0)

    status(80.0, 400_000.0, 0.0, 300_000.0)
    race_lap(80.0, 3, 2, 4_900.0)
    status(89.9, 700_000.0, 0.0, 0.0)
    calls = race_lap(90.0, 4, 0, 0.0)

    snapshot = engine.state.snapshot(90.0)
    assert snapshot.energy_prev_lap_mode == "under"
    assert snapshot.energy_prev_under_mj > 0.2
    assert any(call.rule_id == "energy_under" for call in calls)
