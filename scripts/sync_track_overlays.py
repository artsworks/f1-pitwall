"""Promote learned per-track overlays into src/pitwall/config/defaults/tracks/.

Reads ~/.pitwall/tracks/*.yaml (authoritative when present) or the
track_overlays block of a learning pack, merges the learned keys into the
packaged files, runs the track tests and opens one feat(tracks): PR.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from pitwall.learning.debrief import _TRACK_NAMES

REPO_ROOT = Path(__file__).resolve().parent.parent
TRACKS_DIR = REPO_ROOT / "src" / "pitwall" / "config" / "defaults" / "tracks"
SKIP_KEYS = {"track_id", "name", "deg_ms_per_lap"}
VERIFY_TESTS = ["tests/test_config.py", "tests/test_track_overlay.py", "tests/test_calibrate.py"]


def _int_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            (int(k) if isinstance(k, str) and k.isdigit() else k): _int_keys(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_int_keys(v) for v in value]
    return value


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )


def check_clean_tree(yes: bool) -> bool:
    """True when it is safe to proceed. Prompts on a dirty tree."""
    status = _git("status", "--porcelain")
    if status.returncode != 0:
        print(f"ERROR: git status failed: {status.stderr.strip()}")
        return False
    dirty = status.stdout.strip()
    if not dirty:
        return True
    print("Working tree is not clean:")
    print(dirty)
    if yes:
        return True
    if not sys.stdin.isatty():
        print("ERROR: dirty tree and stdin is not a TTY. Pass --yes to continue.")
        return False
    reply = input("Continue? [y/N] ")
    return reply.strip().lower() == "y"


def load_pack(path: Path) -> tuple[str, dict[int, dict[str, Any]]]:
    pack = json.loads(path.read_text(encoding="utf-8"))
    date = str(pack.get("written_at", ""))[:10]
    overlays = _int_keys(pack.get("track_overlays") or {})
    return date, {int(k): v for k, v in overlays.items() if isinstance(v, dict)}


def load_overlays(
    pack_overlays: dict[int, dict[str, Any]], overlay_dir: Path
) -> tuple[dict[int, dict[str, Any]], list[str]]:
    """Home YAML wins when present. Returns (overlays, warnings)."""
    warnings: list[str] = []
    home_files = sorted(overlay_dir.glob("*.yaml")) if overlay_dir.is_dir() else []
    if not home_files:
        return pack_overlays, warnings
    overlays: dict[int, dict[str, Any]] = {}
    for path in home_files:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not path.stem.lstrip("-").isdigit():
            continue
        learned = _int_keys(data)
        tid = int(path.stem)
        overlays[tid] = learned
        if pack_overlays.get(tid) != learned:
            warnings.append(
                f"WARN: home/pack mismatch for track {tid}: {path.name} differs from the pack entry"
            )
    return overlays, warnings


def track_name(track_id: int, name_overrides: dict[int, str]) -> str:
    if track_id in name_overrides:
        return name_overrides[track_id]
    if track_id in _TRACK_NAMES:
        return _TRACK_NAMES[track_id].lower().replace(" ", "_")
    raise ValueError(f"no name for track {track_id}: pass --name {track_id}=<venue>")


def merge_overlay(
    packaged: dict[str, Any], learned: dict[str, Any], *, track_id: int
) -> tuple[dict[str, Any], list[str]]:
    """Return (merged, warnings). Learned keys replace; packaged-only keys stay."""
    warnings: list[str] = []
    if "deg_ms_per_lap" in learned:
        warnings.append(f"WARN: deg_ms_per_lap in learned overlay for track {track_id}; not copied")
    merged = dict(packaged)
    for key, value in learned.items():
        if key in SKIP_KEYS:
            continue
        if key == "thresholds" and isinstance(value, dict):
            base = dict(merged.get("thresholds") or {})
            base.update(value)
            merged["thresholds"] = base
        else:
            merged[key] = value
    return merged, warnings


def new_overlay(track_id: int, name: str, learned: dict[str, Any]) -> dict[str, Any]:
    overlay: dict[str, Any] = {"track_id": track_id, "name": name}
    for key, value in learned.items():
        if key not in SKIP_KEYS:
            overlay[key] = value
    return overlay


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def render(data: dict[str, Any]) -> str:
    """Render one track file: plain scalars, dicts as one-line flow maps, LF."""
    lines = []
    for key, value in data.items():
        if isinstance(value, dict):
            inner = ", ".join(f"{_scalar(k)}: {_scalar(v)}" for k, v in value.items())
            lines.append(f"{key}: {{{inner}}}")
        else:
            lines.append(f"{key}: {_scalar(value)}")
    return "\n".join(lines) + "\n"


def _diff_rows(
    track_id: int, before: dict[str, Any], after: dict[str, Any]
) -> list[tuple[str, str, str, str]]:
    rows = []
    for key, value in after.items():
        if before.get(key) == value:
            continue
        if key == "thresholds" and isinstance(value, dict):
            base = before.get("thresholds") or {}
            for tkey, tval in value.items():
                if base.get(tkey) != tval:
                    old = _scalar(base[tkey]) if tkey in base else "-"
                    rows.append((str(track_id), f"thresholds.{tkey}", old, _scalar(tval)))
        else:
            old = _scalar(before[key]) if key in before else "-"
            rows.append((str(track_id), key, old, _scalar(value)))
    return rows


def verify_written(written: dict[str, Any], learned: dict[str, Any]) -> list[str]:
    """Learned keys missing from the written file. thresholds compare key by key,
    because the file keeps packaged keys that the learned file does not set."""
    bad = []
    for key, value in learned.items():
        if key in SKIP_KEYS:
            continue
        if key == "thresholds" and isinstance(value, dict):
            base = written.get("thresholds") or {}
            bad += [f"thresholds.{k}" for k, v in value.items() if base.get(k) != v]
        elif written.get(key) != value:
            bad.append(key)
    return bad


def _parse_names(items: list[str]) -> dict[int, str]:
    names: dict[int, str] = {}
    for item in items:
        tid, _, name = item.partition("=")
        names[int(tid)] = name
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pack", type=Path, default=REPO_ROOT / "learnings" / "learning-latest.json"
    )
    parser.add_argument("--overlay-dir", type=Path, default=Path.home() / ".pitwall" / "tracks")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-pr", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--name", action="append", default=[], metavar="ID=venue")
    args = parser.parse_args()

    date, pack_overlays = load_pack(args.pack)
    learned, warnings = load_overlays(pack_overlays, args.overlay_dir)
    name_overrides = _parse_names(args.name)

    if not args.dry_run:
        if not check_clean_tree(args.yes):
            return 1
        fetch = _git("fetch", "origin")
        if fetch.returncode != 0:
            print(f"ERROR: git fetch failed: {fetch.stderr.strip()}")
            return 1
        switch = _git("switch", "-c", f"tracks/sync-{date}", "origin/main")
        if switch.returncode != 0:
            print(f"ERROR: git switch failed: {switch.stderr.strip()}")
            return 1

    rows: list[tuple[str, str, str, str]] = []
    changed: list[Path] = []
    merged_by_id: dict[int, dict[str, Any]] = {}
    for tid in sorted(t for t in learned if t != -1):
        overlay = learned[tid]
        path = TRACKS_DIR / f"{tid}.yaml"
        if path.exists():
            packaged = yaml.safe_load(path.read_text(encoding="utf-8"))
            merged, warns = merge_overlay(packaged, overlay, track_id=tid)
        else:
            name = track_name(tid, name_overrides)
            packaged = {}
            merged = new_overlay(tid, name, overlay)
            warns = (
                [f"WARN: deg_ms_per_lap in learned overlay for track {tid}; not copied"]
                if "deg_ms_per_lap" in overlay
                else []
            )
            print(f"NEW: track {tid} ({name})")
        warnings.extend(warns)
        merged_by_id[tid] = merged
        rows.extend(_diff_rows(tid, packaged, merged))
        if not path.exists() or render(merged).encode() != path.read_bytes().replace(
            b"\r\n", b"\n"
        ):
            changed.append(path)

    for warning in warnings:
        print(warning)
    if rows:
        width = max(len(r[1]) for r in rows)
        print(f"{'track':<6}{'key':<{width + 2}}{'old':>10}  {'new':>10}")
        for tid, key, old, new in rows:
            print(f"{tid:<6}{key:<{width + 2}}{old:>10}  {new:>10}")
    read_count = len(learned) - (1 if -1 in learned else 0)
    print(f"{len(changed)} file(s) changed, {read_count} overlay(s) read")
    if not changed:
        print("Nothing to do.")
        return 0
    if args.dry_run:
        return 0

    for path in changed:
        tid = int(path.stem)
        path.write_bytes(render(merged_by_id[tid]).encode())

    for tid, overlay in learned.items():
        if tid == -1:
            continue
        written = yaml.safe_load((TRACKS_DIR / f"{tid}.yaml").read_text(encoding="utf-8"))
        bad = verify_written(written, overlay)
        if bad:
            print(f"ERROR: track {tid} did not persist learned keys: {bad}")
            return 1

    tests = subprocess.run(["uv", "run", "pytest", "-q", *VERIFY_TESTS], cwd=REPO_ROOT, check=False)
    if tests.returncode != 0:
        print("ERROR: tests failed; not committing")
        return 1

    _git("add", *[str(p.relative_to(REPO_ROOT)) for p in changed])
    commit = _git("commit", "-m", f"feat(tracks): promote learned overlays from the {date} pack")
    if commit.returncode != 0:
        print(f"ERROR: git commit failed: {commit.stderr.strip()}")
        return 1
    if args.no_pr:
        return 0
    push = _git("push", "-u", "origin", f"tracks/sync-{date}")
    if push.returncode != 0:
        print(f"ERROR: git push failed: {push.stderr.strip()}")
        return 1
    body = "\n".join(
        ["| track | key | old | new |", "| --- | --- | --- | --- |"]
        + [f"| {t} | {k} | {o} | {n} |" for t, k, o, n in rows]
        + [""]
        + warnings
    )
    pr = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--title",
            f"feat(tracks): promote learned overlays from the {date} pack",
            "--body",
            body,
        ],
        cwd=REPO_ROOT,
        check=False,
    )
    return pr.returncode


if __name__ == "__main__":
    raise SystemExit(main())
