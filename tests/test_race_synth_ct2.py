from __future__ import annotations

from typing import Any

from pitwall.ingest import Ingest
from pitwall.protocol.header import PacketId, parse_header
from pitwall.protocol.packets import parse
from pitwall.state.session import SessionState

from .race_synth import TRACK_M, RaceSpec, race_stream


def _ct2_frames(spec: RaceSpec) -> list[tuple[float, Any, Any]]:
    ingest = Ingest()
    state = SessionState()
    state.register(ingest)
    frames = []
    for session_time, payload in race_stream(spec):
        header = parse_header(payload)
        ingest.on_datagram(payload, session_time)
        if header.packet_id == PacketId.CAR_TELEMETRY_2:
            packet = parse(header.packet_id, payload)
            frames.append((session_time, packet, state.snapshot(session_time)))
    return frames


def test_default_race_stream_emits_no_ct2_packets() -> None:
    packets = race_stream(RaceSpec(laps=1, base_ms=10_000, dt=0.25))

    assert all(
        parse_header(payload).packet_id != PacketId.CAR_TELEMETRY_2 for _, payload in packets
    )


def test_aero_zone_countdown_mode_and_pulse() -> None:
    frames = _ct2_frames(
        RaceSpec(
            laps=2,
            base_ms=10_000,
            dt=0.25,
            ct2=True,
            aero_zones_m=((1000, 1500),),
        )
    )
    player_rows = [
        (snapshot.lap_distance, packet.cars[0], snapshot) for _, packet, snapshot in frames
    ]
    inside = [
        (distance, car, snapshot)
        for distance, car, snapshot in player_rows
        if 1000 <= distance < 1500
    ]
    outside = [(distance, car) for distance, car, _ in player_rows if not 1000 <= distance < 1500]

    assert inside and all(
        snapshot.active_aero_mode == 1 and car.active_aero_mode == 1 for _, car, snapshot in inside
    )
    assert outside and all(car.active_aero_mode == 0 for _, car in outside)
    countdown = [
        (distance, car.active_aero_activation_distance)
        for distance, car, _ in player_rows
        if 0 < car.active_aero_activation_distance
    ]
    first_lap_countdown = [
        (distance, remaining) for distance, remaining in countdown if distance < 1000
    ]
    assert first_lap_countdown[:2] == [(750.0, 250), (875.0, 125)]
    assert all(car.active_aero_activation_distance == 0 for _, car, _ in inside)
    assert sum(car.active_aero_available for _, car, _ in player_rows) == 2

    session_packets = [
        parse(PacketId.SESSION, payload)
        for _, payload in race_stream(
            RaceSpec(laps=1, base_ms=10_000, dt=0.25, ct2=True, aero_zones_m=((1000, 1500),))
        )
        if parse_header(payload).packet_id == PacketId.SESSION
    ]
    assert session_packets
    assert all(packet.num_drs_zones == 0 for packet in session_packets)
    assert all(packet.num_active_aero_zones_full == 0 for packet in session_packets)
    assert all(packet.num_active_aero_zones_partial == 0 for packet in session_packets)


def test_overtake_availability_and_activation_countdown() -> None:
    for gap_ahead_s, expected in ((0.5, 1), (1.5, 0)):
        frames = _ct2_frames(
            RaceSpec(
                laps=2,
                base_ms=10_000,
                dt=0.25,
                ct2=True,
                gap_ahead_s=gap_ahead_s,
                overtake_detect_m=2000,
            )
        )
        player_rows = [
            (snapshot.lap_distance, packet.cars[0], snapshot) for _, packet, snapshot in frames
        ]
        crossing_idx = next(i for i, (distance, _, _) in enumerate(player_rows) if distance == 2000)
        after_crossing = player_rows[crossing_idx:]
        assert all(car.overtake_available == expected for _, car, _ in after_crossing)
        assert all(car.overtake_active == expected for _, car, _ in after_crossing)
        assert all(snapshot.overtake_available == expected for _, _, snapshot in after_crossing)
        assert all(snapshot.overtake_active == expected for _, _, snapshot in after_crossing)
        for distance, car, _ in player_rows:
            remaining = (2000 - distance) % TRACK_M
            expected_countdown = int(remaining) if 0 < remaining <= 300 else 0
            assert car.overtake_activation_distance == expected_countdown


def test_wraparound_aero_zone_and_rival_overtake() -> None:
    frames = _ct2_frames(
        RaceSpec(
            laps=2,
            base_ms=10_000,
            dt=0.25,
            ct2=True,
            aero_zones_m=((TRACK_M - 200, 300),),
            rival_overtake_cars=(2,),
        )
    )
    player_rows = [(snapshot.lap_distance, packet.cars[0]) for _, packet, snapshot in frames]

    assert all(car.active_aero_mode == 1 for distance, car in player_rows if distance < 300)
    assert all(
        car.active_aero_mode == 1 for distance, car in player_rows if distance >= TRACK_M - 200
    )
    assert (
        sum(car.active_aero_available for distance, car in player_rows if distance >= TRACK_M - 200)
        == 2
    )

    for _, packet, _ in frames:
        assert all(packet.cars[car_idx].regulations_2026 == 1 for car_idx in range(4))
        assert packet.cars[2].overtake_available == 1
        assert packet.cars[2].overtake_active == 1
        assert packet.cars[0].overtake_available == 0
        assert packet.cars[1].overtake_available == 0
        assert packet.cars[3].overtake_available == 0
