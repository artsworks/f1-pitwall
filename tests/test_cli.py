from __future__ import annotations

import json
from pathlib import Path

from pitwall.cli import main

from .synth import mixed_session_packets, write_synthetic_recording


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
