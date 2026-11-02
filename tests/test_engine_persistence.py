"""M3 persistence hooks: synthetic race stream -> laps/stints/pit_events/
model_params rows, rival laps from Session History."""

from __future__ import annotations

import asyncio
from pathlib import Path
from statistics import median

import pytest

from pitwall.clock import VirtualClock
from pitwall.engine import _PendingPitLoss, build_engine, run_replay
from pitwall.model.deg import DegFit
from pitwall.protocol.header import PacketId
from pitwall.state.lap import SC_UNKNOWN, LapSummary
from pitwall.store.db import Database

from .race_synth import RaceSpec, race_stream
from .synth import make_event_packet, pack_packet, write_packet_stream

BASE_MS = 90_000
SLOPE = 100.0


def _race_stream(
    post_laps: int = 0, *, pit_status_laps: set[int] | None = None
) -> list[tuple[float, bytes]]:
    """10 player laps on a 5 km race (session_type 15, track 7). Lap 8 pits,
    and the next lap is the out-lap. Rival session history has two completed laps."""
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
    pitted_laps = {8} if pit_status_laps is None else pit_status_laps

    def lap_time(finished_lap: int) -> int:
        if finished_lap == 8:
            return BASE_MS + 5_000  # in-lap
        if finished_lap == 9:
            return BASE_MS + 18_000  # out-lap
        return int(BASE_MS + SLOPE * finished_lap)

    for lap in range(1, 11 + post_laps):
        pit_status = 1 if lap in pitted_laps else 0
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
                            "sector1_time_ms_part": 30_000 + lap,
                            "sector2_time_ms_part": 30_500 + lap,
                            "driver_status": 4,
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


def _run(
    tmp_path: Path, *, post_laps: int = 0, pit_status_laps: set[int] | None = None
) -> tuple[object, object, Database]:
    rec = write_packet_stream(
        tmp_path / "race.f1bin",
        _race_stream(post_laps, pit_status_laps=pit_status_laps),
    )
    db = Database(":memory:")
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    asyncio.run(run_replay(rec, engine, None))
    return engine, engine.state, db


def test_sc_pit_loss_uses_vsc_default_for_vsc_status() -> None:
    db = Database(":memory:")
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    engine.state.session_uid = 1
    engine.state.track_id = 7

    for status, expected in ((2, 12.0), (1, 8.0), (0, 8.0)):
        engine.state.safety_car_status = status
        engine._update_model()
        assert engine._sc_pit_loss_s == expected

    db.close()


def _run_synth_spec(tmp_path: Path, spec: RaceSpec) -> tuple[object, Database]:
    rec = write_packet_stream(
        tmp_path / "race-synth.f1bin",
        race_stream(spec),
        metadata={"synthetic": False},
    )
    db = Database(":memory:")
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    asyncio.run(run_replay(rec, engine, None))
    return engine, db


def _deg_prior_engine(db: Database):
    uid = 0xF1262010
    engine = build_engine(
        clock=VirtualClock(),
        sinks=[],
        db=db,
    )
    engine.store.set_track(7)
    engine.state.session_uid = uid
    engine.state.session_type = 15
    db.upsert_session(uid, track_id=7, session_type=15)
    settings = engine.store.current()
    assert settings.track is not None and settings.track.base_pace_ms == 0
    return engine, uid, settings


def _insert_deg_prior_lap(
    db: Database,
    uid: int,
    lap_num: int,
    lap_time_ms: int,
    tyre_age_laps: int,
    *,
    valid: bool = True,
    sc_status: int = 0,
) -> None:
    db.insert_lap(
        uid,
        0,
        LapSummary(
            lap_num=lap_num,
            lap_time_ms=lap_time_ms,
            sector1_ms=0,
            sector2_ms=0,
            compound=17,
            tyre_age_laps=tyre_age_laps,
            fuel_remaining_laps_at_end=10.0,
            valid=valid,
            sc_status=sc_status,
        ),
    )


def _flush_pit_loss_sample(inlap_slow_ms: int, neutralised: int) -> tuple[Database, int]:
    uid = 7001
    db = Database(":memory:")
    db.upsert_session(uid, track_id=7, session_type=15)
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    lap_times = [
        (5, BASE_MS),
        (6, BASE_MS),
        (7, BASE_MS),
        (8, BASE_MS + inlap_slow_ms),
        (9, BASE_MS),
        (10, BASE_MS),
        (11, BASE_MS),
        (12, BASE_MS),
    ]
    for lap_num, lap_time_ms in lap_times:
        _insert_deg_prior_lap(db, uid, lap_num, lap_time_ms, lap_num)
    laps = db.laps_for(uid, 0)
    by_num = {lap.lap_num: lap for lap in laps}
    engine._pending_pit_loss = _PendingPitLoss(
        session_uid=uid,
        track_id=7,
        in_lap=by_num[8],
        out_lap=by_num[9],
        ref_pace_ms=BASE_MS,
        lane_ms=19_500,
        neutralised=neutralised,
    )

    engine._flush_pending_pit_loss(uid, laps, force=True)
    return db, uid


def test_deg_prior_uses_clean_session_lap_median() -> None:
    db = Database(":memory:")
    engine, uid, settings = _deg_prior_engine(db)
    clean = [(112_618, 1), (112_958, 2), (112_093, 3)]
    for lap_num, (lap_time_ms, age) in enumerate(clean, start=1):
        _insert_deg_prior_lap(db, uid, lap_num, lap_time_ms, age)

    prior = engine._deg_prior(7, 17, settings)  # noqa: SLF001
    expected = median(lap_time_ms - prior.deg_ms_per_lap * age for lap_time_ms, age in clean)

    assert prior.base_ms == expected
    assert prior.base_ms != 95_000


def test_deg_prior_keeps_learned_base_ahead_of_session_laps() -> None:
    db = Database(":memory:")
    engine, uid, settings = _deg_prior_engine(db)
    _insert_deg_prior_lap(db, uid, 1, 112_618, 1)
    name = engine._learned_name(7, 17, "base_ms")  # noqa: SLF001
    db.set_param(7, 17, name, 110_000.0, weight=3.0)

    prior = engine._deg_prior(7, 17, settings)  # noqa: SLF001

    assert prior.base_ms == 110_000.0


def test_deg_prior_uses_fallback_without_clean_session_laps() -> None:
    db = Database(":memory:")
    engine, uid, settings = _deg_prior_engine(db)
    _insert_deg_prior_lap(db, uid, 1, 127_038, 1, valid=False)
    _insert_deg_prior_lap(db, uid, 2, 127_038, 2, sc_status=1)

    prior = engine._deg_prior(7, 17, settings)  # noqa: SLF001

    assert prior.base_ms == 95_000


def test_learned_thermal_window_widens_the_degradation_cutoff() -> None:
    db = Database(":memory:")
    engine, _, settings = _deg_prior_engine(db)
    db.set_param(7, 17, "thermal_lo_c", 95.0, weight=2.0)
    db.set_param(7, 17, "thermal_hi_c", 100.0, weight=2.0)

    assert engine._thermal_hi_c(7, 17, settings) == 105.0


def test_player_laps_carry_wear_fuel_ers(tmp_path: Path) -> None:
    engine, state, db = _run(tmp_path)
    uid = state.session_uid
    assert uid is not None
    laps = db.laps_for(uid, 0)
    assert len(laps) >= 9
    lap2 = next(r for r in laps if r.lap_num == 2)
    assert lap2.wear_pct > 0.0
    assert lap2.fuel_kg == pytest.approx(110.0 - 1.7 * 3, abs=0.01)  # lap-3 tick value
    assert lap2.ers_deployed_j == 250_000.0


def test_player_laps_written_once_with_sector_times(tmp_path: Path) -> None:
    _, state, db = _run(tmp_path)
    uid = state.session_uid
    assert uid is not None

    laps = db.laps_for(uid, 0)
    lap_nums = [lap.lap_num for lap in laps]
    assert len(lap_nums) == len(set(lap_nums))
    assert len(laps) >= 9
    for lap in laps:
        assert (lap.s1_ms, lap.s2_ms) == (30_000 + lap.lap_num, 30_500 + lap.lap_num)


def test_flashback_across_line_keeps_one_row_per_lap(tmp_path: Path) -> None:
    packets: list[tuple[float, bytes]] = []
    t = 0.0
    session_time = 0.0

    def emit(pid: int, data: dict) -> None:
        nonlocal t
        packets.append((t, pack_packet(pid, data, session_time=session_time)))
        t += 0.033

    emit(
        PacketId.SESSION,
        {"session_type": 15, "track_id": 7, "total_laps": 12, "track_length": 5000},
    )

    def lap_data(lap_num: int) -> None:
        nonlocal session_time
        for _ in range(3):
            emit(
                PacketId.LAP_DATA,
                {
                    "cars": {
                        0: {
                            "current_lap_num": lap_num,
                            "last_lap_time_ms": 90_000 + lap_num,
                            "driver_status": 4,
                            "result_status": 2,
                            "sector1_time_ms_part": 30_000 + lap_num,
                            "sector2_time_ms_part": 30_500 + lap_num,
                        }
                    }
                },
            )
            session_time += 30

    for index, lap_num in enumerate((1, 2, 3, 4, 3, 4, 5, 6)):
        if index == 4:
            session_time -= 100
        lap_data(lap_num)

    recording = write_packet_stream(tmp_path / "flashback.f1bin", packets)
    db = Database(":memory:")
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    asyncio.run(run_replay(recording, engine, None))
    uid = engine.state.session_uid
    assert uid is not None

    laps = db.laps_for(uid, 0)
    assert [lap.lap_num for lap in laps] == [1, 2, 3, 4, 5]
    lap3 = next(lap for lap in laps if lap.lap_num == 3)
    assert lap3.valid == 0
    assert "flashback" in lap3.invalid_reasons


def test_stint_refit_writes_stints_row(tmp_path: Path) -> None:
    engine, state, db = _run(tmp_path)
    uid = state.session_uid
    assert uid is not None
    stints = db.stints_for_track(7, 18)
    assert stints, "expected a stints row after enough valid laps"
    assert abs(stints[0].deg_ms_per_lap - SLOPE) < 30.0
    assert engine.deg_fit is not None and engine.deg_fit.n >= 5


def test_pit_sequence_persists_event_and_param(tmp_path: Path) -> None:
    engine, state, db = _run(tmp_path, post_laps=3)
    uid = state.session_uid
    assert uid is not None
    events = db.pit_events_for_session(uid)
    assert len(events) == 1
    ev = events[0]
    assert ev.lap_num == 8  # the in-lap
    assert ev.ref_pace_ms > 0
    # The out-lap uses the fresh-tyre reference.
    assert 20_000 < ev.loss_ms < 26_000
    p = db.get_param(7, 0, "pit_loss_green_ms")
    assert p is not None and p.value == float(ev.loss_ms)


def test_slow_green_pit_inlap_does_not_persist_event_or_param() -> None:
    db, uid = _flush_pit_loss_sample(23_700, neutralised=0)

    assert db.pit_events_for_session(uid) == []
    assert db.get_param(7, 0, "pit_loss_green_ms") is None


@pytest.mark.parametrize(
    ("inlap_slow_ms", "neutralised", "param_name"),
    [(5_000, 0, "pit_loss_green_ms"), (15_000, 2, "pit_loss_vsc_ms")],
)
def test_pit_loss_guard_keeps_normal_and_neutralised_stops(
    inlap_slow_ms: int, neutralised: int, param_name: str
) -> None:
    db, uid = _flush_pit_loss_sample(inlap_slow_ms, neutralised)

    events = db.pit_events_for_session(uid)
    assert len(events) == 1
    assert events[0].loss_ms == inlap_slow_ms
    param = db.get_param(7, 0, param_name)
    assert param is not None and param.value == inlap_slow_ms


def test_repeated_pit_lane_laps_do_not_persist_a_stop(tmp_path: Path) -> None:
    engine, _, db = _run(tmp_path, post_laps=3, pit_status_laps={8, 9, 10})
    uid = engine.state.session_uid
    assert uid is not None
    laps = {row.lap_num: row for row in db.laps_for(uid, 0)}

    assert all("pitted" in laps[n].invalid_reasons for n in (8, 9, 10))
    assert "after_in_lap" in laps[9].invalid_reasons
    assert db.pit_events_for_session(uid) == []
    assert db.get_param(7, 0, "pit_loss_green_ms") is None


@pytest.mark.replay
def test_race_synth_pit_loss_and_compound_change_replay(tmp_path: Path) -> None:
    spec = RaceSpec(
        laps=14,
        player_pit_lap=6,
        pit_lane_loss_ms=20_000,
        compound_after_stop=18,
        deg_ms_after_stop=40,
        lap_noise_ms=0,
    )
    rec = write_packet_stream(
        tmp_path / "race-synth.f1bin",
        race_stream(spec),
        metadata={"synthetic": False},
    )
    db = Database(":memory:")
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    asyncio.run(run_replay(rec, engine, None))

    uid = engine.state.session_uid
    assert uid is not None
    events = db.pit_events_for_session(uid)
    assert len(events) == 1
    assert events[0].loss_ms == pytest.approx(20_000, abs=1_000)
    laps = db.laps_for(uid, 0)
    assert "pitted" in laps[5].invalid_reasons
    assert "after_in_lap" in laps[6].invalid_reasons
    assert [stint.compound for stint in db.stints_for_session(uid)] == [17, 18]


@pytest.mark.replay
def test_race_synth_after_line_stop_ignores_early_request(tmp_path: Path) -> None:
    spec = RaceSpec(
        laps=18,
        player_pit_lap=12,
        pit_lane_loss_ms=20_000,
        pit_request_laps_early=4,
        pit_box_after_line=True,
    )
    engine, db = _run_synth_spec(tmp_path, spec)
    uid = engine.state.session_uid
    assert uid is not None
    events = db.pit_events_for_session(uid)
    laps = db.laps_for(uid, 0)

    assert len(events) == 1
    assert events[0].lap_num == 12
    assert events[0].loss_ms == pytest.approx(20_000, abs=1_000)
    assert all(row.valid for row in laps if 2 <= row.lap_num < 8)


@pytest.mark.replay
def test_race_synth_rejects_implausible_pit_loss(tmp_path: Path) -> None:
    spec = RaceSpec(laps=14, player_pit_lap=6, pit_lane_loss_ms=70_000)
    engine, db = _run_synth_spec(tmp_path, spec)
    uid = engine.state.session_uid
    assert uid is not None
    laps = {row.lap_num: row for row in db.laps_for(uid, 0)}

    assert "pitted" in laps[6].invalid_reasons
    assert "after_in_lap" in laps[7].invalid_reasons
    assert db.pit_events_for_session(uid) == []
    assert db.get_param(7, 0, "pit_loss_green_ms") is None


@pytest.mark.replay
def test_race_synth_lap_noise_is_seeded() -> None:
    spec = RaceSpec(laps=8, lap_noise_ms=250.0, seed=17)

    assert race_stream(spec) == race_stream(spec)
    assert race_stream(spec) != race_stream(RaceSpec(laps=8))


def test_fuel_kg_per_lap_folded(tmp_path: Path) -> None:
    engine, state, db = _run(tmp_path)
    p = db.get_param(7, 0, "fuel_kg_per_lap")
    assert p is not None
    assert abs(p.value - 1.7) < 0.01


def test_run_fuel_excludes_out_laps_and_resets_on_next_run() -> None:
    engine = build_engine(clock=VirtualClock(), sinks=[], db=Database(":memory:"))
    engine._note_session_fuel(
        LapSummary(1, 90_000, 30_000, 30_000, 17, 1, 10.0, False, ["first_lap"], fuel_kg=20.0)
    )
    engine._note_session_fuel(
        LapSummary(2, 90_000, 30_000, 30_000, 17, 1, 10.0, True, fuel_kg=18.0)
    )
    engine._note_session_fuel(
        LapSummary(3, 100_000, 30_000, 30_000, 17, 1, 10.0, False, ["pitted"], fuel_kg=17.0)
    )
    engine._note_session_fuel(
        LapSummary(
            4,
            100_000,
            30_000,
            30_000,
            17,
            1,
            10.0,
            False,
            ["pitted", "after_in_lap"],
            fuel_kg=16.0,
        )
    )
    assert [delta for _, delta in engine._run_fuel_deltas] == pytest.approx([2.0])

    engine._note_session_fuel(
        LapSummary(5, 90_000, 30_000, 30_000, 17, 1, 10.0, True, fuel_kg=14.38)
    )
    engine._note_session_fuel(
        LapSummary(6, 90_000, 30_000, 30_000, 17, 1, 10.0, True, fuel_kg=12.76)
    )
    assert [delta for _, delta in engine._run_fuel_deltas] == pytest.approx([2.0, 1.62, 1.62])

    engine._run_fuel_deltas[:] = [(1.0, 1.5), (2.0, 1.6)]
    engine.ingest.on_datagram(
        pack_packet(PacketId.LAP_DATA, {"cars": {0: {"driver_status": 3, "pit_status": 0}}}),
        1.0,
    )
    assert engine.state.run_seq == 1
    engine.tick(1.0)
    assert engine.last_snapshot is not None
    assert engine.last_snapshot.run_fuel_per_lap_kg == 0.0
    assert engine.last_snapshot.run_fuel_laps == 0


def test_run_fuel_drops_laps_a_flashback_erased() -> None:
    engine = build_engine(clock=VirtualClock(), sinks=[], db=Database(":memory:"))
    state = engine.state

    def lap(num: int, fuel_kg: float, at_s: float) -> None:
        state.last_packet_t = at_s
        engine._note_session_fuel(
            LapSummary(num, 90_000, 30_000, 30_000, 17, 1, 10.0, True, fuel_kg=fuel_kg)
        )

    lap(1, 20.0, 90.0)
    lap(2, 18.5, 180.0)
    lap(3, 16.8, 270.0)
    assert [delta for _, delta in engine._run_fuel_deltas] == pytest.approx([1.5, 1.7])

    for listener in state.rewind_listeners:
        listener(170.0)
    assert engine._run_fuel_deltas == []

    lap(2, 18.6, 180.0)
    lap(3, 16.6, 270.0)
    engine.ingest.on_datagram(pack_packet(PacketId.LAP_DATA, {"cars": {0: {}}}), 1.0)
    engine.tick(1.0)
    assert engine.last_snapshot is not None
    assert engine.last_snapshot.run_fuel_per_lap_kg == pytest.approx(2.0)
    assert engine.last_snapshot.run_fuel_laps == 1


def test_session_fuel_median_drops_laps_a_flashback_erased() -> None:
    engine = build_engine(clock=VirtualClock(), sinks=[], db=Database(":memory:"))
    state = engine.state

    def lap(num: int, fuel_kg: float, at_s: float) -> None:
        state.last_packet_t = at_s
        engine._note_session_fuel(
            LapSummary(num, 90_000, 30_000, 30_000, 17, 1, 10.0, True, fuel_kg=fuel_kg)
        )

    lap(1, 20.0, 90.0)
    lap(2, 18.5, 180.0)
    lap(3, 16.8, 270.0)
    assert engine._fuel_per_lap(10) == pytest.approx((1.6, "session"))

    for listener in state.rewind_listeners:
        listener(170.0)

    lap(2, 18.6, 180.0)
    lap(3, 16.6, 270.0)
    lap(4, 14.6, 360.0)
    assert engine._fuel_per_lap(10) == pytest.approx((2.0, "session"))


def test_rival_laps_persisted(tmp_path: Path) -> None:
    engine, state, db = _run(tmp_path)
    uid = state.session_uid
    assert uid is not None
    rival = db.laps_for(uid, 1)
    assert [r.lap_num for r in rival] == [1, 2]
    assert rival[0].lap_time_ms == 91_500 and rival[0].compound == 17


def test_rival_safety_car_laps_are_excluded_from_field_pace() -> None:
    uid = 801
    db = Database(":memory:")
    db.upsert_session(uid, track_id=7, session_type=5)
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    for lap_num in range(1, 7):
        safety_car_status = int(lap_num > 3)
        session = pack_packet(
            PacketId.SESSION,
            {
                "session_type": 5,
                "track_id": 7,
                "safety_car_status": safety_car_status,
            },
            session_uid=uid,
            session_time=float(lap_num),
        )
        engine.ingest.on_datagram(session, float(lap_num))
        lap_data = pack_packet(
            PacketId.LAP_DATA,
            {"cars": {car_idx: {"current_lap_num": lap_num} for car_idx in range(4)}},
            session_uid=uid,
            session_time=lap_num + 0.1,
        )
        engine.ingest.on_datagram(lap_data, lap_num + 0.1)

    history = pack_packet(
        PacketId.SESSION_HISTORY,
        {
            "car_idx": 1,
            "num_laps": 7,
            "num_tyre_stints": 1,
            "laps": {
                lap_idx: {
                    "lap_time_ms": 90_000 if lap_idx < 3 else 120_000,
                    "lap_valid_bit_flags": 1,
                }
                for lap_idx in range(7)
            },
            "tyre_stints": {
                0: {"end_lap": 255, "tyre_actual_compound": 17, "tyre_visual_compound": 17}
            },
        },
        session_uid=uid,
        session_time=7.0,
    )
    engine.ingest.on_datagram(history, 7.0)

    assert [lap.sc_status for _, lap in engine.state.rival_laps] == [0, 0, 0, 1, 1, 1]
    for car_idx, lap_time in ((2, 95_000), (3, 105_000)):
        for lap_num in range(1, 4):
            db.insert_lap(
                uid, car_idx, LapSummary(lap_num, lap_time, 30_000, 30_000, 17, 1, 10, True)
            )

    engine._write_laps()
    assert [lap.sc_status for lap in db.laps_for(uid, 1)] == [0, 0, 0, 1, 1, 1]
    for lap_num, lap_time in enumerate((90_000, 90_000, 90_000), start=1):
        db.insert_lap(uid, 0, LapSummary(lap_num, lap_time, 30_000, 30_000, 17, 1, 10, True))
    engine._fold_driver(uid)

    assert db.get_param(7, 0, "driver_pace_delta_ms").value == -5_000


def test_rival_history_without_lap_data_uses_unknown_sc_and_is_excluded() -> None:
    uid = 802
    db = Database(":memory:")
    db.upsert_session(uid, track_id=7, session_type=5)
    engine = build_engine(
        clock=VirtualClock(),
        overrides={"thresholds": {"driver_min_laps": 2, "driver_field_min_cars": 1}},
        sinks=[],
        db=db,
    )
    session = pack_packet(
        PacketId.SESSION,
        {"session_type": 5, "track_id": 7},
        session_uid=uid,
    )
    engine.ingest.on_datagram(session, 0.0)
    history = pack_packet(
        PacketId.SESSION_HISTORY,
        {
            "car_idx": 3,
            "num_laps": 3,
            "num_tyre_stints": 1,
            "laps": {
                lap_idx: {"lap_time_ms": 60_000, "lap_valid_bit_flags": 1} for lap_idx in range(3)
            },
            "tyre_stints": {
                0: {"end_lap": 255, "tyre_actual_compound": 17, "tyre_visual_compound": 17}
            },
        },
        session_uid=uid,
        session_time=1.0,
    )
    engine.ingest.on_datagram(history, 1.0)

    unknown = [lap for car_idx, lap in engine.state.rival_laps if car_idx == 3]
    assert len(unknown) == 2
    assert [lap.sc_status for lap in unknown] == [SC_UNKNOWN, SC_UNKNOWN]

    for lap_num in range(1, 4):
        db.insert_lap(uid, 0, LapSummary(lap_num, 90_000, 30_000, 30_000, 17, 1, 10, True))
    engine._write_laps()
    assert [lap.sc_status for lap in db.laps_for(uid, 3)] == [SC_UNKNOWN, SC_UNKNOWN]
    engine._fold_driver(uid)

    assert db.get_param(7, 0, "driver_pace_delta_ms") is None


def test_rules_facing_snapshot_carries_model(tmp_path: Path) -> None:
    engine, state, db = _run(tmp_path)
    snap = engine.state.snapshot(engine.clock.now())
    assert snap.deg_fit_source in ("blend", "fit")
    assert snap.laps_of_pace < float("inf")
    assert snap.pit_loss_source != ""
    assert snap.predicted_lap_ms > 0
    assert snap.fuel_source != ""


def test_session_end_calls_hook_after_grading() -> None:
    db = Database(":memory:")
    uid = 205
    db.upsert_session(uid, track_id=7, session_type=15, started_at=1.0)
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    calls: list[int] = []

    def on_session_end(session_uid: int) -> None:
        assert db.maintenance_version(f"graded:{session_uid}") == 1
        calls.append(session_uid)

    engine.on_session_end = on_session_end
    engine.state.session_uid = uid
    engine.state.session_type = 15
    engine.state.track_id = 7
    engine.state.session_ended = True

    engine.tick(1.0)
    engine.tick(2.0)

    assert calls == [uid]
    assert db.session_row(uid)["ended_at"] == 1.0


@pytest.mark.replay
def test_restart_mid_session_on_same_uid_folds_stints_once(tmp_path: Path) -> None:
    uid = 0xDEADBEEF
    packets = _race_stream()
    cut = int(len(packets) * 0.7)
    t_end = packets[-1][0] + 0.1
    send = (t_end, make_event_packet(b"SEND", session_uid=uid, session_time=t_end))
    full = [*packets, send]
    resumed = [(packets[cut][0], packets[0][1]), *packets[cut:], send]

    def replay(name: str, stream: list[tuple[float, bytes]], db: Database) -> None:
        rec = write_packet_stream(tmp_path / f"{name}.f1bin", stream)
        engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
        asyncio.run(run_replay(rec, engine, None))

    baseline_db = Database(tmp_path / "baseline.sqlite")
    replay("baseline", full, baseline_db)
    baseline_deg = baseline_db.get_param(7, 18, "deg_ms_per_lap@12L")
    baseline_base = baseline_db.get_param(7, 18, "base_ms@12L")
    assert baseline_deg is not None and baseline_deg.weight == 6.0
    assert baseline_base is not None and baseline_base.weight == 6.0

    restart_db = Database(tmp_path / "restart.sqlite")
    replay("first", packets[:cut], restart_db)
    session = restart_db.session_row(uid)
    assert session is not None and session["ended_at"] is None
    assert restart_db.get_param(7, 18, "deg_ms_per_lap@12L") is None

    replay("resumed", resumed, restart_db)
    restart_deg = restart_db.get_param(7, 18, "deg_ms_per_lap@12L")
    restart_base = restart_db.get_param(7, 18, "base_ms@12L")
    assert restart_deg is not None and restart_deg.weight == baseline_deg.weight
    assert restart_base is not None and restart_base.weight == baseline_base.weight
    session = restart_db.session_row(uid)
    assert session is not None and session["ended_at"] is not None


def test_only_clean_fits_on_known_tracks_fold_into_priors() -> None:
    db = Database(":memory:")
    engine = build_engine(clock=VirtualClock(), db=db)
    clean = DegFit(90_000.0, 80.0, 30.0, 8, 150.0, 0.9, "fit")
    engine._fold_fit(-1, 18, clean)
    engine._fold_fit(7, 18, DegFit(55_520.0, 600.0, 0.0, 7, 7_992.0, 0.2, "fit"))
    engine._fold_fit(7, 18, DegFit(90_000.0, 80.0, 30.0, 4, 150.0, 0.5, "blend"))
    assert db.get_param(-1, 18, "deg_ms_per_lap") is None
    assert db.get_param(7, 18, "deg_ms_per_lap") is None
    engine._fold_fit(7, 18, clean)
    p = db.get_param(7, 18, "deg_ms_per_lap")
    assert p is not None and p.value == 80.0


def test_run_fuel_counts_hot_lap_that_began_at_the_out_lap_crossing() -> None:
    engine = build_engine(clock=VirtualClock(), sinks=[], db=Database(":memory:"))
    state = engine.state

    def lap_data(lap: int, driver_status: int, pit_status: int = 0) -> None:
        engine.ingest.on_datagram(
            pack_packet(
                PacketId.LAP_DATA,
                {
                    "cars": {
                        0: {
                            "current_lap_num": lap,
                            "driver_status": driver_status,
                            "pit_status": pit_status,
                        }
                    }
                },
            ),
            1.0,
        )

    lap_data(1, 3)
    state.fuel_in_tank = 4.966
    lap_data(1, 1)
    assert state.hot_lap_fuel_base == (1, pytest.approx(4.966))
    engine._note_session_fuel(
        LapSummary(
            1, 71_498, 20_000, 25_000, 17, 1, 10.0, True, ["first_lap", "pitted"], fuel_kg=4.016
        )
    )
    assert [delta for _, delta in engine._run_fuel_deltas] == pytest.approx([0.95])

    lap_data(2, 3)
    state.fuel_in_tank = 3.9
    lap_data(2, 1)
    lap_data(2, 2, pit_status=1)
    assert state.hot_lap_fuel_base is None
    engine._note_session_fuel(
        LapSummary(2, 80_000, 20_000, 25_000, 17, 1, 10.0, False, ["pitted"], fuel_kg=3.0)
    )
    assert engine._run_fuel_deltas == []
