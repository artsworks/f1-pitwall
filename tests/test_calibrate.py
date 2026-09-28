from __future__ import annotations

import json

import pytest
import yaml

from pitwall.calibrate import calibrate, write_overlays
from pitwall.cli import main
from pitwall.config.loader import ConfigStore
from pitwall.ingest import ingest_recordings
from pitwall.propose import propose_thresholds
from pitwall.state.lap import LapSummary
from pitwall.store.db import Database

from .corpus_synth import generate_learning_corpus
from .race_synth import RaceSpec, race_stream
from .synth import write_packet_stream


def _fit_corpus(tmp_path):
    db = Database(tmp_path / "learn.sqlite")
    settings = ConfigStore().current()
    paths = generate_learning_corpus(tmp_path / "recordings")
    results = ingest_recordings(
        db, [str(path) for path in paths], settings, out_dir=tmp_path / "digests"
    )
    assert all(result.status == "ingested" for result in results)
    return db, settings


def test_calibration_recovers_synthetic_fits_and_converges(tmp_path) -> None:
    db, settings = _fit_corpus(tmp_path)

    report = calibrate(db, settings, track_id=7)
    track = report["tracks"][0]

    assert track["k_identifiable"] is True
    assert track["k_ms_per_kg"] == pytest.approx(35.0, abs=1.0)
    assert track["fuel_kg_per_lap"] == pytest.approx(1.7, abs=0.1)
    assert track["converged"] is True
    assert track["history"][-1]["values"]["fuel_ms_per_kg"] == pytest.approx(35.0, abs=1.0)
    assert track["compounds"][17]["deg_ms_per_lap"] == pytest.approx(120.0, abs=1.0)
    assert track["compounds"][17]["thermal"]["thermal_lo_c"] == 85.0
    assert track["compounds"][17]["thermal"]["thermal_hi_c"] == 100.0
    persisted_laps = [
        lap for session in db.sessions_for_track(7) for lap in db.laps_for(int(session["uid"]))
    ]
    assert any(lap.tyre_inner_c > 0 for lap in persisted_laps)
    assert any(lap.tyre_surface_c > lap.tyre_inner_c for lap in persisted_laps)

    energy = track["energy"]
    assert energy["energy_deployed_j_p25"] == pytest.approx(475_000.0)
    assert energy["energy_deployed_j_p50"] == pytest.approx(487_500.0)
    assert energy["energy_deployed_j_p75"] == pytest.approx(518_750.0)
    db.close()


def test_calibration_is_idempotent_and_dry_run_writes_nothing(tmp_path) -> None:
    db, settings = _fit_corpus(tmp_path)
    before = [(p.track_id, p.compound, p.name, p.value, p.weight) for p in db.all_params()]
    preview = calibrate(db, settings, track_id=7, dry_run=True)
    assert preview["tracks"][0]["writes"] == []
    assert [(p.track_id, p.compound, p.name, p.value, p.weight) for p in db.all_params()] == before

    calibrate(db, settings, track_id=7)
    once = [(p.track_id, p.compound, p.name, p.value, p.weight) for p in db.all_params()]
    calibrate(db, settings, track_id=7)
    twice = [(p.track_id, p.compound, p.name, p.value, p.weight) for p in db.all_params()]
    assert twice == once
    db.close()


def test_calibration_gate_prevents_low_weight_writes(tmp_path) -> None:
    db = Database(tmp_path / "low.sqlite")
    db.upsert_session(501, track_id=7, session_type=15, started_at=1.0)
    db.insert_lap(501, 0, LapSummary(1, 90_000, 30_000, 30_000, 17, 0, 10, True, []))

    report = calibrate(db, ConfigStore().current(), track_id=7)

    assert report["tracks"][0]["writes"] == []
    assert db.all_params() == []
    assert report["tracks"][0]["compounds"][17]["status"] == "insufficient (1/3)"
    overlay_dir = tmp_path / "empty-tracks"
    assert write_overlays(report, ConfigStore().current(), overlay_dir) == []
    assert not overlay_dir.exists()


def test_corpus_proposals_are_review_only_and_require_convergence(tmp_path) -> None:
    db, settings = _fit_corpus(tmp_path)
    before = [(p.track_id, p.compound, p.name, p.value, p.weight) for p in db.all_params()]
    result = propose_thresholds(db, settings)
    assert result["review_required"] is True and result["applied"] is False
    assert result["track_overlays"][0]["thresholds"]["tyre_inner_cold_by_compound_c"][17] == 85
    assert [(p.track_id, p.compound, p.name, p.value, p.weight) for p in db.all_params()] == before
    db.close()

    empty = Database(tmp_path / "sparse.sqlite")
    empty.upsert_session(901, track_id=7, session_type=15, started_at=1.0)
    empty.insert_lap(901, 0, LapSummary(1, 90_000, 30_000, 30_000, 17, 0, 10, True, []))
    assert propose_thresholds(empty, settings)["track_overlays"] == []
    empty.close()


def test_calibration_fits_intermediate_window_and_fuel_without_compound_zero(tmp_path) -> None:
    settings = ConfigStore().current()
    db = Database(tmp_path / "inter.sqlite")
    spec = RaceSpec(
        laps=20,
        compound=7,
        session_type=15,
        session_uid=9026,
        tyre_inner_profile=(55.0, 70.0, 70.0, 70.0, 90.0) * 4,
        thermal_window_c=(60.0, 85.0),
        thermal_penalty_ms=800,
        dt=1.0,
        send_session_end=True,
    )
    path = write_packet_stream(tmp_path / "inter.f1bin", race_stream(spec), session_uid=9026)
    result = ingest_recordings(db, [str(path)], settings, out_dir=tmp_path / "digests")
    assert result[0].status == "ingested"

    track = calibrate(db, settings, track_id=7)["tracks"][0]
    assert track["fuel_burn_n"] > 0
    assert track["compounds"][7]["thermal"] is not None
    overlay_dir = tmp_path / "overlays"
    write_overlays({"tracks": [track]}, settings, overlay_dir)
    overlay = yaml.safe_load((overlay_dir / "7.yaml").read_text())
    assert 7 in overlay["thresholds"]["tyre_inner_cold_by_compound_c"]
    db.close()


def test_write_overlay_preserves_existing_keys_and_gates_values(tmp_path) -> None:
    db, settings = _fit_corpus(tmp_path)
    report = calibrate(db, settings, track_id=7)
    overlay_dir = tmp_path / "tracks"
    overlay_dir.mkdir()
    existing = {
        "name": "Keep this name",
        "custom_key": "keep",
        "deg_ms_per_lap": {16: 55},
        "thresholds": {"custom_threshold": 9},
    }
    (overlay_dir / "7.yaml").write_text(yaml.safe_dump(existing))

    paths = write_overlays(report, settings, overlay_dir)

    assert paths == [overlay_dir / "7.yaml"]
    merged = yaml.safe_load(paths[0].read_text())
    assert merged["name"] == "Keep this name"
    assert merged["custom_key"] == "keep"
    assert merged["deg_ms_per_lap"][16] == 55
    assert merged["deg_ms_per_lap"][17] == pytest.approx(120.0)
    assert merged["thresholds"]["custom_threshold"] == 9
    assert merged["thresholds"]["tyre_inner_cold_by_compound_c"][17] == 85.0
    assert merged["thresholds"]["tyre_inner_hot_by_compound_c"][17] == 100.0
    assert 16 not in merged["thresholds"]["tyre_inner_cold_by_compound_c"]
    assert "energy_over_tolerance_j" in merged["thresholds"]
    db.close()


def test_calibrate_cli_ingests_paths_and_emits_history_json(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    paths = generate_learning_corpus(tmp_path / "recordings")
    db_path = tmp_path / "cli.sqlite"

    assert (
        main(
            [
                "calibrate",
                *(str(path) for path in paths),
                "--db",
                str(db_path),
                "--track",
                "7",
                "--json",
            ]
        )
        == 0
    )

    report = json.loads(capsys.readouterr().out)
    assert report["tracks"][0]["sessions"] == 8
    assert report["tracks"][0]["history"]
    db = Database(db_path)
    assert db.ingested_count(7) == 8
    db.close()
