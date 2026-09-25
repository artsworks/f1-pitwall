from __future__ import annotations

import pytest

from pitwall.ingest import Ingest
from pitwall.protocol.header import PacketId
from pitwall.state.ema import Ema
from pitwall.state.session import SessionState

from .synth import pack_packet


def _state() -> tuple[Ingest, SessionState]:
    ingest = Ingest()
    state = SessionState(ema_fast_s=3.0, ema_slow_s=30.0)
    state.register(ingest)
    return ingest, state


def _send(ingest: Ingest, pkt: bytes, t: float) -> None:
    ingest.on_datagram(pkt, t)


def test_phase_mapping() -> None:
    ingest, state = _state()
    _send(
        ingest,
        pack_packet(PacketId.LAP_DATA, {"cars": {0: {"driver_status": 3, "pit_status": 0}}}),
        0.0,
    )
    assert state.snapshot(0.0).phase == "out_lap"
    _send(
        ingest,
        pack_packet(PacketId.LAP_DATA, {"cars": {0: {"driver_status": 1, "pit_status": 1}}}),
        1.0,
    )
    assert state.snapshot(1.0).phase == "pitting"


def test_ema_converges_and_uses_session_time() -> None:
    ingest, state = _state()
    # settle at 60, then step to 90: fast EMA (tau 3s) tracks far ahead of
    # slow EMA (tau 30s).
    for i in range(330):
        t = i / 30.0
        temp = 60 if t < 1.0 else 90
        _send(
            ingest,
            pack_packet(
                PacketId.CAR_TELEMETRY,
                {"cars": {0: {"tyres_inner_temperature": (temp, temp, temp, temp)}}},
                session_time=t,
            ),
            t,
        )
    snap = state.snapshot(t)
    assert snap.tyre_inner_ema_fast.FL > 88.0
    assert snap.tyre_inner_ema_slow.FL < 80.0


def test_ema_reset_on_session_time_rewind() -> None:
    ingest, state = _state()
    for i in range(10):
        t = 10.0 + i
        _send(
            ingest,
            pack_packet(
                PacketId.CAR_TELEMETRY,
                {"cars": {0: {"tyres_inner_temperature": (90, 90, 90, 90)}}},
                session_time=t,
            ),
            t,
        )
    # session_time jumps backwards > 1 s (flashback)
    _send(
        ingest,
        pack_packet(
            PacketId.CAR_TELEMETRY,
            {"cars": {0: {"tyres_inner_temperature": (50, 50, 50, 50)}}},
            session_time=5.0,
        ),
        20.0,
    )
    snap = state.snapshot(20.0)
    assert snap.tyre_inner_ema_fast.FL == pytest.approx(50.0)


def test_snapshot_fields_and_age() -> None:
    ingest, state = _state()
    _send(
        ingest,
        pack_packet(
            PacketId.SESSION,
            {"session_type": 15, "track_id": 7, "total_laps": 50},
            session_time=10.0,
        ),
        0.0,
    )
    _send(
        ingest,
        pack_packet(
            PacketId.LAP_DATA,
            {"cars": {0: {"current_lap_num": 3, "driver_status": 1, "car_position": 2}}},
            session_time=10.5,
        ),
        0.1,
    )
    snap = state.snapshot(1.0)
    assert snap.session_kind == "race"
    assert snap.track_id == 7
    assert snap.total_laps == 50
    assert snap.lap_num == 3
    assert snap.position == 2
    assert snap.session_time == 10.5
    assert snap.age("lap_data") == pytest.approx(0.0)
    assert snap.age("car_damage") == float("inf")


def test_lap_summary_and_validity() -> None:
    ingest, state = _state()
    _send(
        ingest,
        pack_packet(
            PacketId.SESSION, {"session_type": 15, "safety_car_status": 0}, session_time=0.0
        ),
        0.0,
    )

    def lap(num: int, t: float, **kw: object) -> None:
        car = {"current_lap_num": num, "driver_status": 1, "pit_status": 0, **kw}
        _send(ingest, pack_packet(PacketId.LAP_DATA, {"cars": {0: car}}, session_time=t), t)

    # lap 1 in progress, then lap 2 starts -> lap 1 summary (first_lap invalid)
    lap(1, 0.1, last_lap_time_ms=0)
    lap(2, 90.0, last_lap_time_ms=80_000, sector1_time_ms_part=25_000)
    assert len(state.laps) == 1
    s1 = state.laps[0]
    assert s1.lap_num == 1
    assert not s1.valid
    assert "first_lap" in s1.invalid_reasons
    assert s1.lap_time_ms == 80_000
    assert s1.sector1_ms == 25_000

    # clean lap 2 -> valid summary at lap 3 boundary
    lap(3, 180.0, last_lap_time_ms=81_000)
    assert state.laps[-1].lap_num == 2
    assert state.laps[-1].valid

    # lap 3 pitted -> invalid with 'pitted'
    lap(4, 200.0, last_lap_time_ms=82_000)
    # pit during lap 4 then cross to 5
    lap(4, 201.0, pit_status=1)
    lap(5, 270.0, last_lap_time_ms=83_000)
    assert not state.laps[-1].valid
    assert "pitted" in state.laps[-1].invalid_reasons


def test_ema_scalar() -> None:
    e = Ema(3.0)
    e.update(0.0, 100.0)
    assert e.value == 100.0
    e.update(3.0, 0.0)  # dt == tau -> retains e^-1 of the old value
    assert e.value == pytest.approx(100.0 * 0.3678794411, abs=1.0)


def test_car_damage_reaches_snapshot_and_payload() -> None:
    from pitwall.server.app import state_payload

    ingest, state = _state()
    _send(
        ingest,
        pack_packet(
            PacketId.CAR_DAMAGE,
            {"cars": {0: {"front_left_wing_damage": 25, "floor_damage": 8}}},
        ),
        0.0,
    )
    snap = state.snapshot(0.0)
    assert snap.damage.front_left_wing == 25
    assert snap.damage.floor == 8

    from pitwall.config.loader import ConfigStore
    from pitwall.metrics import Metrics

    payload = state_payload(snap, settings=ConfigStore().current(), metrics=Metrics(), quiet=False)
    assert payload["damage"]["front_left_wing"] == 25
    assert payload["damage"]["ers_fault"] == 0
