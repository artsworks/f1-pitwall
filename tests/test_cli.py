from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pitwall.cli import RecordingNotFoundError, main
from pitwall.cli.common import _resolve_recording
from pitwall.cli.serve import _handle_https_disconnect
from pitwall.net.recording import compress_recording

from .synth import mixed_session_packets, write_synthetic_recording


def test_windows_https_disconnect_only_ignores_proactor_peer_resets() -> None:
    loop = Mock(spec=asyncio.AbstractEventLoop)
    expected = {
        "message": "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)",
        "exception": ConnectionResetError(10054, "connection reset"),
    }
    _handle_https_disconnect(loop, expected)
    loop.default_exception_handler.assert_not_called()

    unexpected = {**expected, "exception": RuntimeError("server failed")}
    _handle_https_disconnect(loop, unexpected)
    loop.default_exception_handler.assert_called_once_with(unexpected)
    other_transport = {**expected, "message": "Exception in callback other_transport"}
    _handle_https_disconnect(loop, other_transport)
    assert loop.default_exception_handler.call_count == 2


def test_stats_command(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    rec = write_synthetic_recording(tmp_path / "s.f1bin", mixed_session_packets(10))
    assert main(["stats", str(rec)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["session_uid"] == 0xDEADBEEF
    assert sum(e["accepted"] for e in out["packets"].values()) == len(mixed_session_packets(10))


def test_replay_and_trim(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    rec = write_synthetic_recording(tmp_path / "s.f1bin", mixed_session_packets(10))
    assert main(["replay", str(rec), "--speed", "max", "--stats"]) == 0
    capsys.readouterr()

    trimmed = tmp_path / "t.f1bin"
    assert (
        main(["trim", str(rec), "--from-us", "0", "--to-us", "150000", "--out", str(trimmed)]) == 0
    )
    assert trimmed.exists()


def test_replay_from_lap_rebuilds_missing_or_stale_index(  # type: ignore[no-untyped-def]
    tmp_path: Path, capsys
) -> None:
    rec = write_synthetic_recording(tmp_path / "s.f1bin", mixed_session_packets(10))
    idx = tmp_path / "s.f1idx"
    idx.unlink(missing_ok=True)
    main(["replay", str(rec), "--from-lap", "99"])
    assert idx.exists()
    assert f"index: rebuilt {idx}" in capsys.readouterr().out

    main(["replay", str(rec), "--from-lap", "99"])
    assert "index: rebuilt" not in capsys.readouterr().out

    old = rec.stat().st_mtime - 60
    os.utime(idx, (old, old))
    main(["replay", str(rec), "--from-lap", "99"])
    assert "index: rebuilt" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["compress", "maintain", "index", "record", "speak", "voices"])
def test_removed_commands(command: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main([command])
    assert exc.value.code == 2


def _recordings_dir(tmp_path: Path) -> tuple[Path, list[Path]]:
    folder = tmp_path / "recordings"
    folder.mkdir()
    older = write_synthetic_recording(folder / "old.f1bin", mixed_session_packets(5))
    plain = write_synthetic_recording(folder / "new.f1bin", mixed_session_packets(5))
    newer = compress_recording(plain, remove=True)
    os.utime(older, (1_000, 1_000))
    os.utime(newer, (2_000, 2_000))
    return folder, [newer, older]


def test_resolve_recording(tmp_path: Path) -> None:
    folder, (newer, older) = _recordings_dir(tmp_path)
    settings = SimpleNamespace(recording=SimpleNamespace(directory=str(folder)))
    assert _resolve_recording(None, settings) == newer  # type: ignore[arg-type]
    assert _resolve_recording("latest", settings) == newer  # type: ignore[arg-type]
    assert _resolve_recording("0", settings) == newer  # type: ignore[arg-type]
    assert _resolve_recording("1", settings) == older  # type: ignore[arg-type]
    assert _resolve_recording("old.f1bin", settings) == older  # type: ignore[arg-type]
    assert _resolve_recording("old", settings) == older  # type: ignore[arg-type]
    assert _resolve_recording(str(older), settings) == older  # type: ignore[arg-type]
    for missing in ("2", "nope.f1bin"):
        with pytest.raises(RecordingNotFoundError):
            _resolve_recording(missing, settings)  # type: ignore[arg-type]
    empty = SimpleNamespace(recording=SimpleNamespace(directory=str(tmp_path / "none")))
    with pytest.raises(RecordingNotFoundError):
        _resolve_recording(None, empty)  # type: ignore[arg-type]


def _use_dirs(monkeypatch, recordings: Path, db_path: Path | None = None) -> None:  # type: ignore[no-untyped-def]
    import pitwall.cli.learn as cli_learn
    import pitwall.cli.recordings as cli_recordings
    import pitwall.cli.serve as cli_serve
    import pitwall.cli.voice as cli_voice

    base = cli_recordings.ConfigStore
    overrides: dict[str, object] = {"recording": {"directory": str(recordings)}}
    if db_path is not None:
        overrides["persistence"] = {"enabled": True, "path": str(db_path)}

    class _Store(base):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **k):  # type: ignore[no-untyped-def]
            super().__init__(overrides=overrides)

    for cli_module in (cli_learn, cli_recordings, cli_serve, cli_voice):
        monkeypatch.setattr(cli_module, "ConfigStore", _Store)


def test_recordings_list_and_stats_by_index(  # type: ignore[no-untyped-def]
    tmp_path: Path, monkeypatch, capsys
) -> None:
    folder, (newer, older) = _recordings_dir(tmp_path)
    _use_dirs(monkeypatch, folder)
    assert main(["recordings"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[2].split()[:2] == ["0", newer.name]
    assert lines[3].split()[:2] == ["1", older.name]
    assert str(0xDEADBEEF) in lines[2]

    assert main(["stats", "1"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["file"] == str(older)
    assert f"recording: {older}" in captured.err

    assert main(["stats", "7"]) == 1
    assert "no recording #7" in capsys.readouterr().out


def test_sessions_command(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    from pitwall.store.db import Database

    db_path = tmp_path / "s.sqlite"
    db = Database(db_path)
    db.upsert_session(11, track_id=3, session_type=15, started_at=1_000.0)
    db.upsert_session(22, track_id=7, session_type=10, started_at=2_000.0, recording_path="x.f1bin")
    db.close()
    assert main(["sessions", "--db", str(db_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].split()[0] == "22" and lines[1].endswith("x.f1bin")
    assert lines[2].split()[0] == "11"


def test_voice_say(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["voice", "say", "--engine", "null", "hi"]) == 0
    out = capsys.readouterr().out
    assert "speaker: null" in out
    assert "spoken after" in out


def test_voice_say_default_text(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["voice", "say", "--engine", "null"]) == 0
    assert "spoken after" in capsys.readouterr().out


def test_voice_defaults_to_list(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["voice"]) == 0
    assert "voices in" in capsys.readouterr().out


def test_voice_grammar(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main(["voice", "grammar"]) == 0
    assert "<grammar" in capsys.readouterr().out
