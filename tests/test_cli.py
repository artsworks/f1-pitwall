from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import Mock

from pitwall.cli import _handle_https_disconnect, _QuietShutdownTimeout, main

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


def test_shutdown_timeout_log_is_dropped_but_other_errors_kept() -> None:
    quiet = _QuietShutdownTimeout()

    def record(msg: str, *args: object) -> logging.LogRecord:
        return logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, msg, args, None)

    assert not quiet.filter(
        record("Cancel %s running task(s), timeout graceful shutdown exceeded", 0)
    )
    assert quiet.filter(record("Exception in ASGI application"))


def test_stats_command(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    rec = write_synthetic_recording(tmp_path / "s.f1bin", mixed_session_packets(10))
    assert main(["stats", str(rec)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["session_uid"] == 0xDEADBEEF
    assert sum(e["accepted"] for e in out["packets"].values()) == len(mixed_session_packets(10))


def test_replay_and_trim_and_index(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    rec = write_synthetic_recording(tmp_path / "s.f1bin", mixed_session_packets(10))
    assert main(["replay", str(rec), "--speed", "max", "--stats"]) == 0
    capsys.readouterr()

    trimmed = tmp_path / "t.f1bin"
    assert (
        main(["trim", str(rec), "--from-us", "0", "--to-us", "150000", "--out", str(trimmed)]) == 0
    )
    assert trimmed.exists()

    (tmp_path / "s.f1idx").unlink(missing_ok=True)
    assert main(["index", str(rec)]) == 0
    assert (tmp_path / "s.f1idx").exists()


def test_speak_command(capsys) -> None:  # type: ignore[no-untyped-def]
    from pitwall.cli import main

    assert main(["speak", "--engine", "null", "hi"]) == 0
    out = capsys.readouterr().out
    assert "speaker: null" in out
    assert "spoken after" in out


def test_speak_default_text(capsys) -> None:  # type: ignore[no-untyped-def]
    from pitwall.cli import main

    assert main(["speak"]) == 0
    out = capsys.readouterr().out
    assert "spoken after" in out
