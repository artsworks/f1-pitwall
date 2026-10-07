"""M3 persistence hooks: synthetic race stream -> laps/stints/pit_events/
model_params rows, rival laps from Session History."""

from __future__ import annotations

import asyncio
from pathlib import Path
from statistics import median

import pytest

from pitwall.clock import VirtualClock
from pitwall.engine import build_engine, run_replay
from pitwall.model.deg import DegFit
from pitwall.protocol.header import PacketId
from pitwall.state.lap import LapSummary
from pitwall.store.db import Database

from .race_synth import RaceSpec, race_stream
from .synth import make_event_packet, pack_packet, write_packet_stream

BASE_MS = 90_000
SLOPE = 100.0


def _race_stream(post_laps: int = 0) -> list[tuple[float, bytes]]:
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

    for lap in range(1, 11 + post_laps):
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


def _run(tmp_path: Path, *, post_laps: int = 0) -> tuple[VirtualClock, object, Database]:
    rec = write_packet_stream(tmp_path / "race.f1bin", _race_stream(post_laps))
    db = Database(":memory:")
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    asyncio.run(run_replay(rec, engine, None))
    return engine, engine.state, db


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


def test_race_synth_lap_noise_is_seeded() -> None:
    spec = RaceSpec(laps=8, lap_noise_ms=250.0, seed=17)

    assert race_stream(spec) == race_stream(spec)
    assert race_stream(spec) != race_stream(RaceSpec(laps=8))


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


def test_rules_facing_snapshot_carries_model(tmp_path: Path) -> None:
    engine, state, db = _run(tmp_path)
    snap = engine.state.snapshot(engine.clock.now())
    assert snap.deg_fit_source in ("blend", "fit")
    assert snap.laps_of_pace < float("inf")
    assert snap.pit_loss_source != ""
    assert snap.predicted_lap_ms > 0
    assert snap.fuel_source != ""


def test_session_end_writes_learning_pack_after_grading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = Database(":memory:")
    uid = 205
    db.upsert_session(uid, track_id=7, session_type=15, started_at=1.0)
    pack_dir = tmp_path / "learnings"
    calls: list[tuple[Database, Path, int, list[int] | None]] = []

    def write_pack(
        db_arg: Database,
        directory: Path,
        *,
        keep_days: int,
        refresh_quality: list[int] | None = None,
    ) -> Path:
        assert db_arg.maintenance_version(f"graded:{uid}") == 1
        calls.append((db_arg, directory, keep_days, refresh_quality))
        return directory / "learning-latest.json"

    monkeypatch.setattr("pitwall.learnpack.write_pack", write_pack)
    engine = build_engine(
        clock=VirtualClock(),
        sinks=[],
        db=db,
        learning_pack_dir=pack_dir,
        learning_pack_keep_days=9,
    )
    engine.state.session_uid = uid
    engine.state.session_type = 15
    engine.state.track_id = 7
    engine.state.session_ended = True

    engine.tick(1.0)

    assert calls == [(db, pack_dir, 9, [uid])]
    assert db.session_row(uid)["ended_at"] == 1.0


def test_restart_mid_session_on_same_uid_folds_stints_once(tmp_path: Path) -> None:
    uid = 0xDEADBEEF
    packets = _race_stream()
    cut = int(len(packets) * 0.8)
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
