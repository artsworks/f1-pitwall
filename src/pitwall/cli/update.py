from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO


def install_dir() -> Path:
    return Path(__file__).resolve().parents[3]


def _error(out: TextIO, message: str) -> int:
    print(f"update: {message}", file=out)
    return 1


def _git_output(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        encoding="utf-8",
    )


def _version(root: Path) -> str:
    with (root / "pyproject.toml").open("rb") as project_file:
        version = tomllib.load(project_file).get("project", {}).get("version")
    if not isinstance(version, str):
        raise ValueError("pyproject.toml has no project version")
    return version


def update(root: Path, out: TextIO = sys.stdout, sync_cmd: Sequence[str] | None = None) -> int:
    if shutil.which("git") is None:
        return _error(out, "Git is not installed. Install Git, then run the update again.")

    try:
        root = root.resolve()
        top_level = _git_output(root, "rev-parse", "--show-toplevel")
    except OSError as exc:
        return _error(out, f"could not inspect the installation: {exc}")

    if (
        top_level.returncode != 0
        or Path(top_level.stdout.strip()).resolve() != root
        or not (root / "pyproject.toml").is_file()
    ):
        return _error(
            out,
            f"{root} is not a git clone of f1-pitwall, so it cannot update itself. "
            "Reinstall with the installer: https://pitwall.arthurku.com/install.html",
        )

    try:
        status = _git_output(root, "status", "--porcelain", "--untracked-files=no")
    except OSError as exc:
        return _error(out, f"git status failed: {exc}")
    if status.returncode != 0:
        return _error(out, f"git status failed: {status.stderr.strip() or status.stdout.strip()}")
    changed_paths = [line[3:] for line in status.stdout.splitlines()]
    if changed_paths:
        print("update: tracked files have local changes:", file=out)
        for path in changed_paths[:10]:
            print(f"update:   {path}", file=out)
        if len(changed_paths) > 10:
            print(f"update: and {len(changed_paths) - 10} more", file=out)
        print(
            "update: Pitwall did not update. Personal settings belong in ~/.pitwall/profile.yaml. "
            f'To set your edits aside, run: git -C "{root}" stash',
            file=out,
        )
        return _error(out, "then run the update again")

    try:
        version_before = _version(root)
        head_before = _git_output(root, "rev-parse", "HEAD")
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        return _error(out, f"could not read the current Pitwall version: {exc}")
    if head_before.returncode != 0:
        return _error(out, f"git rev-parse HEAD failed: {head_before.stderr.strip()}")

    head_before_value = head_before.stdout.strip()
    print(f"Updating Pitwall {version_before} in {root}", file=out, flush=True)
    try:
        pull = subprocess.run(
            ["git", "-C", str(root), "pull", "--ff-only"],
            check=False,
        )
    except OSError as exc:
        return _error(out, f"git pull failed (see above). Pitwall did not change. {exc}")
    if pull.returncode != 0:
        return _error(out, "git pull failed (see above). Pitwall did not change.")

    if sync_cmd is None:
        uv = os.environ.get("UV") or shutil.which("uv")
        if uv is None:
            return _error(out, f"uv not found. Run `uv sync` in {root} yourself.")
        command = [uv, "sync"]
    else:
        command = list(sync_cmd)

    try:
        sync = subprocess.run(command, cwd=root, check=False)
    except OSError as exc:
        return _error(
            out,
            "uv sync failed (see above). The code is updated. Stop Pitwall if it is running, "
            "then run the update again. `uv run pitwall start` also finishes the sync. "
            f"{exc}",
        )
    if sync.returncode != 0:
        return _error(
            out,
            "uv sync failed (see above). The code is updated. Stop Pitwall if it is running, "
            "then run the update again. `uv run pitwall start` also finishes the sync.",
        )

    try:
        head_after = _git_output(root, "rev-parse", "HEAD")
        version_after = _version(root)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        return _error(out, f"could not read the updated Pitwall version: {exc}")
    if head_after.returncode != 0:
        return _error(out, f"git rev-parse HEAD failed: {head_after.stderr.strip()}")
    if head_after.stdout.strip() == head_before_value:
        print(f"Pitwall is up to date (version {version_before}).", file=out)
    else:
        print(f"Updated Pitwall from {version_before} to {version_after}.", file=out)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    return update(install_dir())
