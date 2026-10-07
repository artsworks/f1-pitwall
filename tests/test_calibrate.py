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

pytestmark = pytest.mark.slow


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


def test_calibration_does_not_write_rank_deficient_pace() -> None:
    db = Database(":memory:")
    db.upsert_session(1, track_id=7, session_type=1)
    db.set_param(7, 17, "deg_ms_per_lap", 120, 5)
    for age in range(1, 8):
        db.insert_lap(
            1,
            0,
            LapSummary(
                age, 90_000 + 100 * age, 30_000, 30_000, 17, age, 0, True, [], fuel_kg=20 - age
            ),
        )
    report = calibrate(db, ConfigStore().current(), track_id=7)["tracks"][0]
    assert not report["k_identifiable"]
    assert not any(w["name"] in ("base_ms", "deg_ms_per_lap") for w in report["writes"])
    assert db.get_param(7, 17, "deg_ms_per_lap").value == 120


def test_calibration_preserves_separate_race_distances_and_practice() -> None:
    db = Database(":memory:")
    for uid, distance, deg in ((1, 13, 250), (2, 52, 90), (3, 0, 120)):
        db.upsert_session(uid, track_id=7, session_type=15 if distance else 1)
        db.set_session_total_laps(uid, distance)
        for age in range(1, 8):
            rate = 2 if distance == 52 else 1
            fuel = 25 - rate * age + 0.2 * (age % 2)
            db.insert_lap(
                uid,
                0,
                LapSummary(
                    age,
                    round(90_000 + deg * age + 35 * fuel),
                    30_000,
                    30_000,
                    17,
                    age,
                    0,
                    True,
                    [],
                    fuel_kg=fuel,
                ),
            )
    calibrate(db, ConfigStore().current(), track_id=7)
    for name, expected in (
        ("deg_ms_per_lap@13L", 250),
        ("deg_ms_per_lap@52L", 90),
        ("deg_ms_per_lap", 120),
    ):
        assert db.get_param(7, 17, name).value == pytest.approx(expected, abs=0.1)
    assert db.get_param(7, 17, "fuel_ms_per_lap@13L").value == pytest.approx(35, abs=0.1)
    assert db.get_param(7, 17, "fuel_ms_per_lap@52L").value == pytest.approx(70, abs=0.1)
    assert db.get_param(7, 17, "deg_fuel_ref_ms_per_lap@52L").value == pytest.approx(70, abs=0.1)


def test_unknown_race_length_does_not_replace_practice_priors() -> None:
    db = Database(":memory:")
    db.upsert_session(1, track_id=7, session_type=15)
    db.set_param(7, 17, "deg_ms_per_lap", 120, 5)
    for age in range(1, 8):
        fuel = 25 - age + 0.2 * (age % 2)
        db.insert_lap(
            1,
            0,
            LapSummary(
                age,
                round(90_000 + 250 * age + 35 * fuel),
                30_000,
                30_000,
                17,
                age,
                0,
                True,
                [],
                fuel_kg=fuel,
            ),
        )
    report = calibrate(db, ConfigStore().current(), track_id=7)["tracks"][0]
    assert report["compounds"][17]["status"] == "race length unknown"
    assert db.get_param(7, 17, "deg_ms_per_lap").value == 120


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
        ers_deployed_j_per_lap=500_000.0,
        dt=1.0,
        send_session_end=True,
    )
    path = write_packet_stream(
        tmp_path / "inter.f1bin",
        race_stream(spec),
        session_uid=9026,
        metadata={"synthetic": False},
    )
    result = ingest_recordings(db, [str(path)], settings, out_dir=tmp_path / "digests")
    assert result[0].status == "ingested"

    track = calibrate(db, settings, track_id=7)["tracks"][0]
    assert track["fuel_burn_n"] > 0
    assert track["compounds"][7]["thermal"] is not None
    assert track["energy"]["energy_deployed_j_p50"] == pytest.approx(500_000.0)
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
