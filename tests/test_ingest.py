from __future__ import annotations

from pathlib import Path

from pitwall.cli import main
from pitwall.config.loader import ConfigStore
from pitwall.digest import DIGEST_VERSION
from pitwall.ingest import Ingest, expand_paths, header_calls_mode, ingest_recordings
from pitwall.protocol.header import PACKET_SIZES, PacketId, parse_header
from pitwall.state.lap import LapSummary
from pitwall.store.db import Database

from .race_synth import RaceSpec, race_stream
from .synth import make_packet, write_packet_stream


def test_accepted_and_handlers() -> None:
    ingest = Ingest()
    seen: list[int] = []
    ingest.register(PacketId.SESSION, lambda h, p, t: seen.append(h.packet_id))
    for i in range(3):
        ingest.on_datagram(make_packet(PacketId.SESSION, frame=i), recv_time=float(i))
    census = ingest.census()
    assert census["packets"][str(PacketId.SESSION)]["accepted"] == 3
    assert seen == [PacketId.SESSION] * 3


def test_drops_unsupported_format() -> None:
    ingest = Ingest()
    ingest.on_datagram(make_packet(PacketId.SESSION, packet_format=2025), recv_time=0.0)
    ingest.on_datagram(make_packet(PacketId.SESSION, packet_format=2024), recv_time=0.0)
    census = ingest.census()
    assert census["dropped_unsupported"] == 2
    assert census["packets"] == {}


def test_drops_size_mismatch() -> None:
    ingest = Ingest()
    good = make_packet(PacketId.SESSION)
    ingest.on_datagram(good[:-4], recv_time=0.0)  # truncated body
    ingest.on_datagram(good + b"\x00", recv_time=0.0)  # padded body
    census = ingest.census()
    assert census["packets"][str(PacketId.SESSION)]["dropped_size_mismatch"] == 2
    assert census["packets"][str(PacketId.SESSION)]["accepted"] == 0


def test_drops_malformed() -> None:
    ingest = Ingest()
    ingest.on_datagram(b"\x01\x02\x03", recv_time=0.0)
    assert ingest.census()["dropped_malformed"] == 1


def test_sizes_cover_all_ids() -> None:
    ingest = Ingest()
    for pid, size in PACKET_SIZES.items():
        pkt = make_packet(pid)
        assert len(pkt) == size
        ingest.on_datagram(pkt, recv_time=0.0)
        h = parse_header(pkt)
        assert h.packet_id == pid
    assert sum(e["accepted"] for e in ingest.census()["packets"].values()) == len(PACKET_SIZES)


def test_rate_window() -> None:
    ingest = Ingest()
    for i in range(10):
        ingest.on_datagram(make_packet(PacketId.LAP_DATA, frame=i), recv_time=4.0 + i * 0.1)
    assert ingest.rate_hz(PacketId.LAP_DATA, now=4.9) == 10 / 5.0
    assert ingest.rate_hz(PacketId.LAP_DATA, now=100.0) == 0.0


def _recording(path: Path, uid: int) -> Path:
    spec = RaceSpec(
        laps=5,
        session_uid=uid,
        session_type=15,
        dt=1.0,
        send_session_end=True,
    )
    return write_packet_stream(path, race_stream(spec), session_uid=uid)


def _counts(db: Database) -> tuple[int, ...]:
    return tuple(
        int(db._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: SLF001
        for table in ("sessions", "laps", "stints", "ingested", "outcomes")
    )


def test_expand_paths_directories_and_globs(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    first = tmp_path / "first.f1bin"
    second = nested / "second.f1bin"
    compressed = nested / "third.f1bin.zst"
    for path in (first, second, compressed):
        path.write_bytes(b"fixture")

    assert expand_paths([str(tmp_path)]) == sorted(
        [first.resolve(), second.resolve(), compressed.resolve()], key=str
    )
    assert expand_paths([str(nested / "*.f1bin*")]) == sorted(
        [second.resolve(), compressed.resolve()], key=str
    )
    assert expand_paths([str(first), str(first)]) == [first.resolve()]


def test_header_calls_mode_distrusts_legacy_off() -> None:
    # recorders before `speech_enabled` spoke every call with speech.enabled: false
    assert header_calls_mode({"calls_mode": "off"}) == "on"
    assert header_calls_mode({"calls_mode": "off", "quiet": True}) == "off"
    assert header_calls_mode({"calls_mode": "off", "speech_enabled": False}) == "off"
    assert header_calls_mode({"calls_mode": "on"}) == "on"
    assert header_calls_mode({}) == ""


def test_ingest_relabels_legacy_off_header(tmp_path: Path) -> None:
    db = Database(tmp_path / "learn.sqlite")
    uid = 0xF1261009
    spec = RaceSpec(laps=5, session_uid=uid, session_type=15, dt=1.0, send_session_end=True)
    recording = write_packet_stream(
        tmp_path / "legacy.f1bin",
        race_stream(spec),
        session_uid=uid,
        metadata={"calls_mode": "off"},
    )
    settings = ConfigStore().current()
    ingest_recordings(db, [str(recording)], settings, out_dir=tmp_path / "digests")
    row = db.session_row(uid)
    assert row is not None and row["calls_mode"] == "on"


def test_ingest_is_idempotent_and_clears_heartbeat(tmp_path: Path) -> None:
    db = Database(tmp_path / "learn.sqlite")
    recording = _recording(tmp_path / "race.f1bin", 0xF1261001)
    settings = ConfigStore().current()

    first = ingest_recordings(
        db, [str(recording)], settings, calls_mode="off", out_dir=tmp_path / "digests"
    )
    assert len(first) == 1 and first[0].status == "ingested"
    row = db.session_row(first[0].session_uid)
    assert row is not None and row["calls_mode"] == "off"
    assert (tmp_path / "digests" / f"{first[0].session_uid}.json").is_file()
    assert db.read_heartbeat() is None

    counts = _counts(db)
    second = ingest_recordings(db, [str(recording)], settings, out_dir=tmp_path / "digests")
    assert [result.status for result in second] == ["skipped"]
    assert _counts(db) == counts


def test_mirrored_session_is_digest_only(tmp_path: Path) -> None:
    db = Database(tmp_path / "learn.sqlite")
    uid = 0xF1261002
    recording = _recording(tmp_path / "mirrored.f1bin", uid)
    db.upsert_session(uid, track_id=7, session_type=15)
    db.insert_lap(
        uid,
        0,
        LapSummary(1, 90_000, 30_000, 30_000, 17, 0, 10.0, True, []),
    )

    result = ingest_recordings(
        db, [str(recording)], ConfigStore().current(), out_dir=tmp_path / "digests"
    )

    assert len(result) == 1 and result[0].status == "digest_only"
    assert len(db.laps_for(uid)) == 1
    assert db.is_ingested(uid, DIGEST_VERSION)


def test_ingest_isolates_file_errors_and_caps_findings(tmp_path: Path) -> None:
    db = Database(tmp_path / "learn.sqlite")
    bad = tmp_path / "broken.f1bin"
    bad.write_bytes(b"broken recording")
    valid = _recording(tmp_path / "valid.f1bin", 0xF1261003)
    settings = ConfigStore().current()

    results = ingest_recordings(db, [str(bad), str(valid)], settings, out_dir=tmp_path / "digests")

    assert [result.status for result in results] == ["error", "ingested"]
    assert results[0].error
    assert len(results[1].findings) <= int(settings.thresholds["digest_max_findings"])
    assert db.read_heartbeat() is None


def test_failed_ingest_rolls_back_learning_and_retries_complete_recording(tmp_path, monkeypatch):
    import pitwall.digest

    db = Database(tmp_path / "learn.sqlite")
    uid = 0xF1261005
    recording = _recording(tmp_path / "race.f1bin", uid)
    settings = ConfigStore().current()

    def fail_digest(*args, **kwargs):
        raise OSError("injected digest failure")

    with monkeypatch.context() as patch:
        patch.setattr(pitwall.digest, "build_digest", fail_digest)
        result = ingest_recordings(db, [str(recording)], settings, out_dir=tmp_path / "digests")
    assert result[0].status == "error"
    assert _counts(db) == (0, 0, 0, 0, 0)
    assert db.all_params() == []
    retry = ingest_recordings(db, [str(recording)], settings, out_dir=tmp_path / "digests")
    assert retry[0].status == "ingested"
    assert [lap.lap_num for lap in db.laps_for(uid)] == [1, 2, 3, 4]


def test_digest_and_tune_cli_ingest_recording_paths(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    uid = 0xF1261004
    recording = _recording(tmp_path / "cli.f1bin", uid)
    db_path = tmp_path / "cli.sqlite"

    assert main(["digest", str(recording), "--db", str(db_path), "--calls-mode", "off"]) == 0
    digest_output = capsys.readouterr().out
    assert f"session {uid}: ingested" in digest_output
    assert "digest: no sessions in the database" not in digest_output

    db = Database(db_path)
    session = db.session_row(uid)
    assert session is not None and session["calls_mode"] == "off"
    assert db.is_ingested(uid, DIGEST_VERSION)
    db.close()

    assert main(["tune", str(recording), "--db", str(db_path), "--calls-mode", "on"]) == 0
    assert f"{uid} skipped" in capsys.readouterr().out


def test_ingest_relabels_already_ingested_legacy_session(tmp_path: Path) -> None:
    db = Database(tmp_path / "learn.sqlite")
    uid = 0xF126100A
    spec = RaceSpec(laps=5, session_uid=uid, session_type=15, dt=1.0, send_session_end=True)
    recording = write_packet_stream(
        tmp_path / "legacy.f1bin",
        race_stream(spec),
        session_uid=uid,
        metadata={"calls_mode": "off"},
    )
    settings = ConfigStore().current()
    ingest_recordings(db, [str(recording)], settings, out_dir=tmp_path / "digests")
    # an older ingest trusted the header
    db.set_session_origin(uid, started_at=1.0, recording_path=str(recording), calls_mode="off")
    laps_before = _counts(db)

    again = ingest_recordings(db, [str(recording)], settings, out_dir=tmp_path / "digests")
    assert again[0].status == "skipped"
    row = db.session_row(uid)
    assert row is not None and row["calls_mode"] == "on"
    assert _counts(db) == laps_before
