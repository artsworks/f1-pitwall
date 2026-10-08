from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import zstandard

from pitwall.cleanup import apply, plan_cleanup

NOW = 1_800_000_000.0
DAY = 86400.0


def _touch(p: Path, age_days: float, size: int = 10) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    t = NOW - age_days * DAY
    os.utime(p, (t, t))
    return p


def _recording_pair(
    directory: Path, uid: int, source_data: bytes, compressed_data: bytes | None = None
) -> tuple[Path, Path]:
    source = directory / f"session_{uid:016x}_1.f1bin"
    compressed = source.with_name(source.name + ".zst")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(source_data)
    compressed.write_bytes(zstandard.ZstdCompressor().compress(compressed_data or source_data))
    return source, compressed


def _set_pair_age(paths: tuple[Path, Path], age_days: float) -> None:
    mtime = NOW - age_days * DAY
    for path in paths:
        os.utime(path, (mtime, mtime))


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
    f = rec / f"session_{0xAB:016x}_1.f1bin.zst"
    f.parent.mkdir()
    f.write_bytes(b"x")
    os.utime(f, (1.0, 1.0))
    db.mark_ingested(0xAB, 1, str(f))
    args = ["cleanup", "--recordings", str(rec), "--db", str(db_path)]
    assert main(args) == 1  # stdin is not a terminal: nothing deleted
    assert f.exists()
    assert main([*args, "--yes"]) == 0
    assert not f.exists()
    assert "deleted 1 file" in capsys.readouterr().out


def test_symlinked_folder_is_skipped(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "a.json").write_text("{}")
    pw = tmp_path / "pw"
    pw.mkdir()
    (pw / "digests").symlink_to(real)
    plan = plan_cleanup(
        tmp_path / "rec", pw, tmp_path / "voices", set(), 0.0, now=time.time() + 1e6
    )
    assert plan.delete == []


def test_zero_days_keeps_recent_logs_digests_and_cache(tmp_path: Path) -> None:
    rec, home, voices = tmp_path / "rec", tmp_path / "home", tmp_path / "voices"
    for path in (
        rec / "voice-spike-live.jsonl",
        home / "digests" / "1.json",
        voices / ".phrases" / "live.wav",
    ):
        _touch(path, 0.01)
    assert plan_cleanup(rec, home, voices, set(), 0, now=NOW).delete == []


def test_cleanup_skips_changed_file_after_confirmation(tmp_path: Path) -> None:
    file = _touch(tmp_path / "digests" / "1.json", 40)
    plan = plan_cleanup(tmp_path / "rec", tmp_path, tmp_path / "voices", set(), 30, now=NOW)
    file.write_text("updated after preview")
    assert apply(plan) == (0, 0)
    assert file.exists()


def test_cleanup_skips_replacement_with_same_size_and_mtime(tmp_path: Path) -> None:
    file = _touch(tmp_path / "digests" / "1.json", 40)
    stat = file.stat()
    plan = plan_cleanup(tmp_path / "rec", tmp_path, tmp_path / "voices", set(), 30, now=NOW)
    file.rename(file.with_suffix(".saved"))
    file.write_bytes(b"x" * stat.st_size)
    os.utime(file, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert apply(plan) == (0, 0)
    assert file.exists()


def test_cleanup_skips_linked_ancestor_and_replaced_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    file = _touch(home / "digests" / "1.json", 40)
    link = tmp_path / "linked-home"
    link.symlink_to(home, target_is_directory=True)
    assert (
        plan_cleanup(tmp_path / "rec", link, tmp_path / "voices", set(), 30, now=NOW).delete == []
    )
    plan = plan_cleanup(tmp_path / "rec", home, tmp_path / "voices", set(), 30, now=NOW)
    saved = home / "saved"
    file.parent.rename(saved)
    (home / "digests").symlink_to(saved, target_is_directory=True)
    assert apply(plan) == (0, 0)
    assert (saved / "1.json").exists()


@pytest.mark.parametrize("days", [-1, float("nan"), float("inf")])
def test_cleanup_rejects_invalid_age(tmp_path: Path, days: float) -> None:
    with pytest.raises(ValueError, match="--days"):
        plan_cleanup(tmp_path, tmp_path, tmp_path, set(), days, now=NOW)


def test_cleanup_keeps_other_recordings_with_the_same_uid(tmp_path: Path) -> None:
    uid = 0xAB
    old = _touch(tmp_path / f"session_{uid:016x}_1.f1bin.zst", 40)
    other = _touch(tmp_path / f"session_{uid:016x}_2.f1bin.zst", 40)
    changed = _touch(tmp_path / f"session_{uid:016x}_3.f1bin.zst", 40)
    imports = {old.resolve(): NOW, changed.resolve(): NOW - 50 * DAY}
    plan = plan_cleanup(
        tmp_path,
        tmp_path / "home",
        tmp_path / "voices",
        {uid},
        30,
        now=NOW,
        recording_imports=imports,
    )
    assert {c.path for c in plan.delete} == {old}
    assert plan.kept_unlearned == 2
    assert other.exists() and changed.exists()


def test_cleanup_removes_identical_uncompressed_copy_regardless_of_learning(
    tmp_path: Path,
) -> None:
    raw, compressed = _recording_pair(
        tmp_path / "rec", 0xAB, b"raw recording data", compressed_data=b"raw recording data"
    )
    _set_pair_age((raw, compressed), 40)

    plan = plan_cleanup(
        tmp_path / "rec",
        tmp_path / "home",
        tmp_path / "voices",
        set(),
        365,
        now=NOW,
        recording_imports={},
    )

    planned = {candidate.path: candidate.reason for candidate in plan.delete}
    assert planned[raw.absolute()] == "uncompressed copy, the .f1bin.zst has the same data"
    assert compressed.absolute() not in planned


def test_cleanup_keeps_uncompressed_copy_when_compressed_data_differs(
    tmp_path: Path,
) -> None:
    raw, compressed = _recording_pair(
        tmp_path / "rec", 0xAB, b"raw recording data", compressed_data=b"raw recording"
    )
    _set_pair_age((raw, compressed), 40)

    plan = plan_cleanup(
        tmp_path / "rec", tmp_path / "home", tmp_path / "voices", set(), 30, now=NOW
    )

    assert raw.absolute() not in {candidate.path for candidate in plan.delete}
    assert compressed.absolute() not in {candidate.path for candidate in plan.delete}


def test_cleanup_keeps_recent_uncompressed_copy(tmp_path: Path) -> None:
    raw, compressed = _recording_pair(tmp_path / "rec", 0xAB, b"raw recording data")
    _set_pair_age((raw, compressed), 0.01)

    plan = plan_cleanup(tmp_path / "rec", tmp_path / "home", tmp_path / "voices", set(), 0, now=NOW)

    assert raw.absolute() not in {candidate.path for candidate in plan.delete}
    assert compressed.absolute() not in {candidate.path for candidate in plan.delete}
