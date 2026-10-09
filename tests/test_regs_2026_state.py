from __future__ import annotations

from pitwall.ingest import Ingest
from pitwall.protocol.header import PacketId, parse_header
from pitwall.state.session import SessionState, Snapshot

from .race_synth import RaceSpec, race_stream
from .synth import pack_packet


def _ct2_snapshots(spec: RaceSpec) -> tuple[SessionState, list[Snapshot]]:
    ingest = Ingest()
    state = SessionState()
    state.register(ingest)
    snapshots = []
    for session_time, payload in race_stream(spec):
        header = parse_header(payload)
        ingest.on_datagram(payload, session_time)
        if header.packet_id == PacketId.CAR_TELEMETRY_2:
            snapshots.append(state.snapshot(session_time))
    return state, snapshots


def test_aero_zone_ahead_uses_ct2_countdown() -> None:
    _, snapshots = _ct2_snapshots(
        RaceSpec(
            laps=1,
            base_ms=10_000,
            dt=0.25,
            ct2=True,
            aero_zones_m=((1000, 1500),),
        )
    )

    assert snapshots
    assert any(snapshot.aero_zone_ahead for snapshot in snapshots)
    for snapshot in snapshots:
        distance = snapshot.active_aero_activation_distance_m
        assert snapshot.aero_zone_ahead == (0 < distance <= 250)
        assert not snapshot.drs_zone_ahead
        if 1000 <= snapshot.lap_distance < 1500:
            assert not snapshot.aero_zone_ahead
        if snapshot.lap_distance >= 1500:
            assert not snapshot.aero_zone_ahead


def test_overtake_zone_ahead_uses_activation_countdown() -> None:
    _, snapshots = _ct2_snapshots(
        RaceSpec(
            laps=1,
            base_ms=20_000,
            dt=0.25,
            ct2=True,
            overtake_detect_m=2000,
        )
    )

    assert snapshots
    assert any(snapshot.overtake_zone_ahead for snapshot in snapshots)
    for snapshot in snapshots:
        distance = snapshot.overtake_activation_distance_m
        expected = 0 < distance <= 300
        assert snapshot.overtake_zone_ahead == expected
        assert snapshot.drs_zone_ahead == expected


def test_2026_drs_availability_follows_overtake_activation() -> None:
    _, snapshots = _ct2_snapshots(
        RaceSpec(
            laps=2,
            base_ms=20_000,
            dt=0.25,
            ct2=True,
            gap_ahead_s=0.5,
            overtake_detect_m=2000,
        )
    )

    assert snapshots
    assert all(snapshot.drs_available == bool(snapshot.overtake_active) for snapshot in snapshots)
    detection_idx = next(i for i, snapshot in enumerate(snapshots) if snapshot.overtake_available)
    activation_idx = next(
        i for i in range(detection_idx + 1, len(snapshots)) if snapshots[i].overtake_active
    )
    assert detection_idx < activation_idx
    assert all(
        snapshot.overtake_available and not snapshot.overtake_active and not snapshot.drs_available
        for snapshot in snapshots[detection_idx:activation_idx]
    )
    assert all(
        snapshot.overtake_active and snapshot.drs_available
        for snapshot in snapshots[activation_idx:]
    )

    _, no_gap_snapshots = _ct2_snapshots(
        RaceSpec(
            laps=2,
            base_ms=20_000,
            dt=0.25,
            ct2=True,
            gap_ahead_s=1.5,
            overtake_detect_m=2000,
        )
    )
    assert no_gap_snapshots
    assert all(not snapshot.overtake_active for snapshot in no_gap_snapshots)
    assert all(not snapshot.overtake_available for snapshot in no_gap_snapshots)
    assert all(not snapshot.drs_available for snapshot in no_gap_snapshots)


def test_legacy_snapshot_defaults_without_ct2() -> None:
    state, _ = _ct2_snapshots(RaceSpec(laps=1, base_ms=10_000, dt=0.25))

    snapshot = state.snapshot(0.0)
    assert not snapshot.regulations_2026
    assert not snapshot.drs_available
    assert not snapshot.aero_zone_ahead


def test_rival_overtake_uses_lazy_ct2_car_array() -> None:
    _, snapshots = _ct2_snapshots(
        RaceSpec(
            laps=1,
            base_ms=10_000,
            dt=0.25,
            ct2=True,
            rival_overtake_cars=(1,),
        )
    )

    assert snapshots
    assert all(snapshot.rival_ahead_idx == 1 for snapshot in snapshots)
    assert all(snapshot.rival_ahead_overtake for snapshot in snapshots)
    assert all(not snapshot.rival_behind_overtake for snapshot in snapshots)


def test_2026_car_status_ignores_mguh_harvest() -> None:
    ingest = Ingest()
    state = SessionState()
    state.register(ingest)

    ingest.on_datagram(
        pack_packet(
            PacketId.CAR_TELEMETRY_2,
            {"cars": {0: {"regulations_2026": 1}}},
            session_time=1.0,
            frame=1,
        ),
        1.0,
    )
    ingest.on_datagram(
        pack_packet(
            PacketId.CAR_STATUS,
            {"cars": {0: {"ers_harvested_this_lap_mguh": 123_456.0}}},
            session_time=1.0,
            frame=1,
        ),
        1.0,
    )

    assert state.ers_harvested_mguh_j == 0.0
    assert state._ers_samples[-1][3] == 0.0
