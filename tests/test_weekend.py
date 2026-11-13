from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime

import pytest

from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.engine import build_engine, run_replay
from pitwall.ingest import ingest_recordings
from pitwall.model.deg import DegFit
from pitwall.net.recording import RecordingReader
from pitwall.state.lap import LapSummary
from pitwall.store.db import Database

from .race_synth import RaceSpec, race_stream
from .synth import write_packet_stream


def _prior_db(
    db: Database,
    *,
    link: int,
    started_at: float,
    laps: int = 8,
    rmse_ms: float = 0.0,
    session_type: int = 1,
) -> None:
    uid = 0xF1262001
    db.upsert_session(
        uid,
        track_id=7,
        session_type=session_type,
        started_at=started_at,
        weekend_link=link,
    )
    db.upsert_stint(
        uid,
        0,
        17,
        1,
        laps,
        DegFit(90_000.0, 140.0, 0.0, laps, rmse_ms, 1.0, "fit"),
    )


@pytest.mark.replay
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
                fuel_kg_per_lap=2.0,
                fuel_ms_per_kg=15.0,  # 30 ms/lap, the default fuel prior
                compound=17,
                dt=1.0,
                send_session_end=True,
            )
        ),
        session_uid=practice_uid,
        metadata={"synthetic": False},
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
        metadata={"synthetic": False},
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

    prior = engine.model._deg_prior(7, 17, settings)  # noqa: SLF001

    assert prior.source == "weekend"
    assert prior.deg_ms_per_lap == pytest.approx(140.0, abs=0.01)  # float32 fuel kg
    # The practice base lap time reaches the race prior as the learned base_ms.
    assert prior.base_ms == pytest.approx(db.stints_for_session(practice_uid)[0].base_ms)

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
        prior = engine.model._deg_prior(7, 17, ConfigStore().current())  # noqa: SLF001

        assert prior.source != "weekend"
        assert db.weekend_stints(current_uid, 7, 17) == []


def test_noisy_practice_fit_is_not_a_weekend_prior() -> None:
    db = Database(":memory:")
    day = datetime(2026, 5, 1, 12, 0, tzinfo=UTC).timestamp()
    _prior_db(db, link=0, started_at=day, rmse_ms=1180.0)
    current_uid = 0xF1262002
    db.upsert_session(current_uid, track_id=7, session_type=15, started_at=day + 3_600)
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db, decision_log_fp=io.StringIO())
    engine.state.session_uid = current_uid

    prior = engine.model._deg_prior(7, 17, ConfigStore().current())  # noqa: SLF001

    assert prior.source != "weekend"


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


@pytest.mark.replay
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
        metadata={"synthetic": False},
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


def test_sprint_stint_is_a_weekend_prior_for_the_main_race() -> None:
    db = Database(":memory:")
    link = 0x26000007
    day = datetime(2026, 5, 1, 12, 0, tzinfo=UTC).timestamp()
    _prior_db(db, link=link, started_at=day, session_type=15)
    race_uid = 0xF1262002
    db.upsert_session(
        race_uid, track_id=7, session_type=16, started_at=day + 3_600, weekend_link=link
    )
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db, decision_log_fp=io.StringIO())
    engine.state.session_uid = race_uid

    prior = engine.model._deg_prior(7, 17, ConfigStore().current())  # noqa: SLF001

    assert prior.source == "weekend"


def test_race_2_stint_is_not_a_weekend_prior_for_race_3() -> None:
    db = Database(":memory:")
    link = 0x26000009
    day = datetime(2026, 5, 1, 12, 0, tzinfo=UTC).timestamp()
    _prior_db(db, link=link, started_at=day, session_type=16)
    race_uid = 0xF1262003
    db.upsert_session(
        race_uid, track_id=7, session_type=17, started_at=day + 3_600, weekend_link=link
    )

    assert db.weekend_stints(race_uid, 7, 17) == []


def test_noisy_sprint_fit_is_not_a_weekend_prior() -> None:
    db = Database(":memory:")
    link = 0x26000008
    day = datetime(2026, 5, 1, 12, 0, tzinfo=UTC).timestamp()
    _prior_db(db, link=link, started_at=day, rmse_ms=1180.0, session_type=15)
    race_uid = 0xF1262002
    db.upsert_session(
        race_uid, track_id=7, session_type=16, started_at=day + 3_600, weekend_link=link
    )
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db, decision_log_fp=io.StringIO())
    engine.state.session_uid = race_uid

    prior = engine.model._deg_prior(7, 17, ConfigStore().current())  # noqa: SLF001

    assert prior.source != "weekend"


def test_race_stint_from_another_weekend_is_not_used() -> None:
    db = Database(":memory:")
    day = datetime(2026, 5, 1, 12, 0, tzinfo=UTC).timestamp()
    _prior_db(db, link=301, started_at=day, session_type=15)
    race_uid = 0xF1262002
    db.upsert_session(
        race_uid, track_id=7, session_type=16, started_at=day + 3_600, weekend_link=302
    )

    assert db.weekend_stints(race_uid, 7, 17) == []


def test_normal_weekend_race_still_uses_only_practice_stints() -> None:
    db = Database(":memory:")
    day = datetime(2026, 5, 1, 12, 0, tzinfo=UTC).timestamp()
    _prior_db(db, link=401, started_at=day)
    race_uid = 0xF1262002
    db.upsert_session(
        race_uid, track_id=7, session_type=15, started_at=day + 3_600, weekend_link=401
    )

    stints = db.weekend_stints(race_uid, 7, 17)

    assert len(stints) == 1 and stints[0].deg_ms_per_lap == 140.0


def test_engine_stores_weekend_structure_arriving_after_upsert() -> None:
    db = Database(":memory:")
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db, decision_log_fp=io.StringIO())
    engine.state.session_uid = 0xF1262009
    engine.state.track_id = 7
    engine.state.session_type = 15
    engine.writer._upsert_session(0xF1262009)  # noqa: SLF001
    assert db.session_row(0xF1262009)["weekend_structure"] == ""

    engine.state.weekend_structure = (1, 10, 15, 5, 16)
    engine.writer._write_laps()  # noqa: SLF001

    assert db.session_row(0xF1262009)["weekend_structure"] == "1,10,15,5,16"


def _fuel_engine(db: Database, *, laps: int, practice_link: int, race_link: int):
    """A race engine after a practice stint that burned 2.1 kg per lap."""
    day = datetime(2026, 5, 1, 12, 0, tzinfo=UTC).timestamp()
    _prior_db(db, link=practice_link, started_at=day, laps=laps)
    for n in range(laps + 1):
        db.insert_lap(
            0xF1262001,
            0,
            LapSummary(n, 90_000, 30_000, 30_000, 17, n, 0.0, n > 0, fuel_kg=40.0 - 2.1 * n),
        )
    race_uid = 0xF1262002
    db.upsert_session(
        race_uid, track_id=7, session_type=15, started_at=day + 3_600, weekend_link=race_link
    )
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db, decision_log_fp=io.StringIO())
    engine.state.session_uid = race_uid
    engine.state.track_id = 7
    engine.model._update_model()  # noqa: SLF001
    return engine


def test_weekend_practice_fuel_burn_seeds_the_race() -> None:
    engine = _fuel_engine(Database(":memory:"), laps=8, practice_link=501, race_link=501)

    per_lap, source = engine.model._fuel_per_lap(0)  # noqa: SLF001

    assert source == "weekend"
    assert per_lap == pytest.approx(2.1)


def test_short_or_other_weekend_practice_fuel_burn_is_ignored() -> None:
    default = ConfigStore().current().thresholds["fuel_kg_per_lap_default"]
    short = _fuel_engine(Database(":memory:"), laps=5, practice_link=501, race_link=501)
    assert short.model._fuel_per_lap(0) == (default, "default")  # noqa: SLF001

    db = Database(":memory:")
    db.fold_param(7, 0, "fuel_kg_per_lap", 1.9, weight=5.0)
    other = _fuel_engine(db, laps=8, practice_link=501, race_link=502)
    assert other.model._fuel_per_lap(0) == (1.9, "learned")  # noqa: SLF001


def test_measured_race_fuel_burn_beats_the_weekend_value() -> None:
    engine = _fuel_engine(Database(":memory:"), laps=8, practice_link=501, race_link=501)
    engine.model._session_fuel_deltas = [(1.0, 1.5), (2.0, 1.5)]  # noqa: SLF001

    assert engine.model._fuel_per_lap(0) == (1.5, "session")  # noqa: SLF001
