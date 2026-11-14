from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
import yaml

from pitwall.clock import VirtualClock
from pitwall.config.loader import DEFAULTS_DIR, ConfigStore
from pitwall.engine import build_engine, run_replay
from pitwall.ingest import ingest_recordings
from pitwall.learning.calibrate import calibrate
from pitwall.learning.maintenance import maintain
from pitwall.learning.tune import COOLDOWN_PREFIX, TUNE_COMPOUND, TUNE_TRACK, tune_from_db
from pitwall.model.deg import DegFit
from pitwall.store.db import Database

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


NEW_TRACK = 4
TRUE_DEG_MS = 120
TRUE_FUEL_KG_PER_LAP = 1.7
TRUE_FUEL_MS_PER_KG = 35.0
TRUE_STINT_SLOPE_MS = TRUE_DEG_MS - TRUE_FUEL_MS_PER_KG * TRUE_FUEL_KG_PER_LAP
LEARN_SPEC = {
    "laps": 16,
    "base_ms": 45_000,
    "deg_ms": TRUE_DEG_MS,
    "fuel_kg": 32.0,
    "fuel_kg_per_lap": TRUE_FUEL_KG_PER_LAP,
    "fuel_ms_per_kg": TRUE_FUEL_MS_PER_KG,
    "track_id": NEW_TRACK,
    "compound": 17,
    "dt": 1.0,
    "send_session_end": True,
}


def _replay_session(db: Database, tmp_path: Path, uid: int, **overrides: object):
    """Replay one session of its own weekend, so no weekend prior applies."""
    spec = RaceSpec(**{**LEARN_SPEC, **overrides}, session_uid=uid, weekend_link=uid)
    path = write_packet_stream(
        tmp_path / f"{uid:x}.f1bin",
        race_stream(spec),
        session_uid=uid,
        metadata={"synthetic": False},
    )
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db, decision_log_fp=io.StringIO())
    asyncio.run(run_replay(path, engine))
    return engine


def _track_overlay() -> Path:
    return Path.home() / ".pitwall" / "tracks" / f"{NEW_TRACK}.yaml"


def _race_deg_prior(db: Database) -> DegFit:
    """The tyre prior that a new LEARN_SPEC race starts with."""
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db, decision_log_fp=io.StringIO())
    engine.store.set_track(NEW_TRACK)
    engine.state.session_type = 15
    engine.state.total_laps = int(LEARN_SPEC["laps"])
    return engine.model._deg_prior(NEW_TRACK, 17, engine.store.current())  # noqa: SLF001


def test_first_session_on_new_track_feeds_the_next_session(tmp_path) -> None:
    settings = ConfigStore().current()
    min_weight = float(settings.thresholds["prior_min_weight"])
    assert not (DEFAULTS_DIR / "tracks" / f"{NEW_TRACK}.yaml").exists()
    db = Database(tmp_path / "learn.sqlite")

    _replay_session(db, tmp_path, 0xF1264001, laps=3)
    maintain(db, settings)
    assert not _track_overlay().exists()
    assert not [
        param
        for param in db.params_for_track(NEW_TRACK)
        if param.weight >= min_weight and not param.name.startswith("driver_")
    ]
    assert _race_deg_prior(db).source == "default"

    _replay_session(db, tmp_path, 0xF1264002, player_pit_lap=8, lap_noise_ms=150.0, seed=3)
    assert maintain(db, settings).overlays == 1
    overlay = yaml.safe_load(_track_overlay().read_text())
    assert overlay["fuel_kg_per_lap"] == pytest.approx(TRUE_FUEL_KG_PER_LAP, abs=0.05)
    for deg_ms in overlay.get("deg_ms_per_lap", {}).values():
        assert deg_ms == pytest.approx(TRUE_DEG_MS, abs=40.0)
    fuel = db.get_param(NEW_TRACK, 0, "fuel_kg_per_lap")
    assert fuel is not None and fuel.weight >= min_weight
    assert fuel.value == pytest.approx(TRUE_FUEL_KG_PER_LAP, abs=0.05)
    prior = _race_deg_prior(db)
    assert prior.source == "learned"
    assert prior.deg_ms_per_lap - prior.fuel_ms_per_lap == pytest.approx(
        TRUE_STINT_SLOPE_MS, abs=40.0
    )

    engine = _replay_session(db, tmp_path, 0xF1264003, player_pit_lap=8)
    assert engine.store.current().track.fuel_kg_per_lap == overlay["fuel_kg_per_lap"]
    assert engine.model._fuel_prior.source == "learned"  # noqa: SLF001
    assert engine.model._deg_prior(NEW_TRACK, 17, engine.store.current()).source == "learned"  # noqa: SLF001


GUARD_CHECKS = (
    ("plan_announce", 1, 1),
    ("battle_under_threat", 2, 5),
    ("plan_status", 5, 10),
    ("pit_exit_traffic_race", 9, 9),
    ("plan_invalid", 10, 10),
    ("plan_window_open", 10, 12),
    ("plan_target_lap", 11, 14),
    ("three_to_go", 14, 14),
    ("last_lap", 16, 16),
)


def _guard_results(rows: list[dict]) -> dict[str, bool]:
    return {
        rule_id: any(
            row["rule_id"] == rule_id and row["outcome"] == "fired" and first <= row["lap"] <= last
            for row in rows
        )
        for rule_id, first, last in GUARD_CHECKS
    }


def test_noisy_sessions_do_not_set_back_guard_race(tmp_path) -> None:
    settings = ConfigStore().current()
    guard_uid = 0xF1264020
    default_db = Database(":memory:")
    _replay_session(default_db, tmp_path, guard_uid, player_pit_lap=8)
    default = default_db.calls_for_session(guard_uid)
    default_db.close()
    assert all(_guard_results(default).values())

    db = Database(tmp_path / "learn.sqlite")
    for index in range(3):
        _replay_session(
            db,
            tmp_path,
            0xF1264021 + index,
            player_pit_lap=7 + index,
            lap_noise_ms=250.0,
            seed=10 + index,
        )
        maintain(db, settings)
    assert _race_deg_prior(db).source == "learned"

    _replay_session(db, tmp_path, guard_uid, player_pit_lap=8)
    learned = db.calls_for_session(guard_uid)
    assert _guard_results(learned) == _guard_results(default)
    assert {row["rule_id"] for row in learned if row["outcome"] == "fired"} == {
        row["rule_id"] for row in default if row["outcome"] == "fired"
    }
    db.close()


def test_one_bad_session_does_not_move_priors(tmp_path) -> None:
    settings = ConfigStore().current()
    db = Database(tmp_path / "learn.sqlite")
    for index in range(2):
        _replay_session(
            db,
            tmp_path,
            0xF1264031 + index,
            player_pit_lap=7 + index,
            lap_noise_ms=150.0,
            seed=20 + index,
        )
    maintain(db, settings)
    before = {
        param.name: param.value
        for param in db.params_for_track(NEW_TRACK)
        if param.compound == 17 and param.name.startswith(("deg_ms_per_lap", "base_ms"))
    }
    assert before

    _replay_session(
        db,
        tmp_path,
        0xF1264035,
        laps=12,
        compound=18,
        deg_ms=900,
        fuel_kg=70.0,
        fuel_kg_per_lap=3 * TRUE_FUEL_KG_PER_LAP,
        lap_noise_ms=150.0,
        seed=30,
    )
    maintain(db, settings)
    after = {param.name: param for param in db.params_for_track(NEW_TRACK)}
    for name, value in before.items():
        assert after[name].compound == 17
        assert after[name].value == pytest.approx(value, rel=0.1)
    fuel = db.get_param(NEW_TRACK, 0, "fuel_kg_per_lap")
    assert fuel is not None
    assert fuel.value == pytest.approx(TRUE_FUEL_KG_PER_LAP, abs=0.1)
    assert not [
        param
        for param in db.params_for_track(NEW_TRACK)
        if param.compound == 18 and param.name.startswith("deg_ms_per_lap")
    ]
    overlay = yaml.safe_load(_track_overlay().read_text())
    assert overlay["fuel_kg_per_lap"] == pytest.approx(TRUE_FUEL_KG_PER_LAP, abs=0.1)
    assert 18 not in overlay.get("deg_ms_per_lap", {})
    db.close()
