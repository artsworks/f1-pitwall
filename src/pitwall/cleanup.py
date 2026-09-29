"""`pitwall cleanup`: find old files that are safe to delete.

Only files Pitwall can rebuild or no longer needs are listed. The database,
profile, track overlays and voice models are never touched, and a recording
is only listed once its session is in the learning database."""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_SESSION = re.compile(r"^session_([0-9a-f]{16})_\d+\.f1(bin|bin\.zst|idx)$")
_RECENT_S = 3600.0  # never touch files written in the last hour: a session may be live


@dataclass(frozen=True, slots=True)
class Candidate:
    path: Path
    size: int
    reason: str


@dataclass(frozen=True, slots=True)
class Plan:
    delete: list[Candidate]
    kept_unlearned: int  # old recordings kept because they are not in the database yet

    @property
    def total_bytes(self) -> int:
        return sum(c.size for c in self.delete)


def _files(root: Path, pattern: str) -> Iterable[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    base = root.resolve()
    return sorted(
        p
        for p in root.glob(pattern)
        if p.is_file() and not p.is_symlink() and p.resolve().parent == base
    )


def plan_cleanup(
    recordings_dir: Path,
    pitwall_dir: Path,
    voices_dir: Path,
    learned_uids: set[int],
    older_than_days: float,
    now: float | None = None,
) -> Plan:
    now = time.time() if now is None else now
    cutoff = now - older_than_days * 86400.0
    recent = now - _RECENT_S
    out: list[Candidate] = []
    kept = 0

    def add(p: Path, reason: str) -> None:
        out.append(Candidate(p, p.stat().st_size, reason))

    for p in _files(recordings_dir, "session_*"):
        m = _SESSION.match(p.name)
        if m is None:
            continue
        mtime = p.stat().st_mtime
        if mtime > recent:
            continue
        if mtime > cutoff:
            continue
        if int(m.group(1), 16) not in learned_uids:
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
    for p in _files(voices_dir / ".phrases", "*"):
        if p.stat().st_mtime <= recent:
            add(p, "speech cache, rebuilt when needed")
    return Plan(out, kept)


def apply(plan: Plan) -> tuple[int, int]:
    """Delete the planned files; returns (files deleted, bytes freed)."""
    n = freed = 0
    for c in plan.delete:
        try:
            c.path.unlink()
        except FileNotFoundError:
            continue
        n += 1
        freed += c.size
    return n, freed
