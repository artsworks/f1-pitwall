from __future__ import annotations

import argparse
import subprocess
import sys
from io import StringIO
from pathlib import Path

from pitwall.cli.parser import build_parser
from pitwall.cli.update import cmd_update, update


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _commit_project(directory: Path, version: str) -> None:
    (directory / "pyproject.toml").write_text(
        f'[project]\nname = "pitwall"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    _git("add", "pyproject.toml", cwd=directory)
    _git("commit", "-m", f"version {version}", cwd=directory)


def _clone(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-b", "main", cwd=origin)
    _commit_project(origin, "0.1.0")
    clone = tmp_path / "clone"
    _git("clone", str(origin), str(clone), cwd=tmp_path)
    return origin, clone


def _sync() -> list[str]:
    return [sys.executable, "-c", ""]


def test_update_up_to_date(tmp_path: Path) -> None:
    _, clone = _clone(tmp_path)
    out = StringIO()

    assert update(clone, out=out, sync_cmd=_sync()) == 0

    assert "up to date" in out.getvalue()
    assert "0.1.0" in out.getvalue()


def test_update_pulls_new_version(tmp_path: Path) -> None:
    origin, clone = _clone(tmp_path)
    _commit_project(origin, "0.2.0")
    out = StringIO()

    assert update(clone, out=out, sync_cmd=_sync()) == 0

    assert "0.1.0" in out.getvalue()
    assert "0.2.0" in out.getvalue()
    assert 'version = "0.2.0"' in (clone / "pyproject.toml").read_text(encoding="utf-8")


def test_update_refuses_tracked_changes(tmp_path: Path) -> None:
    origin, clone = _clone(tmp_path)
    _commit_project(origin, "0.2.0")
    (clone / "pyproject.toml").write_text(
        '[project]\nname = "pitwall"\nversion = "0.1.0"\n# local edit\n',
        encoding="utf-8",
    )
    head = _git("rev-parse", "HEAD", cwd=clone)
    out = StringIO()

    assert update(clone, out=out, sync_cmd=_sync()) == 1

    assert "pyproject.toml" in out.getvalue()
    assert "Pitwall did not update" in out.getvalue()
    assert _git("rev-parse", "HEAD", cwd=clone) == head


def test_update_ignores_untracked_files(tmp_path: Path) -> None:
    _, clone = _clone(tmp_path)
    (clone / "personal-notes.txt").write_text("keep me\n", encoding="utf-8")
    out = StringIO()

    assert update(clone, out=out, sync_cmd=_sync()) == 0
    assert "up to date" in out.getvalue()


def test_update_refuses_plain_directory(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    out = StringIO()

    assert update(root, out=out, sync_cmd=_sync()) == 1
    assert "not a git clone" in out.getvalue()


def test_update_refuses_other_project_before_pull_or_sync(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _, clone = _clone(tmp_path)
    (clone / "pyproject.toml").write_text(
        '[project]\nname = "other"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    _git("add", "pyproject.toml", cwd=clone)
    _git("commit", "-m", "rename project", cwd=clone)
    head = _git("rev-parse", "HEAD", cwd=clone)
    status = _git("status", "--porcelain", cwd=clone)
    sync_cmd = [sys.executable, "-c", "raise SystemExit('sync ran')"]
    run = subprocess.run
    invoked: list[str] = []

    def track_run(command, *args, **kwargs):
        if command == ["git", "-C", str(clone), "pull", "--ff-only"]:
            invoked.append("pull")
        elif command == sync_cmd:
            invoked.append("sync")
        return run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", track_run)
    out = StringIO()

    assert update(clone, out=out, sync_cmd=sync_cmd) == 1

    assert "is not a git clone of f1-pitwall" in out.getvalue()
    assert "Updating Pitwall" not in out.getvalue()
    assert invoked == []
    assert _git("rev-parse", "HEAD", cwd=clone) == head
    assert _git("status", "--porcelain", cwd=clone) == status
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_update_reports_sync_failure(tmp_path: Path) -> None:
    _, clone = _clone(tmp_path)
    out = StringIO()

    assert (
        update(
            clone,
            out=out,
            sync_cmd=[sys.executable, "-c", "raise SystemExit(3)"],
        )
        == 1
    )
    assert "uv sync failed" in out.getvalue()


def test_update_parser_uses_cmd_update() -> None:
    args: argparse.Namespace = build_parser().parse_args(["update"])
    assert args.func is cmd_update
