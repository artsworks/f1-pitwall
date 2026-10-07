from __future__ import annotations

import asyncio
import json
import math
import struct
from types import SimpleNamespace

import pytest

from pitwall.calibrate import calibrate
from pitwall.cli import main
from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.debrief import render_debrief, render_debrief_index
from pitwall.derive import (
    SYNTHETIC_UID_TAG,
    InjectSafetyCar,
    Penalty,
    UidRewrite,
    WearScale,
    derive_recording,
    derive_records,
    derived_uid,
    is_synthetic_uid,
)
from pitwall.digest import DIGEST_VERSION
from pitwall.engine import build_engine, run_replay
from pitwall.ingest import ingest_recordings
from pitwall.learned import format_learned, learned_state
from pitwall.model.deg import DegFit
from pitwall.net.recording import RecordingReader
from pitwall.protocol.header import (
    HEADER_SIZE,
    PACKET_SIZES,
    PacketId,
    parse_header,
)
from pitwall.protocol.packets import (
    SESSION_SAFETY_CAR_STATUS_OFFSET,
    car_field_offset,
    parse,
)
from pitwall.state.lap import LapSummary
from pitwall.store.db import Database, _uid_to_sql
from pitwall.tune import tune_from_db

from .race_synth import RaceSpec, race_stream
from .synth import pack_packet, write_packet_stream

SOURCE_UID = 0x1234_5678_9ABC_DEF0


def _read_records(path) -> list[tuple[int, bytes]]:
    with RecordingReader(path) as reader:
        return list(reader.records())


def _write_race(path, uid: int, *, laps: int = 6):
    stream = race_stream(
        RaceSpec(
            laps=laps,
            session_uid=uid,
            dt=1.0,
            player_pit_lap=8,
            send_session_end=True,
        )
    )
    for index, (offset_s, payload) in enumerate(stream):
        header = parse_header(payload)
        if header.packet_id != PacketId.LAP_DATA:
            continue
        lap_offset = car_field_offset(PacketId.LAP_DATA, 0, "current_lap_num")
        if payload[lap_offset] != 9:
            continue
        driver_status_offset = car_field_offset(PacketId.LAP_DATA, 0, "driver_status")
        corrected = bytearray(payload)
        corrected[driver_status_offset] = 2
        stream[index] = (offset_s, bytes(corrected))
    return write_packet_stream(
        path,
        stream,
        session_uid=uid,
        metadata={"synthetic": is_synthetic_uid(uid)},
    )


def test_derived_uid_and_signed_sqlite_uid() -> None:
    uid = derived_uid(SOURCE_UID, ["wear_scale=1.5", "inject_sc=3-4"])
    assert uid == derived_uid(SOURCE_UID, ["wear_scale=1.5", "inject_sc=3-4"])
    assert uid != SOURCE_UID
    assert uid >> 40 == SYNTHETIC_UID_TAG
    assert is_synthetic_uid(uid)
    assert is_synthetic_uid(_uid_to_sql(uid))
    assert not is_synthetic_uid(0xDEADBEEF)
    assert not is_synthetic_uid(0x9234_5678_9ABC_DEF0)


def test_derive_mutations_preserve_headers_offsets_and_unrelated_data(tmp_path) -> None:
    cars = {
        car: {
            "tyres_wear": (float(car * 4 + 5), 65.25, 72.0, 88.0),
            "tyres_damage": (car * 5, car * 5 + 30, car * 5 + 60, car * 5 + 90),
        }
        for car in range(22)
    }
    all_cars_damage = pack_packet(
        PacketId.CAR_DAMAGE,
        {"cars": cars},
        session_uid=SOURCE_UID,
        session_time=0.0,
    )
    packets = [
        (0.0, all_cars_damage),
        *race_stream(RaceSpec(laps=6, dt=1.0, session_uid=SOURCE_UID)),
    ]
    packets.append((packets[-1][0] + 0.1, b"short"))
    source = write_packet_stream(
        tmp_path / "source.f1bin",
        packets,
        session_uid=SOURCE_UID,
        metadata={"synthetic": False, "notes": "source"},
    )
    output = tmp_path / "derived.f1bin.zst"
    mutations = [WearScale(1.5), InjectSafetyCar(3, 4), Penalty(3)]
    summary = derive_recording(source, output, mutations)
    original = _read_records(source)
    derived = _read_records(output)

    assert summary.record_count == len(original) + 4
    assert summary.header.session_uid != SOURCE_UID
    assert is_synthetic_uid(summary.header.session_uid)
    assert summary.header.metadata == {
        "synthetic": True,
        "notes": "source",
        "derived_from": str(SOURCE_UID),
        "derived_from_path": str(source),
        "mutations": ["wear_scale=1.5", "inject_sc=3-4", "penalty=3"],
    }
    with RecordingReader(source) as reader:
        source_header = reader.header
    with RecordingReader(output) as reader:
        output_header = reader.header
    assert output_header.packet_format == source_header.packet_format
    assert output_header.config_hash == source_header.config_hash
    assert output_header.game_version == source_header.game_version
    assert output_header.wall_clock_start_us == source_header.wall_clock_start_us

    offsets = [offset for offset, _ in derived]
    assert offsets == sorted(offsets)
    assert derived[-1][1] == b"short"
    valid_headers = [parse_header(payload) for _, payload in derived if len(payload) >= HEADER_SIZE]
    assert all(header.session_uid == summary.header.session_uid for header in valid_headers)

    original_damage = next(
        payload
        for _, payload in original
        if len(payload) == PACKET_SIZES[PacketId.CAR_DAMAGE]
        and parse_header(payload).packet_id == PacketId.CAR_DAMAGE
    )
    derived_damage = next(
        payload
        for _, payload in derived
        if len(payload) == PACKET_SIZES[PacketId.CAR_DAMAGE]
        and parse_header(payload).packet_id == PacketId.CAR_DAMAGE
    )
    for car in range(22):
        wear_offset = car_field_offset(PacketId.CAR_DAMAGE, car, "tyres_wear")
        damage_offset = car_field_offset(PacketId.CAR_DAMAGE, car, "tyres_damage")
        for corner in range(4):
            source_wear = struct.unpack_from("<f", original_damage, wear_offset + corner * 4)[0]
            output_wear = struct.unpack_from("<f", derived_damage, wear_offset + corner * 4)[0]
            assert output_wear == pytest.approx(min(100.0, source_wear * 1.5))
            source_damage = original_damage[damage_offset + corner]
            assert derived_damage[damage_offset + corner] == min(100, round(source_damage * 1.5))

    original_laps = [
        payload
        for _, payload in original
        if len(payload) == PACKET_SIZES[PacketId.LAP_DATA]
        and parse_header(payload).packet_id == PacketId.LAP_DATA
    ]
    derived_laps = [
        payload
        for _, payload in derived
        if len(payload) == PACKET_SIZES[PacketId.LAP_DATA]
        and parse_header(payload).packet_id == PacketId.LAP_DATA
    ]
    assert len(derived_laps) == len(original_laps)
    for before, after in zip(original_laps, derived_laps, strict=True):
        player = parse_header(before).player_car_index
        lap_offset = car_field_offset(PacketId.LAP_DATA, player, "current_lap_num")
        time_offset = car_field_offset(PacketId.LAP_DATA, player, "last_lap_time_ms")
        penalties_offset = car_field_offset(PacketId.LAP_DATA, player, "penalties")
        lap_num = before[lap_offset]
        last_lap = struct.unpack_from("<I", before, time_offset)[0]
        expected = round(last_lap * 1.4) if last_lap and 3 <= lap_num - 1 <= 4 else last_lap
        assert struct.unpack_from("<I", after, time_offset)[0] == expected
        expected_penalty = before[penalties_offset] + (5 if lap_num >= 3 else 0)
        assert after[penalties_offset] == min(255, expected_penalty)

    latest_lap = None
    session_statuses = []
    events = []
    for _, payload in derived:
        if len(payload) < HEADER_SIZE:
            continue
        header = parse_header(payload)
        if header.packet_id == PacketId.LAP_DATA and len(payload) == PACKET_SIZES[header.packet_id]:
            latest_lap = payload[
                car_field_offset(PacketId.LAP_DATA, header.player_car_index, "current_lap_num")
            ]
        elif header.packet_id == PacketId.SESSION:
            expected_status = 1 if latest_lap is not None and 3 <= latest_lap <= 4 else 0
            session_statuses.append(payload[SESSION_SAFETY_CAR_STATUS_OFFSET])
            assert payload[SESSION_SAFETY_CAR_STATUS_OFFSET] == expected_status
        elif header.packet_id == PacketId.EVENT:
            parsed = parse(PacketId.EVENT, payload, header)
            if parsed.code in {"SCAR", "PENA"}:
                events.append(parsed)
                assert len(payload) == 45
    scars = [event for event in events if event.code == "SCAR"]
    assert [event.detail["event_type"] for event in scars] == [0, 1, 2]
    assert [event.detail["safety_car_type"] for event in scars] == [1, 1, 1]
    assert sum(event.code == "PENA" for event in events) == 1
    assert session_statuses and {0, 1} <= set(session_statuses)


def test_malformed_short_and_unrelated_packets_pass_through(tmp_path) -> None:
    malformed = pack_packet(PacketId.CAR_DAMAGE, session_uid=SOURCE_UID)[:-1]
    unrelated = pack_packet(PacketId.PARTICIPANTS, session_uid=SOURCE_UID + 1)
    short = b"bad"
    records = [(0, malformed), (1, unrelated), (2, short)]
    output = list(
        derive_records(
            records,
            [
                UidRewrite(SOURCE_UID, derived_uid(SOURCE_UID, ["wear_scale=2"])),
                WearScale(2),
                InjectSafetyCar(1, 2),
                Penalty(1),
            ],
        )
    )
    assert output == records
    with pytest.raises(ValueError, match="finite and non-negative"):
        WearScale(math.nan)
    zero_source = write_packet_stream(
        tmp_path / "zero.f1bin",
        [(0.0, pack_packet(PacketId.SESSION, session_uid=0))],
        session_uid=0,
    )
    with pytest.raises(ValueError, match="session UID 0"):
        derive_recording(zero_source, tmp_path / "zero-derived.f1bin", [WearScale(1.5)])


def test_database_synthetic_origin_and_stint_filters() -> None:
    db = Database(":memory:")
    columns = {
        row["name"]
        for row in db._conn.execute("PRAGMA table_info(sessions)")  # noqa: SLF001
    }
    assert {"synthetic", "derived_from"} <= columns
    uid = derived_uid(SOURCE_UID, ["inject_sc=3-4"])
    db.upsert_session(uid, track_id=7, session_type=1, started_at=1_000.0)
    db.upsert_session(uid, track_id=7, session_type=1, started_at=1_000.0, synthetic=True)
    db.upsert_session(uid, track_id=7, session_type=1, started_at=1_000.0, synthetic=False)
    db.set_session_origin(
        uid,
        started_at=1_000.0,
        recording_path="derived.f1bin",
        calls_mode="off",
        synthetic=True,
        derived_from=str(SOURCE_UID),
    )
    db.set_session_origin(
        uid,
        started_at=1_000.0,
        recording_path="derived.f1bin",
        calls_mode="off",
        synthetic=False,
    )
    row = db.session_row(uid)
    assert row is not None and row["synthetic"] == 1
    assert row["derived_from"] == str(SOURCE_UID)

    for session_uid, started_at, session_type, synthetic in (
        (101, 1_000.0, 1, False),
        (uid, 1_100.0, 1, True),
        (303, 1_200.0, 15, False),
    ):
        db.upsert_session(
            session_uid,
            track_id=7,
            session_type=session_type,
            started_at=started_at,
            weekend_link=44,
            synthetic=synthetic,
        )
        db.upsert_stint(
            session_uid,
            0,
            17,
            1,
            4,
            DegFit(90_000.0, 120.0, 30.0, 4, 100.0, 0.9, "fit"),
        )
    assert {int(stint["session_uid"]) for stint in db.learning_stints()} == {101, 303}
    weekend = db.weekend_stints(303, 7, 17)
    assert [stint.session_uid for stint in weekend] == [101]


def test_calibration_skips_synthetic_sessions_by_default_and_cli_can_include(
    tmp_path, capsys
) -> None:
    db_path = tmp_path / "calibrate.sqlite"
    db = Database(db_path)
    synthetic_uid = derived_uid(SOURCE_UID, ["inject_sc=3-4"])
    for uid, track_id, synthetic, pace in (
        (201, 7, False, 90_000),
        (synthetic_uid, 7, True, 110_000),
        (202, 8, True, 105_000),
    ):
        db.upsert_session(
            uid,
            track_id=track_id,
            session_type=15,
            started_at=float(uid & 0xFFFF),
            synthetic=synthetic,
        )
        for lap_num in range(1, 5):
            db.insert_lap(
                uid,
                0,
                LapSummary(
                    lap_num,
                    pace + lap_num * 100,
                    30_000,
                    30_000,
                    17,
                    lap_num,
                    10.0,
                    True,
                    [],
                ),
            )

    settings = ConfigStore().current()
    report = calibrate(db, settings)
    tracks = {track["track_id"]: track for track in report["tracks"]}
    assert set(tracks) == {7}
    assert tracks[7]["sessions"] == 1
    assert tracks[7]["synthetic_skipped"] == 1
    included = calibrate(db, settings, track_id=7, include_synthetic=True)["tracks"][0]
    assert included["sessions"] == 2
    assert included["synthetic_skipped"] == 0

    db.close()
    assert (
        main(
            [
                "calibrate",
                "--db",
                str(db_path),
                "--include-synthetic",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    cli_report = json.loads(capsys.readouterr().out)
    assert {track["track_id"] for track in cli_report["tracks"]} == {7, 8}
    assert all(track["synthetic_skipped"] == 0 for track in cli_report["tracks"])


def test_engine_skips_physics_folds_for_tagged_session(tmp_path) -> None:
    param_names: dict[int, set[str]] = {}
    for uid in (SOURCE_UID, derived_uid(SOURCE_UID, ["wear_scale=1.5"])):
        recording = _write_race(tmp_path / f"{uid}.f1bin", uid, laps=10)
        db = Database(":memory:")
        engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
        asyncio.run(run_replay(recording, engine, None))
        param_names[uid] = {param.name for param in db.params_for_track(7)}
        db.close()

    real_names = param_names[SOURCE_UID]
    synthetic_uid = next(uid for uid in param_names if is_synthetic_uid(uid))
    synthetic_names = param_names[synthetic_uid]
    assert "pit_loss_green_ms" in real_names
    assert "fuel_kg_per_lap" in real_names
    assert any(name.startswith("deg_ms_per_lap") for name in real_names)
    assert "pit_loss_green_ms" not in synthetic_names
    assert "fuel_kg_per_lap" not in synthetic_names
    assert not any(
        name.startswith(("deg_ms_per_lap", "base_ms", "deg_fuel_ref")) for name in synthetic_names
    )


def test_ingest_cli_digest_tune_and_provenance_keep_synthetic(
    tmp_path, capsys, monkeypatch
) -> None:
    source = _write_race(tmp_path / "source.f1bin", SOURCE_UID, laps=4)
    output = tmp_path / "derived.f1bin"
    summary = derive_recording(source, output, [InjectSafetyCar(2, 2)])
    uid = summary.header.session_uid
    db_path = tmp_path / "ingest.sqlite"
    db = Database(db_path)
    settings = ConfigStore().current()
    results = ingest_recordings(db, [str(output)], settings, out_dir=tmp_path / "digests")
    assert len(results) == 1 and results[0].status == "ingested"
    assert db.is_ingested(uid, DIGEST_VERSION)
    session = db.session_row(uid)
    assert session is not None and session["synthetic"] == 1
    assert session["derived_from"] == str(SOURCE_UID)
    assert (tmp_path / "digests" / f"{uid}.json").is_file()

    db.insert_call(
        uid,
        {
            "outcome": "fired",
            "call_id": "synthetic-call",
            "rule_id": "synthetic_rule",
            "text": "Synthetic call",
        },
    )
    db.grade_call(uid, "synthetic-call", "synthetic_rule", "good")
    tuned = tune_from_db(db, settings.thresholds)
    assert any(row.rule_id == "synthetic_rule" and row.grades == 1 for row in tuned)
    db.close()

    assert main(["sessions", "--db", str(db_path)]) == 0
    sessions_output = capsys.readouterr().out
    assert "yes" in sessions_output
    assert str(SOURCE_UID) in sessions_output

    db = Database(db_path)
    index_html = render_debrief_index(db)
    debrief_html = render_debrief(db, uid, settings)
    learned = format_learned(learned_state(db, settings))
    assert "synthetic" in index_html and str(SOURCE_UID) in index_html
    assert "synthetic" in debrief_html and f"derived from session UID {SOURCE_UID}" in debrief_html
    assert "1 synthetic, excluded from priors" in learned
    db.close()

    cli_source = write_packet_stream(
        tmp_path / "cli-source.f1bin",
        [
            (
                0.0,
                pack_packet(
                    PacketId.SESSION,
                    {"session_type": 15, "track_id": 7, "total_laps": 8},
                    session_uid=SOURCE_UID,
                ),
            )
        ],
        session_uid=SOURCE_UID,
        metadata={"synthetic": False},
    )
    cli_output = tmp_path / "cli-derived.f1bin"
    assert main(["derive", str(cli_source), str(cli_output), "--wear-scale", "1.5"]) == 0
    cli_text = capsys.readouterr().out
    assert "derived session UID:" in cli_text and "wear_scale=1.5" in cli_text
    with RecordingReader(cli_output) as reader:
        cli_uid = reader.header.session_uid
        assert is_synthetic_uid(cli_uid)
        assert reader.header.metadata["derived_from"] == str(SOURCE_UID)

    chained_output = tmp_path / "chained.f1bin"
    chained = derive_recording(cli_output, chained_output, [WearScale(2)])
    assert chained.header.metadata["derived_from"] == str(cli_uid)

    seed_db = tmp_path / "replay.sqlite"
    assert main(["replay", str(cli_output), "--seed-db", str(seed_db), "--speed", "max"]) == 0
    replay_db = Database(seed_db)
    replay_session = replay_db.session_row(derived_uid(SOURCE_UID, ["wear_scale=1.5"]))
    assert replay_session is not None and replay_session["synthetic"] == 1
    assert replay_session["derived_from"] == str(SOURCE_UID)
    replay_db.close()

    import pitwall.cli as cli_module

    monkeypatch.setattr(
        cli_module.ConfigStore,
        "current",
        lambda _self: SimpleNamespace(recording=SimpleNamespace(directory=str(tmp_path))),
    )
    assert main(["recordings"]) == 0
    assert "synthetic" in capsys.readouterr().out
