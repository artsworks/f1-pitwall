from __future__ import annotations

import struct
from pathlib import Path

import pytest
import yaml

from pitwall.config.loader import ConfigStore
from pitwall.net.derive import DriveThrough, derive_records
from pitwall.protocol.header import HEADER_SIZE, PacketId, parse_header
from pitwall.protocol.packets import car_field_offset
from pitwall.rules.engine import RuleEngine
from pitwall.state.session import Snapshot

from .race_synth import RaceSpec, race_stream, replay_race

_RACE_EVENTS = (
    Path(__file__).resolve().parents[1] / "src/pitwall/config/defaults/rules/race_events.yaml"
)


def _pena(ptype: int, infringement: int, lap: int) -> bytes:
    return struct.pack("<BBBBBBB", ptype, infringement, 0, 255, 255, lap, 0)


def _drive_through(*, served_in_pits: bool = True) -> RaceSpec:
    return RaceSpec(
        laps=7,
        base_ms=12_000,
        penalty_lap=2,
        penalty_type=0,
        penalty_infringement=17,
        unserved_drive_through_laps=(2, 4),
        player_pit_lap=4,
        player_pit_drive_through=served_in_pits,
        lap_events=(
            (3, b"PENA", _pena(3, 15, 3)),
            (4, b"PENA", _pena(3, 16, 4)),
            (5, b"DTSV", struct.pack("<B", 0)),
        ),
    )


@pytest.mark.replay
def test_drive_through_escalates_then_confirms(tmp_path) -> None:
    calls, _ = replay_race(tmp_path, _drive_through())
    fired = {call.rule_id: call for call in calls}

    assert {
        "penalty_pit",
        "penalty_serve_reminder",
        "penalty_serve_this_lap",
        "penalty_served_drive_through",
    } <= fired.keys()
    assert "drive-through" in fired["penalty_serve_this_lap"].text
    assert "tyre" in fired["serve_penalty"].text and "stop" not in fired["serve_penalty"].text
    assert "penalty" not in fired
    assert not {"out_lap_s3_tyres_cold", "out_lap_s3_tyres_ready"} & fired.keys()


@pytest.mark.replay
def test_real_stop_keeps_out_lap_tyre_call(tmp_path) -> None:
    spec = RaceSpec(laps=7, base_ms=12_000, player_pit_lap=4)
    calls, _ = replay_race(tmp_path, spec)

    assert {"out_lap_s3_tyres_cold", "out_lap_s3_tyres_ready"} & {c.rule_id for c in calls}


@pytest.mark.replay
def test_stop_go_served_gives_stop_time(tmp_path) -> None:
    spec = RaceSpec(
        laps=6,
        base_ms=12_000,
        lap_events=(
            (2, b"PENA", _pena(3, 16, 2)),
            (3, b"SGSV", struct.pack("<Bf", 0, 10.0)),
        ),
    )
    calls, _ = replay_race(tmp_path, spec)
    fired = {call.rule_id: call for call in calls}

    assert "10.0" in fired["penalty_served_stop_go"].text
    # A reminder with no unserved penalty in Lap Data stays silent.
    assert "penalty_serve_this_lap" not in fired


def test_drive_through_mutation_issues_then_serves_at_pit_visit() -> None:
    records = [
        (int(t * 1_000_000), payload)
        for t, payload in race_stream(RaceSpec(laps=6, dt=1.0, player_pit_lap=4))
    ]
    derived = [payload for _, payload in derive_records(records, [DriveThrough(2)])]
    events = [
        payload[HEADER_SIZE : HEADER_SIZE + 5]
        for payload in derived
        if parse_header(payload).packet_id == PacketId.EVENT
    ]
    codes = [event[:4] for event in events]
    assert codes.count(b"PENA") == 1 and codes.count(b"DTSV") == 1
    assert codes.index(b"PENA") < codes.index(b"DTSV")
    assert events[codes.index(b"PENA")][4] == 0  # penalty_type drive-through

    offset = car_field_offset(PacketId.LAP_DATA, 0, "num_unserved_drive_through_pens")
    unserved = [
        payload[offset]
        for payload in derived
        if parse_header(payload).packet_id == PacketId.LAP_DATA
    ]
    assert 1 in unserved and unserved[-1] == 0


def _race_rule(rule_id: str) -> dict:
    rules = yaml.safe_load(_RACE_EVENTS.read_text(encoding="utf-8"))["rules"]
    return next(rule for rule in rules if rule["id"] == rule_id)


def _serve_penalty_text(**fields: int) -> str:
    settings = ConfigStore().current()
    engine = RuleEngine(
        list(settings.rules),
        thresholds=settings.thresholds,
        mode=settings.resolved_mindset(),
        staleness_s=settings.engine.staleness_s,
    )
    snap = Snapshot(
        now=100.0,
        session_kind="race",
        session_type=15,
        phase="racing",
        pit_plan="box_now",
        _ages={"lap_data": 0.1},
        **fields,
    )
    candidates = engine.evaluate(snap).candidates
    return next(c.text for c in candidates if c.rule.defn.id == "serve_penalty")


def test_serve_penalty_drive_through_never_mentions_a_stop() -> None:
    text = _serve_penalty_text(unserved_drive_through=1)

    assert "drive-through" in text.lower() and "tyre" in text
    pool = _race_rule("serve_penalty")["severity"][0]["say"]
    assert all("stop" not in line.lower() and "tyre" in line for line in pool)


def test_serve_penalty_stop_go_keeps_stop_wording() -> None:
    text = _serve_penalty_text(unserved_stop_go=1)

    assert "stop" in text and "drive-through" not in text.lower()


def test_penalty_pit_names_no_lap_count() -> None:
    pool = _race_rule("penalty_pit")["say"]

    assert not any(word in line.lower() for line in pool for word in ("three", "laps", "3"))
