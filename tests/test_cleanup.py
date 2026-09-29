from __future__ import annotations

import os
from pathlib import Path

from pitwall.cleanup import apply, plan_cleanup

NOW = 1_800_000_000.0
DAY = 86400.0


def _touch(p: Path, age_days: float, size: int = 10) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    t = NOW - age_days * DAY
    os.utime(p, (t, t))
    return p


def test_only_old_learned_recordings_and_caches_are_listed(tmp_path: Path) -> None:
    rec, home, voices = tmp_path / "rec", tmp_path / "home", tmp_path / "voices"
    learned = 0xDB913A6A1919407E
    old = _touch(rec / f"session_{learned:016x}_1.f1bin.zst", 40)
    old_idx = _touch(rec / f"session_{learned:016x}_1.f1idx", 40)
    unlearned = _touch(rec / "session_00000000000000aa_1.f1bin.zst", 40)
    fresh = _touch(rec / f"session_{learned:016x}_2.f1bin.zst", 5)
    live = _touch(rec / f"session_{learned:016x}_3.f1bin", 0)
    log = _touch(rec / "balanced.decisions.jsonl", 40)
    digest = _touch(home / "digests" / "1.json", 40)
    db = _touch(home / "pitwall.sqlite", 40)
    profile = _touch(home / "profile.yaml", 40)
    phrase = _touch(voices / ".phrases" / "a.wav", 2)
    model = _touch(voices / "en.onnx", 40)

    plan = plan_cleanup(rec, home, voices, {learned}, 30, now=NOW)
    listed = {c.path for c in plan.delete}
    assert listed == {old, old_idx, digest, phrase}
    assert plan.kept_unlearned == 1
    assert apply(plan) == (4, 40)
    for kept in (unlearned, fresh, live, log, db, profile, model):
        assert kept.exists()
    assert not old.exists()


def test_cleanup_cli_needs_confirmation(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    from pitwall.cli import main
    from pitwall.store.db import Database

    rec = tmp_path / "rec"
    db_path = tmp_path / "pitwall.sqlite"
    db = Database(str(db_path))
    db.mark_ingested(0xAB, 1, "x")
    f = rec / f"session_{0xAB:016x}_1.f1bin.zst"
    f.parent.mkdir()
    f.write_bytes(b"x")
    os.utime(f, (1.0, 1.0))
    args = ["cleanup", "--recordings", str(rec), "--db", str(db_path)]
    assert main(args) == 1  # stdin is not a terminal: nothing deleted
    assert f.exists()
    assert main([*args, "--yes"]) == 0
    assert not f.exists()
    assert "deleted 1 file" in capsys.readouterr().out
