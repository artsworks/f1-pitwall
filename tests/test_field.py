from __future__ import annotations

import json
import sqlite3

import pytest

from pitwall.cli import main
from pitwall.config.loader import ConfigStore
from pitwall.derive import is_synthetic_uid
from pitwall.ingest import ingest_recordings
from pitwall.net.recording import RecordingReader
from pitwall.protocol.header import PacketId, parse_header
from pitwall.protocol.packets import parse
from pitwall.store.db import Database
from pitwall.synth.corpus import generate_corpus
from pitwall.synth.field import (
    FieldSpec,
    Priors,
    _build_lap_times,
    load_priors,
    load_priors_json,
    write_field_recording,
)


def test_field_recording_is_deterministic_and_uses_synthetic_uid(tmp_path) -> None:
    spec = FieldSpec(track_id=12, laps=3, seed=42, cars=4, dt=20.0)
    first = write_field_recording(spec, tmp_path / "first")
    second = write_field_recording(spec, tmp_path / "second")
    changed = write_field_recording(
        FieldSpec(track_id=12, laps=3, seed=43, cars=4, dt=20.0), tmp_path / "changed"
    )

    assert first.sha256 == second.sha256
    assert first.spec_hash == second.spec_hash
    assert first.sha256 != changed.sha256
    assert first.session_uid == second.session_uid
    assert is_synthetic_uid(first.session_uid)
    with RecordingReader(first.path) as reader:
        assert reader.header.session_uid == first.session_uid
        assert reader.header.metadata["synthetic"] is True
        assert reader.header.metadata["generator"] == "field"
        assert reader.header.metadata["spec_hash"] == first.spec_hash
        assert reader.header.wall_clock_start_us == 1_700_000_000_000_042


def test_field_packets_keep_grid_stops_status_history_and_track_consistent(tmp_path) -> None:
    spec = FieldSpec(
        track_id=12,
        laps=4,
        cars=20,
        seed=8,
        dt=3.0,
        sc_laps=(2, 3),
        player_stops=((2, 18),),
    )
    race = write_field_recording(spec, tmp_path)
    active_cars = 0
    track_ids: set[int] = set()
    player_rows: list[tuple[int, int, int, int]] = []
    history_stints: tuple[int, int, int] | None = None
    sc_lap_validity: set[int] = set()
    first_positions: dict[int, int] = {}
    grid_positions: dict[int, set[int]] = {}
    event_codes: set[str] = set()
    with RecordingReader(race.path) as reader:
        for _, payload in reader:
            header = parse_header(payload)
            packet = parse(header.packet_id, payload, header)
            if header.packet_id == PacketId.PARTICIPANTS:
                active_cars = packet.num_active_cars
            elif header.packet_id == PacketId.SESSION:
                track_ids.add(packet.track_id)
            elif header.packet_id == PacketId.LAP_DATA:
                for car_index in range(active_cars):
                    car_data = packet.cars[car_index]
                    first_positions.setdefault(car_index, car_data.car_position)
                    grid_positions.setdefault(car_index, set()).add(car_data.grid_position)
                car = packet.cars[0]
                player_rows.append(
                    (
                        car.current_lap_num,
                        car.pit_status,
                        car.num_pit_stops,
                        car.car_position,
                    )
                )
            elif header.packet_id == PacketId.CAR_STATUS:
                car = packet.cars[0]
                assert 0 <= car.actual_tyre_compound <= 255
                assert car.actual_tyre_compound == car.visual_tyre_compound
                assert car.tyres_age_laps <= 255
            elif header.packet_id == PacketId.SESSION_HISTORY and packet.car_idx == 0:
                if packet.num_laps >= 3:
                    sc_lap_validity.update(
                        (
                            packet.laps[1].lap_valid_bit_flags,
                            packet.laps[2].lap_valid_bit_flags,
                        )
                    )
                if packet.num_tyre_stints >= 2:
                    stop = packet.tyre_stints[0]
                    next_stint = packet.tyre_stints[1]
                    history_stints = (
                        packet.num_tyre_stints,
                        stop.end_lap,
                        next_stint.tyre_actual_compound,
                    )
            elif header.packet_id == PacketId.EVENT:
                event_codes.add(packet.code)

    assert active_cars == 20
    assert track_ids == {12}
    assert len(first_positions) == 20
    assert all(len(positions) == 1 for positions in grid_positions.values())
    assert first_positions == {
        car_index: next(iter(positions)) for car_index, positions in grid_positions.items()
    }
    assert sc_lap_validity == {1}
    assert any(pit == 1 for _, pit, _, _ in player_rows)
    assert any(pit == 2 and count == 1 for _, pit, count, _ in player_rows)
    assert any(lap > 2 and count == 1 for lap, _, count, _ in player_rows)
    assert history_stints == (2, 2, 18)
    assert {"LGOT", "SCAR", "CHQF", "SEND"} <= event_codes


def test_safety_car_bunching_shrinks_large_gaps() -> None:
    spec = FieldSpec(
        track_id=7,
        laps=3,
        cars=20,
        seed=2,
        pace_spread_ms=10_000,
        lap_noise_ms=0,
        sc_laps=(1, 1),
    )
    lap_times, _ = _build_lap_times(spec)
    green_times, _ = _build_lap_times(
        FieldSpec(
            track_id=7,
            laps=3,
            cars=20,
            seed=2,
            pace_spread_ms=10_000,
            lap_noise_ms=0,
        )
    )
    first_lap_spread = float(lap_times[:, 0].max() - lap_times[:, 0].min())
    green_spread = float(green_times[:, 0].max() - green_times[:, 0].min())
    assert first_lap_spread <= green_spread
    assert first_lap_spread < 16_000


def test_load_priors_reads_scoped_values_and_defaults_missing_fields(tmp_path) -> None:
    db_path = tmp_path / "priors.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE model_params (track_id INT, compound INT, name TEXT, value REAL)"
        )
        connection.executemany(
            "INSERT INTO model_params VALUES (?,?,?,?)",
            [
                (7, 17, "base_ms@20L", 91_000.0),
                (7, 17, "deg_ms_per_lap@20L", 85.0),
                (7, 17, "deg_fuel_ref_ms_per_lap@20L", 25.0),
                (7, 17, "fuel_ms_per_lap@20L", 30.0),
                (7, 0, "fuel_kg_per_lap", 1.5),
                (7, 0, "pit_loss_green_ms", 21_000.0),
            ],
        )

    priors, defaulted = load_priors(db_path, 7, 20)
    assert priors.base_ms == 91_000.0
    assert priors.deg_ms_per_lap[17] == 90.0
    assert priors.fuel_kg_per_lap == 1.5
    assert priors.fuel_ms_per_kg == 20.0
    assert priors.pit_loss_green_ms == 21_000.0
    assert "pit_loss_sc_ms" in defaulted
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM model_params").fetchone()[0] == 6


def test_load_priors_json_reads_fields_and_reports_defaults(tmp_path) -> None:
    path = tmp_path / "priors.json"
    path.write_text(
        json.dumps(
            {
                "race_laps": 18,
                "priors": {
                    "base_ms": 92_000,
                    "deg_ms_per_lap": {"16": 120, "17": 75, "18": 50},
                    "fuel_kg_per_lap": 1.6,
                    "fuel_ms_per_kg": 28,
                    "start_fuel_kg": 100,
                    "pit_loss_green_ms": 23_000,
                    "pit_loss_sc_ms": 8_500,
                    "pit_loss_vsc_ms": 12_500,
                },
            }
        ),
        encoding="utf-8",
    )

    priors, defaulted = load_priors_json(path)
    assert priors == Priors(
        base_ms=92_000,
        deg_ms_per_lap={16: 120, 17: 75, 18: 50},
        fuel_kg_per_lap=1.6,
        fuel_ms_per_kg=28,
        start_fuel_kg=100,
        pit_loss_green_ms=23_000,
        pit_loss_sc_ms=8_500,
        pit_loss_vsc_ms=12_500,
    )
    assert defaulted == []


@pytest.mark.slow
def test_generated_race_ingestion_does_not_fold_model_priors(tmp_path) -> None:
    race = write_field_recording(
        FieldSpec(track_id=7, laps=2, cars=4, seed=3, dt=20.0), tmp_path / "recordings"
    )
    db = Database(tmp_path / "learning.db")
    try:
        ingest_recordings(
            db,
            [str(race.path)],
            ConfigStore().current(),
            out_dir=tmp_path / "digests",
        )
        assert db.session_row(race.session_uid)["synthetic"] == 1
        assert db._conn.execute("SELECT COUNT(*) FROM model_params").fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.slow
def test_generate_corpus_serial_and_parallel_are_identical(tmp_path) -> None:
    specs = [FieldSpec(track_id=7, laps=2, cars=4, seed=seed, dt=20.0) for seed in (3, 4)]
    serial = generate_corpus(specs, tmp_path / "serial", jobs=1)
    parallel = generate_corpus(specs, tmp_path / "parallel", jobs=2)

    assert [(race.seed, race.sha256, race.session_uid) for race in serial] == [
        (race.seed, race.sha256, race.session_uid) for race in parallel
    ]
    assert len((tmp_path / "serial" / "manifest.jsonl").read_text().splitlines()) == 2
    assert len((tmp_path / "parallel" / "manifest.jsonl").read_text().splitlines()) == 2


def test_generate_cli_writes_recording_and_manifest(tmp_path, capsys) -> None:
    out_dir = tmp_path / "generated"
    result = main(
        [
            "generate",
            "--track",
            "7",
            "--laps",
            "2",
            "--cars",
            "4",
            "--seed",
            "5",
            "--jobs",
            "1",
            "--out",
            str(out_dir),
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "uid=0x" in output
    assert "sha256=" in output
    assert len(list(out_dir.glob("*.f1bin.zst"))) == 1
    assert len((out_dir / "manifest.jsonl").read_text().splitlines()) == 1
