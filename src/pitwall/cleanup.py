"""`pitwall cleanup`: find old files that are safe to delete.

Only files Pitwall can rebuild or no longer needs are listed. The database,
profile, track overlays and voice models are never touched, and a recording
is only listed once its session is in the learning database."""

from __future__ import annotations

import io
import math
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import zstandard

_SESSION = re.compile(r"^session_([0-9a-f]{16})_\d+\.f1(bin|bin\.zst|idx)$")
_RECENT_S = 3600.0  # never touch files written in the last hour: a session may be live


@dataclass(frozen=True, slots=True)
class Candidate:
    path: Path
    size: int
    reason: str
    root: Path
    device: int
    inode: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class Plan:
    delete: list[Candidate]
    kept_unlearned: int  # old recordings kept because they are not in the database yet

    @property
    def total_bytes(self) -> int:
        return sum(c.size for c in self.delete)


def _linked(path: Path) -> bool:
    return any(p.is_symlink() or p.is_junction() for p in (path, *path.parents))


def _files(root: Path, pattern: str) -> Iterable[Path]:
    if not root.is_dir() or _linked(root.absolute()):
        return []
    base = root.resolve()
    return sorted(
        p
        for p in root.glob(pattern)
        if p.is_file() and not _linked(p.absolute()) and p.resolve().parent == base
    )


def _same_as_zst(path: Path) -> bool:
    sibling = path.with_name(path.name + ".zst")
    try:
        with path.open("rb") as source, sibling.open("rb") as compressed:
            with io.BufferedReader(
                zstandard.ZstdDecompressor().stream_reader(compressed)
            ) as decoded:
                while True:
                    source_chunk = source.read(1 << 20)
                    decoded_chunk = decoded.read(1 << 20)
                    if source_chunk != decoded_chunk:
                        return False
                    if not source_chunk:
                        return True
    except (zstandard.ZstdError, OSError):
        return False


def plan_cleanup(
    recordings_dir: Path,
    pitwall_dir: Path,
    voices_dir: Path,
    learned_uids: set[int],
    older_than_days: float,
    now: float | None = None,
    recording_imports: Mapping[Path, float] | None = None,
) -> Plan:
    if not math.isfinite(older_than_days) or older_than_days < 0:
        raise ValueError("--days must be a finite, non-negative number")
    now = time.time() if now is None else now
    recent = now - _RECENT_S
    cutoff = min(now - older_than_days * 86400.0, recent)
    out: list[Candidate] = []
    kept = 0

    def add(p: Path, reason: str) -> None:
        stat = p.stat()
        out.append(
            Candidate(
                p.absolute(),
                stat.st_size,
                reason,
                p.parent.resolve(),
                stat.st_dev,
                stat.st_ino,
                stat.st_mtime_ns,
            )
        )

    for p in _files(recordings_dir, "session_*"):
        m = _SESSION.match(p.name)
        if m is None:
            continue
        mtime = p.stat().st_mtime
        if mtime > recent:
            continue
        if m.group(2) == "bin":
            sibling = p.with_name(p.name + ".zst")
            try:
                sibling_old = (
                    not _linked(sibling.absolute())
                    and sibling.is_file()
                    and sibling.stat().st_mtime <= recent
                )
            except OSError:
                sibling_old = False
            if sibling_old and _same_as_zst(p):
                add(p, "uncompressed copy, the .f1bin.zst has the same data")
                continue
        if mtime > cutoff:
            continue
        if int(m.group(1), 16) not in learned_uids:
            if m.group(2) != "idx":
                kept += 1
            continue
        if recording_imports is not None:
            source = p.with_suffix(".f1bin") if m.group(2) == "idx" else p
            sources = (source.resolve(), Path(str(source.resolve()) + ".zst"))
            imported_at = max((recording_imports.get(path, 0.0) for path in sources), default=0.0)
            if imported_at < mtime:
                if m.group(2) != "idx":
                    kept += 1
                continue
        add(p, "old recording, already learned")

    for p in _files(recordings_dir, "voice-spike-*.jsonl"):
        if p.stat().st_mtime <= cutoff:
            add(p, "old voice test log")
    for p in _files(pitwall_dir / "digests", "*.json"):
        if p.stat().st_mtime <= cutoff:
            add(p, "old digest, rebuilt from the database")
    for p in _files(voices_dir / ".phrases", "*.wav"):
        if p.stat().st_mtime <= recent:
            add(p, "speech cache, rebuilt when needed")
    return Plan(out, kept)


def apply(plan: Plan) -> tuple[int, int]:
    """Delete the planned files; returns (files deleted, bytes freed)."""
    n = freed = 0
    for c in plan.delete:
        try:
            if _linked(c.path) or not c.path.is_file() or c.path.resolve().parent != c.root:
                continue
            stat = c.path.stat()
            if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != (
                c.device,
                c.inode,
                c.size,
                c.mtime_ns,
            ):
                continue
            c.path.unlink()
        except FileNotFoundError:
            continue
        n += 1
        freed += c.size
    return n, freed
