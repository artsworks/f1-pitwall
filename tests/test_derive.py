from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import struct
from types import SimpleNamespace

import pytest

from pitwall.cli import main
from pitwall.clock import VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.engine import build_engine, run_replay
from pitwall.ingest import ingest_recordings
from pitwall.learning.calibrate import calibrate
from pitwall.learning.debrief import render_debrief, render_debrief_index
from pitwall.learning.digest import DIGEST_VERSION
from pitwall.learning.learned import format_learned, learned_state
from pitwall.learning.tune import tune_from_db
from pitwall.model.deg import DegFit
from pitwall.net.derive import (
    Damage,
    DeriveContext,
    InjectRivalPit,
    InjectSafetyCar,
    Penalty,
    TeammatePit,
    UidRewrite,
    WearScale,
    derive_recording,
    derive_records,
)
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
from pitwall.state.session import Damage as DamageView
from pitwall.state.session import Snapshot
from pitwall.store.db import Database, _uid_to_sql
from pitwall.store.uids import SYNTHETIC_UID_TAG, derived_uid, is_synthetic_uid
from pitwall.strategy.battle import Episode

from .race_synth import RaceSpec, race_stream
from .synth import pack_packet, write_packet_stream

SOURCE_UID = 0x1234_5678_9ABC_DEF0


def test_damage_labels_distinguish_all_mutation_parameters() -> None:
    ops = [Damage(1, 60, 4), Damage(1, 100, 4), Damage(1, 60, 3), Damage(2, 60, 4)]
    assert ops[1].label == "damage=1:wing=100:positions=4"
    assert len({derived_uid(SOURCE_UID, [op.label]) for op in ops}) == len(ops)


@pytest.mark.parametrize(
    "transient", [{"traffic": 1}, {"tyre_inner_c": 200.0}, {"sc_status": 1}, {"valid": False}]
)
def test_engine_damage_signal_excludes_transient_laps(transient: dict[str, object]) -> None:
    db = Database(":memory:")
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    fit = DegFit(90_000.0, 100.0, 0.0, 10, 100.0, 0.9, "prior")
    lap = LapSummary(2, 95_000, 30_000, 30_000, 17, 2, 10.0, True)
    snap = Snapshot(now=1.0, race_phase="racing", lap_num=3, tyre_age_laps=3)
    engine.state.laps.append(dataclasses.replace(lap, **transient))
    assert engine._plan_damage(snap, fit) == ("", 0.0, 0.0)
    engine.state.laps[-1] = lap
    assert engine._plan_damage(snap, fit) == ("pace", 4800.0, 0.0)
    db.close()


def test_engine_damage_signal_rebuilds_and_clears_after_stop() -> None:
    db = Database(":memory:")
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    engine._fresh_prior = DegFit(90_000.0, 100.0, 0.0, 10, 100.0, 0.9, "prior")
    snap = Snapshot(
        now=1.0,
        race_phase="racing",
        lap_num=1,
        laps_remaining=16,
        tyre_visual=17,
        tyre_age_laps=0,
        laps_of_pace=30.0,
        pit_loss_s=20.0,
        pit_loss_source="default",
        position=5,
        grid_position=5,
    )
    engine._plan(snap)
    damaged = dataclasses.replace(snap, damage=DamageView(front_left_wing=60))
    result = engine._plan(damaged)
    assert result.plan_switch_reason == "invalid"
    assert result.plan_switched_from == "A"
    assert result.active_plan == "B"
    assert engine._plan_damage(damaged, engine._fresh_prior) == ("damage", 1200.0, 5.0)
    repaired = dataclasses.replace(snap, lap_num=3, num_pit_stops=1)
    assert engine._plan_damage(repaired, engine._fresh_prior) == ("", 0.0, 0.0)
    db.close()


def test_engine_worsening_damage_recomputes_without_invalid_switch(monkeypatch) -> None:
    db = Database(":memory:")
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    engine._fresh_prior = DegFit(90_000.0, 100.0, 0.0, 10, 100.0, 0.9, "prior")
    snap = Snapshot(
        now=1.0,
        race_phase="racing",
        lap_num=1,
        laps_remaining=16,
        tyre_visual=17,
        laps_of_pace=30.0,
        pit_loss_s=20.0,
        pit_loss_source="default",
    )
    engine._plan(snap)
    damaged = dataclasses.replace(snap, damage=DamageView(front_left_wing=60))
    initial = engine._plan(damaged)
    count = engine.plan_tracker.state.switch_count
    key = engine._plan_key
    calls = []
    tracker_type = type(engine.plan_tracker)
    original = tracker_type.update

    def capture(self, inp, th, **kwargs):
        calls.append(inp)
        return original(self, inp, th, **kwargs)

    monkeypatch.setattr(tracker_type, "update", capture)
    worsened = engine._plan(dataclasses.replace(snap, damage=DamageView(front_left_wing=100)))
    assert engine._plan_key != key
    assert len(calls) == 1
    assert calls[0].damage_pace_ms == 2000.0
    assert calls[0].wing_change_s == 5.0
    assert engine.plan_tracker.state.switch_count == count
    assert worsened.active_plan == initial.active_plan
    engine._plan(dataclasses.replace(snap, damage=DamageView(front_left_wing=99)))
    assert len(calls) == 1
    db.close()


def test_damage_mutation_rewrites_player_and_preserves_positions() -> None:
    op = Damage(2, 60, 4)
    ctx = DeriveContext(player_lap=1)
    damage = pack_packet(PacketId.CAR_DAMAGE, session_uid=SOURCE_UID)
    assert op.apply(damage, ctx) == [damage]
    ctx.player_lap = 2
    changed = op.apply(damage, ctx)[0]
    for name in ("front_left_wing_damage", "front_right_wing_damage"):
        assert changed[car_field_offset(PacketId.CAR_DAMAGE, 0, name)] == 60
    lap = bytearray(pack_packet(PacketId.LAP_DATA, session_uid=SOURCE_UID))
    offsets = [car_field_offset(PacketId.LAP_DATA, i, "car_position") for i in range(22)]
    for i, offset in enumerate(offsets):
        lap[offset] = i + 1
    changed = op.apply(bytes(lap), ctx)[0]
    assert changed[offsets[0]] == 5
    assert sorted(changed[o] for o in offsets) == list(range(1, 23))
    assert [changed[o] for o in offsets[1:5]] == [1, 2, 3, 4]
    assert op.apply(b"", ctx) == [b""]
    with pytest.raises(ValueError):
        Damage(0)


def test_damage_mutation_stops_rewriting_wing_after_player_pit_stop() -> None:
    op = Damage(1, 100, 2)
    lap_before_stop = bytearray(
        pack_packet(
            PacketId.LAP_DATA,
            {"cars": {0: {"current_lap_num": 2, "num_pit_stops": 0}}},
            session_uid=SOURCE_UID,
        )
    )
    positions = [car_field_offset(PacketId.LAP_DATA, i, "car_position") for i in range(22)]
    for i, offset in enumerate(positions):
        lap_before_stop[offset] = i + 1
    lap_after_stop = bytearray(lap_before_stop)
    lap_after_stop[car_field_offset(PacketId.LAP_DATA, 0, "num_pit_stops")] = 1
    damage = pack_packet(
        PacketId.CAR_DAMAGE,
        {"cars": {0: {"front_left_wing_damage": 0, "front_right_wing_damage": 0}}},
        session_uid=SOURCE_UID,
    )
    records = list(
        derive_records(
            [
                (0, bytes(lap_before_stop)),
                (1, damage),
                (2, bytes(lap_after_stop)),
                (3, damage),
            ],
            [op],
        )
    )

    left_wing = car_field_offset(PacketId.CAR_DAMAGE, 0, "front_left_wing_damage")
    assert records[1][1][left_wing] == 100
    assert records[3][1][left_wing] == 0
    assert records[2][1][positions[0]] == 3
    assert sorted(records[2][1][offset] for offset in positions) == list(range(1, 23))


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
            pit_lane_loss_ms=20_000,
            send_session_end=True,
        )
    )
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


def test_teammate_pit_changes_only_target_lap_in_lap_packets() -> None:
    participants = pack_packet(
        PacketId.PARTICIPANTS,
        {
            "num_active_cars": 21,
            "cars": {20: {"team_id": 3}, 21: {"team_id": 3}},
        },
        session_uid=SOURCE_UID,
        player=21,
    )

    def lap_packet(lap: int, driver_status: int, pit_status: int) -> bytes:
        return pack_packet(
            PacketId.LAP_DATA,
            {
                "cars": {
                    21: {
                        "current_lap_num": lap,
                        "driver_status": driver_status,
                        "pit_status": pit_status,
                        "lap_distance": 1000.0,
                        "total_distance": 10_000.0 + lap,
                    },
                    20: {
                        "lap_distance": 100.0,
                        "total_distance": 100.0,
                        "driver_status": 4,
                        "pit_status": 0,
                    },
                }
            },
            session_uid=SOURCE_UID,
            player=21,
        )

    source_packets = [
        participants,
        lap_packet(10, 2, 0),
        lap_packet(11, 2, 0),
        lap_packet(11, 4, 0),
        lap_packet(11, 4, 1),
        lap_packet(12, 4, 0),
    ]
    derived = [
        payload for _, payload in derive_records(enumerate(source_packets), [TeammatePit(11)])
    ]
    assert derived[0] == participants
    assert derived[1] == source_packets[1]
    assert derived[3] == source_packets[3]
    assert derived[5] == source_packets[5]

    for payload, expected_pit_status in ((derived[2], 0), (derived[4], 1)):
        lap_distance = struct.unpack_from(
            "<f", payload, car_field_offset(PacketId.LAP_DATA, 20, "lap_distance")
        )[0]
        total_distance = struct.unpack_from(
            "<f", payload, car_field_offset(PacketId.LAP_DATA, 20, "total_distance")
        )[0]
        assert lap_distance == pytest.approx(1150.0)
        assert total_distance == pytest.approx(10_011.0 + 150.0)
        assert payload[car_field_offset(PacketId.LAP_DATA, 20, "driver_status")] == 2
        assert payload[car_field_offset(PacketId.LAP_DATA, 20, "pit_status")] == expected_pit_status


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

    assert summary.record_count == len(original) + 5
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
    green_sessions = 0
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
            status = payload[SESSION_SAFETY_CAR_STATUS_OFFSET]
            session_statuses.append(status)
            if latest_lap == 4 and status == 0:
                green_sessions += 1
            else:
                assert status == expected_status
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
    assert green_sessions == 1


@pytest.mark.parametrize("side", ["ahead", "behind"])
def test_inject_rival_pit_flags_the_neighbour_on_the_chosen_lap_only(tmp_path, side) -> None:
    source = _write_race(tmp_path / "source.f1bin", SOURCE_UID, laps=5)
    records = _read_records(source)
    mutated = list(derive_records(records, [InjectRivalPit(3, side)]))
    assert len(mutated) == len(records)
    flagged_laps: set[int] = set()
    for (_, before), (_, after) in zip(records, mutated, strict=True):
        header = parse_header(before)
        if header.packet_id != PacketId.LAP_DATA:
            assert after == before
            continue
        player = header.player_car_index
        lap = before[car_field_offset(PacketId.LAP_DATA, player, "current_lap_num")]
        changed = [
            car
            for car in range(22)
            if after[car_field_offset(PacketId.LAP_DATA, car, "pit_status")]
            != before[car_field_offset(PacketId.LAP_DATA, car, "pit_status")]
        ]
        if not changed:
            continue
        flagged_laps.add(lap)
        position = before[car_field_offset(PacketId.LAP_DATA, player, "car_position")]
        assert [
            before[car_field_offset(PacketId.LAP_DATA, car, "car_position")] for car in changed
        ] == [position + (-1 if side == "ahead" else 1)]
        assert after[car_field_offset(PacketId.LAP_DATA, changed[0], "pit_status")] == 1
    assert flagged_laps <= {3}


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


def test_derive_rejects_source_and_output_path_collisions(tmp_path) -> None:
    source = write_packet_stream(
        tmp_path / "source.f1bin",
        [(0.0, pack_packet(PacketId.SESSION, session_uid=SOURCE_UID))],
        session_uid=SOURCE_UID,
        metadata={"synthetic": False},
    )
    original = source.read_bytes()
    for output in (source, source.with_suffix(".f1bin.zst")):
        with pytest.raises(ValueError, match="output path resolves to the source"):
            derive_recording(source, output, [WearScale(1.5)])
    assert source.read_bytes() == original


@pytest.mark.replay
def test_safety_car_ends_before_the_first_lap_after_its_window(tmp_path) -> None:
    source = write_packet_stream(
        tmp_path / "source.f1bin",
        race_stream(
            RaceSpec(
                laps=10,
                session_uid=SOURCE_UID,
                dt=1.0,
                send_session_end=True,
            )
        ),
        session_uid=SOURCE_UID,
        metadata={"synthetic": False},
    )
    output = tmp_path / "derived.f1bin"
    derived = derive_recording(source, output, [InjectSafetyCar(6, 8)])
    db = Database(":memory:")
    settings = ConfigStore().current()

    results = ingest_recordings(
        db,
        [str(output)],
        settings,
        out_dir=tmp_path / "digests",
    )

    assert results[0].status == "ingested"
    laps = {lap.lap_num: lap for lap in db.laps_for(derived.header.session_uid) if lap.car_idx == 0}
    assert laps[8].sc_status == 1
    assert "safety_car" in laps[8].invalid_reasons
    assert laps[9].valid
    assert laps[9].sc_status == 0
    assert "safety_car" not in laps[9].invalid_reasons
    assert all("flashback" not in lap.invalid_reasons for lap in laps.values())
    db.close()


@pytest.mark.replay
def test_ingest_treats_metadata_only_synthetic_recordings_as_synthetic(tmp_path) -> None:
    recording = write_packet_stream(
        tmp_path / "metadata-only.f1bin",
        race_stream(
            RaceSpec(
                laps=8,
                session_uid=SOURCE_UID,
                dt=1.0,
                send_session_end=True,
            )
        ),
        session_uid=SOURCE_UID,
        metadata={"synthetic": True},
    )
    db = Database(":memory:")
    result = ingest_recordings(
        db,
        [str(recording)],
        ConfigStore().current(),
        out_dir=tmp_path / "digests",
    )

    assert result[0].status == "ingested"
    session = db.session_row(SOURCE_UID)
    assert session is not None and session["synthetic"] == 1
    assert db.all_params() == []
    db.close()


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


@pytest.mark.replay
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


def test_persist_episode_skips_battle_param_folds_for_synthetic_session() -> None:
    db = Database(":memory:")
    engine = build_engine(clock=VirtualClock(), sinks=[], db=db)
    snapshot = Snapshot(now=1.0, track_id=7)
    episode = Episode(
        kind="defend",
        rival_idx=1,
        start_lap=3,
        end_lap=4,
        overtake=False,
        result="held",
    )

    engine.state.session_uid = derived_uid(SOURCE_UID, ["inject_sc=3-4"])
    engine._persist_episode(snapshot, episode)
    assert db.params_for_track(7) == []

    engine.state.session_uid = SOURCE_UID
    engine._persist_episode(snapshot, episode)
    params = db.params_for_track(7)
    assert len(params) == 1
    assert params[0].name == "battle_hold"
    db.close()


@pytest.mark.replay
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
    derive_recording(cli_source, cli_output, [WearScale(1.5)])
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

    import pitwall.cli.recordings as cli_module

    serve_db_path = tmp_path / "replay-serve.sqlite"
    serve_origin_at_start = {}

    async def no_serve(_engine, _hub, _store, coro, **_kwargs) -> None:
        coro.close()
        serve_db = Database(serve_db_path)
        serve_origin_at_start.update(serve_db.session_row(cli_uid) or {})
        serve_db.close()

    monkeypatch.setattr(cli_module, "_serve", no_serve)
    assert (
        main(
            [
                "replay",
                str(cli_output),
                "--seed-db",
                str(serve_db_path),
                "--speed",
                "max",
                "--serve",
            ]
        )
        == 0
    )
    assert serve_origin_at_start["synthetic"] == 1
    assert serve_origin_at_start["derived_from"] == str(SOURCE_UID)
    serve_db = Database(serve_db_path)
    serve_session = serve_db.session_row(cli_uid)
    assert serve_session is not None and serve_session["synthetic"] == 1
    assert serve_session["derived_from"] == str(SOURCE_UID)
    serve_db.close()

    monkeypatch.setattr(
        cli_module.ConfigStore,
        "current",
        lambda _self: SimpleNamespace(recording=SimpleNamespace(directory=str(tmp_path))),
    )
    write_packet_stream(
        tmp_path / "metadata-only.f1bin",
        [(0.0, pack_packet(PacketId.SESSION, session_uid=SOURCE_UID))],
        session_uid=SOURCE_UID,
        metadata={"synthetic": True},
    )
    assert main(["recordings"]) == 0
    recordings_output = capsys.readouterr().out
    assert "synthetic" in recordings_output
    metadata_only_line = next(
        line for line in recordings_output.splitlines() if "metadata-only.f1bin" in line
    )
    assert "synthetic" in metadata_only_line
