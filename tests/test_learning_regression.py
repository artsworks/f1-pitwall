from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest

from pitwall.calibrate import calibrate
from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.engine import build_engine, run_replay
from pitwall.ingest import ingest_recordings
from pitwall.store.db import Database
from pitwall.tune import COOLDOWN_PREFIX, TUNE_COMPOUND, TUNE_TRACK, tune_from_db

from .corpus_synth import generate_learning_corpus
from .race_synth import RaceSpec, race_stream
from .synth import write_packet_stream

pytestmark = [pytest.mark.slow, pytest.mark.replay]


def _run_calls(db: Database, recording: Path, uid: int) -> list[dict]:
    engine = build_engine(
        clock=VirtualClock(),
        sinks=[],
        db=db,
        decision_log_fp=io.StringIO(),
    )
    asyncio.run(run_replay(recording, engine))
    return db.calls_for_session(uid)


def _rule_count(rows: list[dict], rule_id: str) -> int:
    return sum(row["rule_id"] == rule_id and row["outcome"] == "fired" for row in rows)


def test_tuned_away_rule_stays_away_after_new_ingest_and_reopen(tmp_path) -> None:
    spec_args = {
        "laps": 36,
        "base_ms": 30_000,
        "deg_ms": 100,
        "wear_pct_per_lap": 3.0,
        "fuel_kg": 65.0,
        "fuel_kg_per_lap": 1.7,
        "compound": 17,
        "dt": 1.0,
        "send_session_end": True,
    }
    baseline_uid = 0xF1263001
    baseline_path = write_packet_stream(
        tmp_path / "baseline.f1bin",
        race_stream(RaceSpec(**spec_args, session_uid=baseline_uid)),
        session_uid=baseline_uid,
        metadata={"synthetic": False},
    )
    baseline_db = Database(":memory:")
    baseline = _run_calls(baseline_db, baseline_path, baseline_uid)
    baseline_count = _rule_count(baseline, "battle_under_threat")
    assert baseline_count >= 2
    baseline_db.close()

    db_path = tmp_path / "learn.sqlite"
    db = Database(db_path)
    for index in range(3):
        db.grade_call(900, f"bad-{index}", "battle_under_threat", "noise")
    settings = ConfigStore().current()
    tuned = tune_from_db(db, settings.thresholds)
    assert next(
        item for item in tuned if item.rule_id == "battle_under_threat"
    ).cooldown_mult == float(settings.thresholds["tune_max_cooldown_mult"])

    first_uid = 0xF1263002
    first_path = write_packet_stream(
        tmp_path / "first.f1bin",
        race_stream(RaceSpec(**spec_args, session_uid=first_uid)),
        session_uid=first_uid,
        metadata={"synthetic": False},
    )
    first_count = _rule_count(_run_calls(db, first_path, first_uid), "battle_under_threat")
    assert 0 < first_count < baseline_count

    training_uid = 0xF1263005
    training_path = write_packet_stream(
        tmp_path / "extra-practice.f1bin",
        race_stream(
            RaceSpec(
                laps=5,
                session_type=1,
                session_uid=training_uid,
                deg_ms=0,
                dt=1.0,
                send_session_end=True,
            )
        ),
        session_uid=training_uid,
        metadata={"synthetic": False},
    )
    ingest_recordings(db, [str(training_path)], settings, out_dir=tmp_path / "digests")
    tuned_again = tune_from_db(db, settings.thresholds)
    assert next(
        item for item in tuned_again if item.rule_id == "battle_under_threat"
    ).cooldown_mult == float(settings.thresholds["tune_max_cooldown_mult"])

    second_uid = 0xF1263003
    second_path = write_packet_stream(
        tmp_path / "second.f1bin",
        race_stream(RaceSpec(**spec_args, session_uid=second_uid)),
        session_uid=second_uid,
        metadata={"synthetic": False},
    )
    second_count = _rule_count(_run_calls(db, second_path, second_uid), "battle_under_threat")
    assert second_count == first_count
    db.close()

    reopened = Database(db_path)
    persisted_uid = 0xF1263004
    persisted_path = write_packet_stream(
        tmp_path / "persisted.f1bin",
        race_stream(RaceSpec(**spec_args, session_uid=persisted_uid)),
        session_uid=persisted_uid,
        metadata={"synthetic": False},
    )
    persisted_count = _rule_count(
        _run_calls(reopened, persisted_path, persisted_uid), "battle_under_threat"
    )
    assert persisted_count == first_count
    persisted_tune = reopened.get_param(
        TUNE_TRACK, TUNE_COMPOUND, COOLDOWN_PREFIX + "battle_under_threat"
    )
    assert persisted_tune is not None
    assert persisted_tune.value == float(settings.thresholds["tune_max_cooldown_mult"])
    reopened.close()


def test_calibrated_prior_removes_false_positive_and_memory_replay_isolated(tmp_path) -> None:
    settings = ConfigStore().current()
    db = Database(tmp_path / "learn.sqlite")
    db.set_param(7, 17, "deg_ms_per_lap", 500.0, 1000.0)
    spec_args = {
        "laps": 20,
        "base_ms": 90_000,
        "deg_ms": 0,
        "wear_pct_per_lap": 0.0,
        "fuel_kg": 45.0,
        "fuel_kg_per_lap": 1.7,
        "fuel_ms_per_kg": 0.0,
        "compound": 17,
        "dt": 1.0,
        "send_session_end": True,
    }
    before_uid = 0xF1263010
    before_path = write_packet_stream(
        tmp_path / "before.f1bin",
        race_stream(RaceSpec(**spec_args, session_uid=before_uid)),
        session_uid=before_uid,
        metadata={"synthetic": False},
    )
    before = _run_calls(db, before_path, before_uid)
    assert _rule_count(before, "plan_window_open") >= 1

    memory_uid = 0xF1263012
    memory_path = write_packet_stream(
        tmp_path / "memory.f1bin",
        race_stream(RaceSpec(**spec_args, session_uid=memory_uid)),
        session_uid=memory_uid,
        metadata={"synthetic": False},
    )
    memory_before_db = Database(":memory:")
    memory_before = _run_calls(memory_before_db, memory_path, memory_uid)
    memory_before_db.close()

    corpus = generate_learning_corpus(
        tmp_path / "true-deg",
        sessions=5,
        laps=8,
        deg_ms=0,
        fuel_ms_per_kg=0.0,
    )
    ingest_recordings(db, [str(path) for path in corpus], settings, out_dir=tmp_path / "digests")
    calibrated = calibrate(db, settings, track_id=7)
    assert calibrated["tracks"][0]["compounds"][17]["deg_ms_per_lap"] == pytest.approx(0.0, abs=5.0)

    after_uid = 0xF1263011
    after_path = write_packet_stream(
        tmp_path / "after.f1bin",
        race_stream(RaceSpec(**spec_args, session_uid=after_uid)),
        session_uid=after_uid,
        metadata={"synthetic": False},
    )
    after = _run_calls(db, after_path, after_uid)
    assert _rule_count(after, "plan_window_open") == 0

    db.close()
    memory_after_db = Database(":memory:")
    memory_after = _run_calls(memory_after_db, memory_path, memory_uid)

    assert memory_after == memory_before
    memory_after_db.close()
