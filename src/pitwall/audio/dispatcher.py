"""Dispatcher: priority queue with suppression layers, deadlines, preemption
and speak-time revalidation (docs/04). Sinks implement speak()/cancel()."""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from typing import Any, Protocol

from pitwall.audio.decision_log import DecisionLog
from pitwall.clock import Clock
from pitwall.config.models import InputSettings, PolicySettings
from pitwall.input.press import Press
from pitwall.metrics import Metrics
from pitwall.rules.engine import Candidate
from pitwall.state.session import Snapshot

_VERBOSITY_PRIORITIES = {
    "silent": set(),
    "critical": {1},
    "normal": {1, 2, 3},
    "coach": {1, 2, 3},
}

# P2/P3 calls per lap under each verbosity preset (P1 never counts).
_VERBOSITY_BUDGET = {
    "silent": 0,
    "critical": 0,
    "normal": 4,
    "coach": 8,
}

# Rough speech estimate for the ack/neg response window (~15 chars/s).
_CHARS_PER_SECOND = 15.0


class CallSink(Protocol):
    speaks_audio: bool  # True: sink plays audio -> skip screen-only calls

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
    screen_only: bool = False
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(order=True)
class _Queued:
    sort_key: tuple[int, float] = field(compare=True)
    call: Call = field(compare=False)


class LogSink:
    """Prints calls to stdout."""

    speaks_audio = False

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
        input: InputSettings | None = None,
    ) -> None:
        self.policy = policy
        self.input = input or InputSettings()
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
        self._calls_lap: tuple[int, int, int] = (0, 0, 0)
        self._run = 0
        self._run_phase = ""
        self._current: Call | None = None
        self.latest_snapshot: Snapshot | None = None
        # Driver input: presses and their effects (docs/12).
        self.quiet_until: float | None = None
        self.on_press_event: Any = None  # callable(payload dict) -> hub broadcast
        self._spoken_calls: list[tuple[Call, float]] = []  # (call, est. speech end t)
        self._negatives: dict[str, int] = {}
        self._neg_mute_until: dict[str, int] = {}  # rule_id -> lap
        self._cooldown_mult: dict[str, float] = {}
        self._acked: dict[str, int] = {}  # rule_id -> lap acknowledged on

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
                screen_only=cand.screen_only or self.policy.verbosity == "silent",
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
                inputs=cand.inputs,
            )
            self._book_call(call, cand, now)
            heapq.heappush(self._queue, _Queued((cand.priority, now), call))
            self._log(cand, snapshot, now, "queued", None, call_id=call.id)

    def _suppression_reason(
        self, cand: Candidate, snapshot: Snapshot, now: float, allowed_p: set[int]
    ) -> str | None:
        d = cand.rule.defn
        # Quiet (and quiet_until) suppress everything except P1 — guard-rail.
        if self.policy.quiet and cand.priority != 1:
            return "quiet_mode"
        if self.quiet_until is not None and now < self.quiet_until and cand.priority != 1:
            return "quiet_until"
        if cand.priority not in allowed_p and not (
            cand.screen_only or self.policy.verbosity == "silent"
        ):
            return "verbosity"
        if snapshot.lap_num < self.policy.mute_until_lap:
            return "mute_until_lap"
        if self._acked.get(cand.rule.id) == snapshot.lap_num:
            return "acknowledged"
        mute_until = self._neg_mute_until.get(cand.rule.id)
        if cand.priority != 1 and mute_until is not None and snapshot.lap_num < mute_until:
            return "negative_backoff"
        cd_key = d.cooldown_group or cand.rule.id
        last = self._last_fired.get(cd_key)
        cooldown = d.cooldown_s * self._cooldown_mult.get(cd_key, 1.0)
        if cooldown and last is not None and now - last < cooldown:
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
            # Qualifying runs sit on one lap number across garage stays; each
            # garage visit and out-lap starts a fresh budget.
            if snapshot.phase != self._run_phase:
                if snapshot.phase in ("garage", "out_lap"):
                    self._run += 1
                self._run_phase = snapshot.phase
            lap_key = (snapshot.lap_num, self._run, snapshot.line_crossings)
            if self._calls_lap != lap_key:
                self._calls_lap = lap_key
                self._calls_this_lap = 0
            budget = (
                self.budget_override
                if self.budget_override is not None
                else self.policy.calls_per_lap
                if self.policy.calls_per_lap is not None
                else _VERBOSITY_BUDGET[self.policy.verbosity]
            )
            if self._calls_this_lap >= budget:
                return "budget"
            if self._last_call_t is not None and now - self._last_call_t < self.policy.min_gap_s:
                return "budget"
        return None

    def _book_call(self, call: Call, cand: Candidate, now: float) -> None:
        self._last_fired[cand.rule.defn.cooldown_group or cand.rule.id] = now
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
        held: list[_Queued] = []  # P3 calls waiting for a straight
        snap = self.latest_snapshot
        on_straight = bool(snap.on_straight) if snap is not None else False
        while self._queue:
            q = heapq.heappop(self._queue)
            call = q.call
            if (now - call.t) * 1000 > call.deadline_ms:
                self._log_call(call, "suppressed", "deadline")
                continue
            if call.priority == 3 and self.policy.p3_straight_only and not on_straight:
                held.append(q)
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
                if call.screen_only and sink.speaks_audio:
                    continue
                sink.speak(call)
            self.metrics.note_trigger_to_speak(call.trigger_t, spoken_t)
            self._current = call
            self._spoken_calls.append((call, now + len(call.text) / _CHARS_PER_SECOND))
            self._spoken_calls = self._spoken_calls[-16:]
            self._log_call(call, "fired", None)
            emitted.append(call)
        for q in held:
            heapq.heappush(self._queue, q)
        return emitted

    def purge(self, reason: str, now: float | None = None) -> int:
        """Drop every queued (not yet spoken) call, logging each as suppressed."""
        purged = 0
        while self._queue:
            call = heapq.heappop(self._queue).call
            self._log_call(call, "suppressed", reason)
            purged += 1
        return purged

    def reset_session(self) -> None:
        """New session: clear the queue and per-stint/per-lap budgets."""
        self._queue.clear()
        self._fires_this_stint.clear()
        self._calls_this_lap = 0
        self._calls_lap = (0, 0, 0)
        self._run = 0
        self._run_phase = ""
        self._current = None
        self.quiet_until = None
        self._spoken_calls.clear()
        self._negatives.clear()
        self._neg_mute_until.clear()
        self._cooldown_mult.clear()
        self._acked.clear()
        snap = self.latest_snapshot
        self.log.write(
            {
                "t": snap.now if snap else self.clock.now(),
                "session_time": snap.session_time if snap else None,
                "lap": snap.lap_num if snap else None,
                "lap_distance": snap.lap_distance if snap else None,
                "rule_id": None,
                "outcome": "session_reset",
                "suppressed_by": None,
                "inputs": {},
                "text": "",
            }
        )

    async def run(self) -> None:
        """Live loop: drain the queue periodically."""
        while True:
            self.drain()
            await self.clock.sleep(0.05)

    # -- driver input ------------------------------------------------------

    def on_press(self, press: Press, snapshot: Snapshot) -> None:
        """Route an ack/neg/bookmark to the most recent spoken call (docs/12)."""
        now = snapshot.now
        window = self.input.response_window_s
        target: Call | None = None
        for call, end_t in reversed(self._spoken_calls):
            if now - end_t <= window:
                target = call
                break
        payload: dict[str, Any] = {
            "kind": press.kind,
            "lap": snapshot.lap_num,
            "rule_id": target.rule_id if target else None,
            "text": target.text if target else "",
        }
        if press.kind == "bookmark":
            self._log_press(now, snapshot, "bookmark", None, None)
            self._broadcast_press(payload)
            return
        if target is None:
            if press.kind == "ack":
                # Say again: re-speak the last spoken call without booking it.
                if (
                    self._spoken_calls
                    and now - self._spoken_calls[-1][1] <= self.input.say_again_window_s
                ):
                    last, _ = self._spoken_calls[-1]
                    self._log_press(now, snapshot, "say_again", last, None)
                    replay = Call(
                        id=f"c-{next(self._counter)}",
                        rule_id=last.rule_id,
                        priority=last.priority,
                        text=last.text,
                        tags=[*last.tags, "say_again"],
                        deadline_ms=last.deadline_ms,
                        lap=snapshot.lap_num,
                        t=now,
                        trigger_t=now,
                        still_true=None,
                        screen_only=last.screen_only,
                    )
                    heapq.heappush(self._queue, _Queued((last.priority, now), replay))
            else:  # neg with no target: quiet for quiet_minutes
                self.quiet_until = now + self.input.quiet_minutes * 60
                self._log_press(now, snapshot, "quiet_until", None, None)
            self._broadcast_press(payload)
            return
        self._log_press(now, snapshot, press.kind, target, press.kind)
        if press.kind == "ack":
            self._acked[target.rule_id] = snapshot.lap_num
        else:
            n = self._negatives.get(target.rule_id, 0) + 1
            self._negatives[target.rule_id] = n
            if target.priority != 1:  # a negative never mutes P1
                if n == 1:
                    self._neg_mute_until[target.rule_id] = (
                        snapshot.lap_num + self.input.negative_mute_laps
                    )
                else:
                    key = target.rule_id
                    self._cooldown_mult[key] = self._cooldown_mult.get(key, 1.0) * 2
        self._broadcast_press(payload)

    def _log_press(
        self,
        now: float,
        snap: Snapshot,
        outcome: str,
        call: Call | None,
        by: str | None,
    ) -> None:
        self.log.write(
            {
                "t": now,
                "session_time": snap.session_time,
                "lap": snap.lap_num,
                "lap_distance": snap.lap_distance,
                "rule_id": call.rule_id if call else None,
                "call_id": call.id if call else None,
                "outcome": outcome,
                "suppressed_by": by,
                "inputs": {},
                "text": call.text if call else "",
            }
        )

    def _broadcast_press(self, payload: dict[str, Any]) -> None:
        if self.on_press_event is not None:
            self.on_press_event(payload)

    def _log(
        self,
        cand: Candidate,
        snap: Snapshot,
        now: float,
        outcome: str,
        by: str | None,
        call_id: str | None = None,
    ) -> None:
        self.log.write(
            {
                "t": now,
                "session_time": snap.session_time,
                "lap": snap.lap_num,
                "lap_distance": snap.lap_distance,
                "call_id": call_id,
                "rule_id": cand.rule.id,
                "priority": cand.priority,
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
                "call_id": call.id,
                "rule_id": call.rule_id,
                "priority": call.priority,
                "outcome": outcome,
                "suppressed_by": by,
                "inputs": call.inputs,
                "text": call.text,
            }
        )
