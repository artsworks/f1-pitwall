"""Review mode: a paced replay of a recording behind the dashboard, with
play/pause/seek/grade. See docs/07.

The decision timeline comes from one max-speed pre-pass with a VirtualClock
and an in-memory DecisionLog; the visible (paced) replay then just drives the
normal dashboard via the hub.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pitwall.clock import PausableClock, ReplayClock, VirtualClock
from pitwall.engine import Engine, run_replay
from pitwall.net.recording import RecordingReader, read_index
from pitwall.server.hub import Hub
from pitwall.store.db import Database

# Decision-log outcomes surfaced as ticks on the transport strip.
_TIMELINE_OUTCOMES = {
    "fired",
    "suppressed",
    "ack",
    "neg",
    "say_again",
    "quiet_until",
    "bookmark",
}


def _index_entries(path: Path) -> list[dict[str, Any]]:
    entries = read_index(path)
    if entries:
        return entries
    from pitwall.net.recording import build_index

    return [e.to_dict() for e in build_index(path)]


def _duration_us(path: Path) -> int:
    last = 0
    with RecordingReader(path) as reader:
        for offset_us, _ in reader:
            last = offset_us
    return last


def grades_path_for(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".grades.jsonl")


class ReviewController:
    """Owns the paced replay task and the review API state."""

    def __init__(
        self,
        path: Path | str,
        engine_factory: Callable[[Any], Engine],
        hub: Hub,
        speed: float,
        db: Database | None = None,
        timeline_log: io.StringIO | None = None,
    ) -> None:
        self.path = Path(path)
        self.speed = speed
        self._engine_factory = engine_factory
        self.hub = hub
        self.db = db or Database(":memory:")
        self.engine: Engine | None = None
        self.clock: PausableClock | None = None
        self._task: asyncio.Task[Any] | None = None
        self._from_us = 0
        self._started_wall: float | None = None
        self._idx: list[dict[str, Any]] | None = None
        self._dur: int | None = None

        self.timeline = self._build_timeline(timeline_log)
        self.grades_path = grades_path_for(self.path)
        self._grades: dict[str, dict[str, Any]] = {}
        if self.grades_path.exists():
            for line in self.grades_path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    self._grades[str(row.get("call_id"))] = row
        for d in self.timeline["decisions"]:
            g = self._grades.get(str(d.get("call_id")))
            if g is not None:
                d["grade"] = g["grade"]

    # -- pre-pass -------------------------------------------------------------

    def _build_timeline(self, log_fp: io.StringIO | None) -> dict[str, Any]:
        """Run the recording once at max speed; collect the decision records."""
        fp = log_fp or io.StringIO()
        engine = self._engine_factory_for_prepass(fp)
        asyncio.run(run_replay(self.path, engine, None))
        decisions = []
        fp.seek(0)
        for line in fp:
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("outcome") in _TIMELINE_OUTCOMES:
                decisions.append(rec)
        laps = [
            {"lap": int(e["detail"]), "offset_us": int(e["offset_us"])}
            for e in self._entries
            if e.get("kind") == "lap"
        ]
        events = [e for e in self._entries if e.get("kind") == "event"]
        return {
            "duration_us": self._duration,
            "laps": laps,
            "events": events,
            "decisions": decisions,
        }

    def _engine_factory_for_prepass(self, fp: io.StringIO) -> Engine:
        """Max-speed timeline run: VirtualClock, in-memory log, the same db so
        grades/calls still land somewhere consistent."""
        engine = self._engine_factory(VirtualClock())
        engine.dispatcher.log._fp = fp  # noqa: SLF001 - in-memory timeline log
        engine.dispatcher.sinks = []  # the pre-pass must not reach the dashboard
        return engine

    # -- transport -------------------------------------------------------------

    def _lap_boundary_us(self, offset_us: int) -> int:
        """Largest lap-boundary offset <= offset_us (0 when none/earlier)."""
        best = 0
        for lap in self.timeline["laps"]:
            if lap["offset_us"] <= offset_us:
                best = lap["offset_us"]
        return best

    def _start_run(self, from_us: int) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._from_us = from_us
        self.hub.recent_calls.clear()
        self.clock = PausableClock(ReplayClock(self.speed, start=from_us / 1e6))
        self.engine = self._engine_factory(self.clock)
        self._task = asyncio.ensure_future(
            run_replay(self.path, self.engine, self.speed, from_us=from_us)
        )
        self._task.add_done_callback(lambda _t: None)

    async def play(self) -> dict[str, Any]:
        if self.clock is not None and self.clock.paused:
            self.clock.resume()
        elif self._task is None or self._task.done():
            self._start_run(self._position_us())
        return self.status()

    async def pause(self) -> dict[str, Any]:
        if self.clock is not None:
            self.clock.pause()
        return self.status()

    async def seek(self, lap: int | None = None, offset_us: int | None = None) -> dict[str, Any]:
        if offset_us is None and lap is not None:
            for entry in self.timeline["laps"]:
                if entry["lap"] == lap:
                    offset_us = entry["offset_us"]
                    break
            else:
                offset_us = 0
        target = self._lap_boundary_us(int(offset_us or 0))
        self._start_run(target)
        return self.status()

    def _position_us(self) -> int:
        if self.clock is None:
            return self._from_us
        return int(self.clock.now() * 1e6)

    def status(self) -> dict[str, Any]:
        pos = self._position_us()
        lap = 0
        for entry in self.timeline["laps"]:
            if entry["offset_us"] <= pos:
                lap = entry["lap"]
        playing = (
            self._task is not None
            and not self._task.done()
            and not (self.clock.paused if self.clock else False)
        )
        return {
            "playing": playing,
            "finished": self._task is not None and self._task.done(),
            "position_us": pos,
            "duration_us": self._duration,
            "lap": lap,
            "speed": self.speed,
        }

    # -- grades -----------------------------------------------------------------

    def grade(
        self, call_id: str, rule_id: str, grade: str, note: str = "", session_uid: int = 0
    ) -> dict[str, Any]:
        row = {
            "call_id": call_id,
            "rule_id": rule_id,
            "grade": grade,
            "note": note,
            "session_uid": session_uid,
            "graded_at": time.time(),
        }
        self._grades[call_id] = row
        for d in self.timeline["decisions"]:
            if str(d.get("call_id")) == call_id:
                d["grade"] = grade
        self.db.grade_call(session_uid, call_id, rule_id, grade, note)
        with self.grades_path.open("a") as f:
            f.write(json.dumps(row) + "\n")
        return row

    def grades(self) -> list[dict[str, Any]]:
        return list(self._grades.values())

    # -- init-time fields ---------------------------------------------------

    @property
    def _entries(self) -> list[dict[str, Any]]:
        if self._idx is None:
            self._idx = _index_entries(self.path)
        return self._idx

    @property
    def _duration(self) -> int:
        if self._dur is None:
            self._dur = _duration_us(self.path)
        return self._dur
