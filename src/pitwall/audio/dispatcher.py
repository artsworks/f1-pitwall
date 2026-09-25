"""Dispatcher: priority queue with suppression layers, deadlines, preemption
and speak-time revalidation (docs/04). Sinks implement speak()/cancel()."""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from typing import Any, Protocol

from pitwall.audio.decision_log import DecisionLog
from pitwall.clock import Clock
from pitwall.config.models import PolicySettings
from pitwall.metrics import Metrics
from pitwall.rules.engine import Candidate
from pitwall.state.session import Snapshot

_VERBOSITY_PRIORITIES = {
    "silent": set(),
    "critical": {1},
    "normal": {1, 2, 3},
    "coach": {1, 2, 3},
}


class CallSink(Protocol):
    def speak(self, call: Call) -> None: ...

    def cancel(self, call_id: str) -> None: ...


@dataclass(slots=True)
class Call:
    id: str
    rule_id: str
    priority: int
    text: str
    tags: list[str]
    deadline_ms: int
    lap: int
    t: float
    trigger_t: float
    still_true: Any = None


@dataclass(order=True)
class _Queued:
    sort_key: tuple[int, float] = field(compare=True)
    call: Call = field(compare=False)


class LogSink:
    """Prints calls to stdout."""

    def speak(self, call: Call) -> None:
        print(f"[radio] {call.text}")

    def cancel(self, call_id: str) -> None:
        pass


class Dispatcher:
    def __init__(
        self,
        policy: PolicySettings,
        clock: Clock,
        decision_log: DecisionLog | None = None,
        sinks: list[CallSink] | None = None,
        metrics: Metrics | None = None,
        budget_override: int | None = None,
    ) -> None:
        self.policy = policy
        self.clock = clock
        self.log = decision_log or DecisionLog()
        self.sinks = sinks or [LogSink()]
        self.metrics = metrics or Metrics()
        self.budget_override = budget_override
        self._queue: list[_Queued] = []
        self._counter = itertools.count()
        self._last_fired: dict[str, float] = {}  # rule_id -> t
        self._fires_this_stint: dict[str, int] = {}
        self._recent_texts: list[tuple[str, float]] = []
        self._last_call_t: float | None = None
        self._calls_this_lap = 0
        self._calls_lap = 0
        self._current: Call | None = None
        self.latest_snapshot: Snapshot | None = None

    # -- submission ----------------------------------------------------------

    def submit(self, candidates: list[Candidate], snapshot: Snapshot) -> None:
        self.latest_snapshot = snapshot
        # Suppression windows run on snapshot time so a max-speed replay
        # decides identically to 1x (docs/07 determinism).
        now = snapshot.now
        allowed_p = _VERBOSITY_PRIORITIES[self.policy.verbosity]
        for cand in candidates:
            suppressed = self._suppression_reason(cand, snapshot, now, allowed_p)
            if suppressed is not None:
                self._log(cand, snapshot, now, "suppressed", suppressed)
                continue
            call = Call(
                id=f"c-{next(self._counter)}",
                rule_id=cand.rule.id,
                priority=cand.priority,
                text=cand.text,
                tags=cand.tags,
                deadline_ms=int(self.policy.deadlines_s.get(cand.priority, 1.5) * 1000),
                lap=snapshot.lap_num,
                t=now,
                trigger_t=cand.trigger_t,
                still_true=cand.still_true,
            )
            self._book_call(call, cand, now)
            heapq.heappush(self._queue, _Queued((cand.priority, now), call))
            self._log(cand, snapshot, now, "queued", None)

    def _suppression_reason(
        self, cand: Candidate, snapshot: Snapshot, now: float, allowed_p: set[int]
    ) -> str | None:
        d = cand.rule.defn
        if self.policy.quiet:
            return "quiet_mode"
        if cand.priority not in allowed_p:
            return "verbosity"
        if snapshot.lap_num < self.policy.mute_until_lap:
            return "mute_until_lap"
        last = self._last_fired.get(cand.rule.id)
        if d.cooldown_s and last is not None and now - last < d.cooldown_s:
            return "cooldown"
        if d.max_per_stint is not None:
            if self._fires_this_stint.get(cand.rule.id, 0) >= d.max_per_stint:
                return "max_per_stint"
        self._recent_texts = [
            (t, when) for t, when in self._recent_texts if now - when < self.policy.dedupe_window_s
        ]
        if any(t == cand.text for t, _ in self._recent_texts):
            return "dedupe"
        if cand.priority != 1:
            if self._calls_lap != snapshot.lap_num:
                self._calls_lap = snapshot.lap_num
                self._calls_this_lap = 0
            budget = self.budget_override or self.policy.calls_per_lap
            if self._calls_this_lap >= budget:
                return "budget"
            if self._last_call_t is not None and now - self._last_call_t < self.policy.min_gap_s:
                return "budget"
        return None

    def _book_call(self, call: Call, cand: Candidate, now: float) -> None:
        self._last_fired[cand.rule.id] = now
        self._fires_this_stint[cand.rule.id] = self._fires_this_stint.get(cand.rule.id, 0) + 1
        self._recent_texts.append((cand.text, now))
        self._last_call_t = now
        if call.priority != 1:
            self._calls_this_lap += 1

    # -- output --------------------------------------------------------------

    def drain(self, now: float | None = None) -> list[Call]:
        """Pop due calls in priority order. Returns calls emitted."""
        if now is None:
            now = self.latest_snapshot.now if self.latest_snapshot is not None else self.clock.now()
        emitted: list[Call] = []
        while self._queue:
            q = heapq.heappop(self._queue)
            call = q.call
            if (now - call.t) * 1000 > call.deadline_ms:
                self._log_call(call, "suppressed", "deadline")
                continue
            if call.still_true is not None and self.latest_snapshot is not None:
                try:
                    if not call.still_true(self.latest_snapshot):
                        self._log_call(call, "suppressed", "revalidation")
                        continue
                except Exception:
                    pass
            if self._current is not None and call.priority < self._current.priority:
                for sink in self.sinks:
                    sink.cancel(self._current.id)
                self._current = None
            spoken_t = self.clock.now()
            for sink in self.sinks:
                sink.speak(call)
            self.metrics.note_trigger_to_speak(call.trigger_t, spoken_t)
            self._current = call
            self._log_call(call, "fired", None)
            emitted.append(call)
        return emitted

    async def run(self) -> None:
        """Live loop: drain the queue periodically."""
        while True:
            self.drain()
            await self.clock.sleep(0.05)

    def _log(
        self, cand: Candidate, snap: Snapshot, now: float, outcome: str, by: str | None
    ) -> None:
        self.log.write(
            {
                "t": now,
                "session_time": snap.session_time,
                "lap": snap.lap_num,
                "lap_distance": snap.lap_distance,
                "rule_id": cand.rule.id,
                "outcome": outcome,
                "suppressed_by": by,
                "inputs": cand.inputs,
                "text": cand.text,
            }
        )

    def _log_call(self, call: Call, outcome: str, by: str | None) -> None:
        snap = self.latest_snapshot
        self.log.write(
            {
                "t": snap.now if snap else self.clock.now(),
                "session_time": snap.session_time if snap else None,
                "lap": call.lap,
                "lap_distance": snap.lap_distance if snap else None,
                "rule_id": call.rule_id,
                "outcome": outcome,
                "suppressed_by": by,
                "inputs": {},
                "text": call.text,
            }
        )
