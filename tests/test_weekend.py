from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime

from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.engine import build_engine, run_replay
from pitwall.ingest import ingest_recordings
from pitwall.model.deg import DegFit
from pitwall.net.recording import RecordingReader
from pitwall.store.db import Database

from .race_synth import RaceSpec, race_stream
from .synth import write_packet_stream


def _prior_db(db: Database, *, link: int, started_at: float, laps: int = 8) -> None:
    uid = 0xF1262001
    db.upsert_session(
        uid,
        track_id=7,
        session_type=1,
        started_at=started_at,
        weekend_link=link,
    )
    db.upsert_stint(
        uid,
        0,
        17,
        1,
        laps,
        DegFit(90_000.0, 140.0, 0.0, laps, 0.0, 1.0, "fit"),
    )


def test_same_weekend_practice_fit_precedes_other_priors(tmp_path) -> None:
    db = Database(tmp_path / "weekend.sqlite")
    settings = ConfigStore().current()
    practice_uid = 0xF1262001
    race_uid = 0xF1262002
    practice = write_packet_stream(
        tmp_path / "practice.f1bin",
        race_stream(
            RaceSpec(
                laps=8,
                session_type=1,
                weekend_link=0x26000002,
                session_uid=practice_uid,
                deg_ms=140,
                compound=17,
                dt=1.0,
                send_session_end=True,
            )
        ),
        session_uid=practice_uid,
    )
    result = ingest_recordings(db, [str(practice)], settings, out_dir=tmp_path / "digests")
    assert result[0].status == "ingested"
    practice_row = db.session_row(practice_uid)
    assert practice_row is not None and practice_row["weekend_link"] == 0x26000002

    race = write_packet_stream(
        tmp_path / "race.f1bin",
        race_stream(
            RaceSpec(
                laps=8,
                session_type=15,
                weekend_link=0x26000002,
                session_uid=race_uid,
                compound=17,
                dt=1.0,
                send_session_end=True,
            )
        ),
        session_uid=race_uid,
    )
    with RecordingReader(race) as reader:
        started_at = reader.header.wall_clock_start_us / 1_000_000
    db.upsert_session(
        race_uid,
        track_id=7,
        session_type=15,
        started_at=started_at,
        weekend_link=0x26000002,
    )
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db, decision_log_fp=io.StringIO())
    engine.state.session_uid = race_uid
    engine.state.track_id = 7

    prior = engine._deg_prior(7, 17, settings)  # noqa: SLF001

    assert prior.source == "weekend"
    assert prior.deg_ms_per_lap == 140.0

    asyncio.run(run_replay(race, engine))
    engine.fold_open_stint()
    assert (
        db._conn.execute(  # noqa: SLF001
            "SELECT count(*) FROM stints WHERE session_uid=?", (race_uid,)
        ).fetchone()[0]
        == 1
    )


def test_different_weekend_link_and_utc_day_do_not_match(tmp_path) -> None:
    db = Database(tmp_path / "weekend.sqlite")
    prior_at = datetime(2026, 5, 1, 12, 0, tzinfo=UTC).timestamp()
    _prior_db(db, link=101, started_at=prior_at)
    cases = (
        (0xF1262002, prior_at + 3_600, 202),
        (0xF1262003, prior_at + 86_400, 0),
    )
    for current_uid, current_at, current_link in cases:
        db.upsert_session(
            current_uid,
            track_id=7,
            session_type=15,
            started_at=current_at,
            weekend_link=current_link,
        )
        engine = build_engine(clock=VirtualClock(), sinks=[], db=db, decision_log_fp=io.StringIO())
        engine.state.session_uid = current_uid
        prior = engine._deg_prior(7, 17, ConfigStore().current())  # noqa: SLF001

        assert prior.source != "weekend"
        assert db.weekend_stints(current_uid, 7, 17) == []


def test_zero_weekend_link_falls_back_to_same_utc_date() -> None:
    db = Database(":memory:")
    same_day = datetime(2026, 5, 1, 12, 0, tzinfo=UTC).timestamp()
    _prior_db(db, link=0, started_at=same_day)
    current_uid = 0xF1262002
    db.upsert_session(
        current_uid,
        track_id=7,
        session_type=15,
        started_at=same_day + 3_600,
        weekend_link=202,
    )

    stints = db.weekend_stints(current_uid, 7, 17)

    assert len(stints) == 1
    assert stints[0].deg_ms_per_lap == 140.0


def test_open_final_stint_folds_once_at_session_end(tmp_path) -> None:
    db = Database(tmp_path / "fold.sqlite")
    uid = 0xF1262003
    path = write_packet_stream(
        tmp_path / "end.f1bin",
        race_stream(
            RaceSpec(
                laps=8,
                session_uid=uid,
                compound=17,
                dt=1.0,
                send_session_end=True,
            )
        ),
        session_uid=uid,
    )
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db, decision_log_fp=io.StringIO())

    asyncio.run(run_replay(path, engine))
    folded_on_end = db._conn.execute(  # noqa: SLF001
        "SELECT count(*) FROM stints WHERE session_uid=?", (uid,)
    ).fetchone()[0]
    assert folded_on_end == 1
    engine.fold_open_stint()

    stints = db._conn.execute(  # noqa: SLF001
        "SELECT start_lap, end_lap, n_valid_laps FROM stints WHERE session_uid=?", (uid,)
    ).fetchall()
    assert len(stints) == 1
    assert stints[0]["end_lap"] >= stints[0]["start_lap"]
    fitted = db.get_param(7, 17, "deg_ms_per_lap@8L")
    assert fitted is not None and fitted.weight == stints[0]["n_valid_laps"]
