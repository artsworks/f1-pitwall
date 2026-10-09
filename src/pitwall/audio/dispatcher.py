"""Dispatcher: priority queue with suppression layers, deadlines, preemption
and speak-time revalidation (docs/04). Sinks implement speak()/cancel()."""

from __future__ import annotations

import heapq
import itertools
import random
from dataclasses import dataclass, field
from typing import Any, Protocol

from pitwall.audio.decision_log import DecisionLog
from pitwall.clock import Clock
from pitwall.config.models import InputSettings, PolicySettings, RuleDefModel
from pitwall.input.press import Press
from pitwall.metrics import Metrics
from pitwall.rules.engine import Candidate
from pitwall.state.session import Snapshot, snapshot_scalars

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
_URGENCY_RANK = {
    "safety": 0,
    "execution": 1,
    "reply": 2,
    "tactical": 3,
    "info": 4,
    "coaching": 5,
}
_COUNT_WORDS = {
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}


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
    current: Any = None
    conflict_group: str = ""
    screen_only: bool = False
    inputs: dict[str, Any] = field(default_factory=dict)
    not_before: float = 0.0  # held in the queue until this time (min-gap spacing)
    ready_t: float = 0.0
    urgency: str = "info"
    rank: int = 4
    decision_point: str = ""
    decision_m: float | None = None
    decision_s: float | None = None
    promoted: bool = False
    outcome_score: float | None = None
    location_ref: bool = False
    resolved_by: list[str] = field(default_factory=list)
    rotate_with: list[str] = field(default_factory=list)
    flushes_queue: bool = False
    related_rules: list[str] = field(default_factory=list)
    merged_ids: list[str] = field(default_factory=list)
    count: int = 1
    brief: str = ""
    requeued: bool = False


@dataclass(order=True)
class _Queued:
    sort_key: tuple[int, int, float, float] = field(compare=True)
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
        self.sinks = [LogSink()] if sinks is None else sinks
        self.metrics = metrics or Metrics()
        self.budget_override = budget_override
        self._queue: list[_Queued] = []
        self._counter = itertools.count()
        self._last_fired: dict[str, float] = {}  # rule_id -> t
        self._fires_this_stint: dict[str, int] = {}
        self._recent_texts: list[tuple[str, float]] = []
        self._last_call_t: float | None = None
        self._booking_undo: dict[
            str, tuple[str, float | None, float | None, tuple[int, int, int]]
        ] = {}
        self._calls_this_lap = 0
        self._calls_lap: tuple[int, int, int] = (0, 0, 0)
        self._run = 0
        self._run_phase = ""
        self._current: Call | None = None
        self.latest_snapshot: Snapshot | None = None
        # Driver input: presses and their effects (docs/12).
        self.quiet_until: float | None = None
        self.silent = False  # radio silent: calls go to the screen, not the headset
        self.on_press_event: Any = None  # callable(payload dict) -> hub broadcast
        self._spoken_calls: list[tuple[Call, float]] = []  # (call, est. speech end t)
        self._negatives: dict[str, int] = {}
        self._neg_mute_until: dict[str, int] = {}  # rule_id -> lap
        self._cooldown_mult: dict[str, float] = {}
        # Persisted per-rule cooldown multipliers from graded review (pitwall tune).
        self.tuned_cooldown: dict[str, float] = {}
        self._acked: dict[str, int] = {}  # rule_id -> lap acknowledged on
        self._defs: dict[str, RuleDefModel] = {}
        self._reply_n: dict[str, int] = {}
        self._focus_until = 0.0
        self._rng = random.Random(0)
        self._digest_prefix_index = 0

    @property
    def last_call_t(self) -> float | None:
        return self._last_call_t

    # -- submission ----------------------------------------------------------

    def submit(self, candidates: list[Candidate], snapshot: Snapshot) -> None:
        self.latest_snapshot = snapshot
        self._refresh_queue(snapshot)
        # Suppression windows run on snapshot time so a max-speed replay
        # decides identically to 1x (docs/07 determinism).
        now = snapshot.now
        allowed_p = _VERBOSITY_PRIORITIES[self.policy.verbosity]
        for cand in candidates:
            definition = cand.rule.defn
            self._defs[cand.rule.id] = definition
            urgency = definition.urgency_class(cand.priority)
            rank = _URGENCY_RANK[urgency]
            decision_m, decision_s, promoted = self._decision_distance(
                urgency, definition.decision_point, snapshot
            )
            cand.inputs = {
                **cand.inputs,
                "decision_m": decision_m,
                "decision_s": decision_s,
                "promoted": promoted,
            }
            call = Call(
                screen_only=cand.screen_only or self.policy.verbosity == "silent",
                id=f"c-{next(self._counter)}",
                rule_id=cand.rule.id,
                priority=cand.priority,
                text=cand.text,
                tags=list(cand.tags),
                deadline_ms=int(self.policy.deadlines_s.get(cand.priority, 1.5) * 1000),
                lap=snapshot.lap_num,
                t=now,
                trigger_t=cand.trigger_t,
                still_true=cand.still_true,
                current=cand.current,
                conflict_group=definition.conflict_group,
                inputs=dict(cand.inputs),
                urgency=urgency,
                rank=rank,
                decision_point=definition.decision_point,
                decision_m=decision_m,
                decision_s=decision_s,
                promoted=promoted,
                outcome_score=cand.outcome_score,
                location_ref=definition.location_ref,
                resolved_by=list(definition.resolved_by),
                rotate_with=list(definition.rotate_with),
                flushes_queue=definition.flushes_queue,
                brief=cand.brief,
            )
            if cand.obvious:
                self._log(cand, snapshot, now, "suppressed", "obvious", call.id)
                continue
            if cand.provisional_silent:
                self._log(cand, snapshot, now, "suppressed", "provisional", call.id)
                continue
            if self._resolved_before_queue(call, now):
                self._log(cand, snapshot, now, "suppressed", "resolved", call.id)
                continue
            if self._coalesce(call, cand, snapshot, now):
                continue
            self._drop_escalated(call)
            conflict = self._resolve_conflict(call, snapshot, now)
            if conflict is not None:
                self._log(cand, snapshot, now, "suppressed", conflict, call.id)
                continue
            suppressed = self._suppression_reason(cand, snapshot, now, allowed_p)
            not_before = now
            if suppressed == "min_gap":
                assert self._last_call_t is not None
                not_before = self._last_call_t + self.policy.min_gap_s
                suppressed = None if not_before - now <= self.policy.min_gap_defer_s else "budget"
            if suppressed is not None:
                self._log(cand, snapshot, now, "suppressed", suppressed, call.id)
                continue
            if now < self._focus_until and call.urgency in ("info", "coaching"):
                self._log(cand, snapshot, now, "suppressed", "focus", call.id)
                continue
            if call.urgency == "reply" and self._apply_reply_absorption(call):
                self._log(cand, snapshot, now, "suppressed", "absorbed", call.id)
                continue
            call.not_before = not_before
            self._book_call(call, cand, not_before)
            if call.urgency == "safety" and "systems" not in call.tags:
                self._focus_until = now + self.policy.focus_window_s
                for queued in list(self._queue):
                    if queued.call.urgency in ("info", "coaching"):
                        self._drop_queued(queued.call.id, "focus")
            self._drop_resolved_by(call)
            if call.urgency != "reply":
                self._apply_reply_absorption(call)
            self._queue_call(call)
            self._log(cand, snapshot, now, "queued", None, call_id=call.id)
            if call.flushes_queue:
                self._flush_for(call)

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
        cooldown = (
            d.cooldown_s
            * self._cooldown_mult.get(cd_key, 1.0)
            * self.tuned_cooldown.get(cand.rule.id, 1.0)
        )
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
                return "min_gap"
        return None

    def _book_call(self, call: Call, cand: Candidate, now: float) -> None:
        cd_key = cand.rule.defn.cooldown_group or cand.rule.id
        self._booking_undo[call.id] = (
            cd_key,
            self._last_fired.get(cd_key),
            self._last_call_t,
            self._calls_lap,
        )
        self._last_fired[cd_key] = now
        self._fires_this_stint[cand.rule.id] = self._fires_this_stint.get(cand.rule.id, 0) + 1
        self._recent_texts.append((cand.text, now))
        self._last_call_t = now
        if call.priority != 1:
            self._calls_this_lap += 1

    def _undo_booking(self, call: Call) -> None:
        booking = self._booking_undo.pop(call.id, None)
        if booking is None:
            return
        cd_key, prev_last_fired, prev_last_call_t, calls_lap = booking
        if self._last_fired.get(cd_key) == call.not_before:
            if prev_last_fired is None:
                self._last_fired.pop(cd_key, None)
            else:
                self._last_fired[cd_key] = prev_last_fired
        self._fires_this_stint[call.rule_id] = max(
            0, self._fires_this_stint.get(call.rule_id, 0) - 1
        )
        if call.priority != 1 and self._calls_lap == calls_lap:
            self._calls_this_lap = max(0, self._calls_this_lap - 1)
        for i, recent in enumerate(self._recent_texts):
            if recent == (call.text, call.not_before):
                del self._recent_texts[i]
                break
        if self._last_call_t == call.not_before:
            self._last_call_t = prev_last_call_t

    def _decision_distance(
        self, urgency: str, point: str, snapshot: Snapshot
    ) -> tuple[float | None, float | None, bool]:
        if urgency != "execution" or not point or snapshot.track_length_m <= 0:
            return None, None, False
        if point == "pit_entry" and snapshot.pit_entry_m > 0:
            distance = (snapshot.pit_entry_m - snapshot.lap_distance) % snapshot.track_length_m
        else:
            distance = snapshot.track_length_m - snapshot.lap_distance
        speed = max(snapshot.speed_kmh / 3.6, 10.0)
        seconds = distance / speed
        return distance, seconds, seconds <= self.policy.decision_near_s

    def _queue_key(self, call: Call) -> tuple[int, int, float, float]:
        promoted = call.urgency == "execution" and call.promoted
        decision_s = call.decision_s if promoted and call.decision_s is not None else 0.0
        return (-1 if promoted else call.rank, 0 if promoted else 1, decision_s, call.t)

    def _queue_call(self, call: Call) -> None:
        heapq.heappush(self._queue, _Queued(self._queue_key(call), call))

    def _refresh_queue(self, snapshot: Snapshot) -> None:
        for queued in self._queue:
            call = queued.call
            call.decision_m, call.decision_s, call.promoted = self._decision_distance(
                call.urgency, call.decision_point, snapshot
            )
            call.inputs.update(
                {
                    "decision_m": call.decision_m,
                    "decision_s": call.decision_s,
                    "promoted": call.promoted,
                }
            )
            queued.sort_key = self._queue_key(call)
        heapq.heapify(self._queue)

    def _resolved_before_queue(self, call: Call, now: float) -> bool:
        if not call.resolved_by:
            return False
        resolver_ids = set(call.resolved_by)
        if any(queued.call.rule_id in resolver_ids for queued in self._queue):
            return True
        for spoken, end_t in reversed(self._spoken_calls):
            spoken_t = end_t - len(spoken.text) / _CHARS_PER_SECOND
            if now - spoken_t > self.policy.resolved_window_s:
                break
            if now >= spoken_t and spoken.rule_id in resolver_ids:
                return True
        return False

    def _coalesce(self, call: Call, cand: Candidate, snapshot: Snapshot, now: float) -> bool:
        queued = next(
            (item.call for item in self._queue if item.call.rule_id == call.rule_id),
            None,
        )
        if queued is None:
            return False
        queued.count += 1
        queued.merged_ids.append(call.id)
        definition = self._defs.get(call.rule_id)
        if definition is not None and definition.say_many:
            template = definition.say_many[(queued.count - 2) % len(definition.say_many)]
            try:
                queued.text = template.format(
                    count=queued.count,
                    count_word=_COUNT_WORDS.get(queued.count, str(queued.count)),
                )
            except (IndexError, KeyError, ValueError):
                queued.text = template
        queued.inputs["count"] = queued.count
        queued.inputs["merged_ids"] = list(queued.merged_ids)
        for index, recent in enumerate(self._recent_texts):
            if recent[0] == call.text and recent[1] == queued.not_before:
                self._recent_texts[index] = (queued.text, queued.not_before)
                break
        cand.inputs = {**cand.inputs, "merged_into": queued.id}
        self._log(cand, snapshot, now, "merged", None, call.id)
        return True

    def _drop_queued(self, call_id: str, reason: str) -> Call | None:
        kept: list[_Queued] = []
        dropped: Call | None = None
        for queued in self._queue:
            if queued.call.id == call_id and dropped is None:
                dropped = queued.call
                self._undo_booking(dropped)
                self._log_call(dropped, "suppressed", reason)
            else:
                kept.append(queued)
        if dropped is not None:
            self._queue = kept
            heapq.heapify(self._queue)
        return dropped

    def _drop_resolved_by(self, call: Call) -> None:
        for queued in list(self._queue):
            if call.rule_id in queued.call.resolved_by:
                self._drop_queued(queued.call.id, "resolved")

    def _drop_escalated(self, call: Call) -> None:
        definition = self._defs[call.rule_id]
        escalated = set(definition.escalates)
        for queued in list(self._queue):
            if queued.call.rule_id in escalated:
                self._drop_queued(queued.call.id, "superseded_escalation")

    def _apply_reply_absorption(self, call: Call) -> bool:
        if call.urgency == "reply":
            if any(
                queued.call.urgency != "reply" and queued.call.rule_id in call.related_rules
                for queued in self._queue
            ):
                return True
            return False
        for queued in list(self._queue):
            reply = queued.call
            if reply.urgency == "reply" and call.rule_id in reply.related_rules:
                self._drop_queued(reply.id, "absorbed")
                if call.rank > _URGENCY_RANK["reply"]:
                    call.rank = _URGENCY_RANK["reply"]
        return False

    def _flush_for(self, call: Call) -> None:
        for queued in list(self._queue):
            if queued.call.id != call.id and queued.call.urgency != "safety":
                self._drop_queued(queued.call.id, "flushed")

    def _resolve_conflict(self, call: Call, snapshot: Snapshot, now: float) -> str | None:
        definition = self._defs[call.rule_id]
        for queued in list(self._queue):
            rival = queued.call
            if rival.rule_id == call.rule_id:
                continue
            same_group = bool(call.conflict_group) and call.conflict_group == rival.conflict_group
            rotates = rival.rule_id in call.rotate_with or call.rule_id in rival.rotate_with
            supersedes = rival.rule_id in definition.supersedes
            rival_definition = self._defs.get(rival.rule_id)
            reverse_supersedes = (
                rival_definition is not None and call.rule_id in rival_definition.supersedes
            )
            if not (same_group or rotates or supersedes or reverse_supersedes):
                continue
            holds = True
            if rival.current is not None:
                try:
                    holds = bool(rival.current(snapshot))
                except Exception:
                    pass
            if supersedes or not holds:
                self._drop_queued(rival.id, "superseded")
                continue
            if reverse_supersedes:
                return "conflict"
            call_missed = (
                call.urgency == "execution"
                and bool(call.decision_point)
                and call.decision_s is not None
                and call.decision_s < self.policy.decision_missed_s
            )
            rival_missed = (
                rival.urgency == "execution"
                and bool(rival.decision_point)
                and rival.decision_s is not None
                and rival.decision_s < self.policy.decision_missed_s
            )
            if rival_missed:
                self._drop_queued(rival.id, "decision_missed")
            if call_missed:
                return "decision_missed"
            if rival_missed:
                continue
            if rotates:
                if self._rng.choice((True, False)):
                    self._drop_queued(rival.id, "rotated")
                    continue
                return "rotated"
            if call.outcome_score is not None and rival.outcome_score is not None:
                difference = call.outcome_score - rival.outcome_score
                if abs(difference) > self.policy.outcome_tie_eps:
                    if difference > 0:
                        self._drop_queued(rival.id, "conflict_loser")
                        continue
                    return "conflict_loser"
            return "conflict"
        return None

    # -- output --------------------------------------------------------------

    def drain(self, now: float | None = None) -> list[Call]:
        """Pop due calls in urgency order and return calls emitted."""
        if now is None:
            now = self.latest_snapshot.now if self.latest_snapshot is not None else self.clock.now()
        emitted: list[Call] = []
        held: list[_Queued] = []
        current_at_start = self._current
        snap = self.latest_snapshot
        on_straight = bool(snap.on_straight) if snap is not None else False
        if snap is not None:
            self._refresh_queue(snap)
        while self._queue:
            q = heapq.heappop(self._queue)
            call = q.call
            prompt = "menu" in call.tags
            waits_for_straight = call.priority == 3 and self.policy.p3_straight_only
            deadline_ms = call.deadline_ms
            if waits_for_straight:
                deadline_ms += int(self.policy.p3_straight_wait_s * 1000)
            deadline_start = max(call.t, call.not_before, call.ready_t)
            if (now - deadline_start) * 1000 > deadline_ms:
                if not prompt:
                    self._log_call(call, "suppressed", "deadline")
                self._booking_undo.pop(call.id, None)
                continue
            if now < call.not_before or (waits_for_straight and not on_straight):
                held.append(q)
                continue
            if call.still_true is not None and self.latest_snapshot is not None:
                try:
                    if not call.still_true(self.latest_snapshot):
                        self._log_call(call, "suppressed", "revalidation")
                        self._booking_undo.pop(call.id, None)
                        continue
                except Exception:
                    pass
            if call.location_ref and self._coherence_conflict(call, now):
                self._log_call(call, "suppressed", "coherence")
                self._booking_undo.pop(call.id, None)
                continue
            if call.urgency == "info" and self._digest_allowed(call, now, on_straight):
                due_info = [call]
                due_info.extend(
                    item.call
                    for item in self._queue
                    if item.call.id != call.id
                    and item.call.urgency == "info"
                    and self._digest_call_ready(item.call, now, on_straight)
                )
                if len(due_info) >= 2:
                    call = self._make_digest(due_info, now, snap)
                    prompt = False
            if (
                self._current is current_at_start
                and current_at_start is not None
                and not call.screen_only
                and self._mid_sentence(current_at_start, now)
                and not self._preempts(call, current_at_start)
            ):
                speech_end = next(
                    (
                        end_t
                        for spoken, end_t in self._spoken_calls
                        if spoken.id == current_at_start.id
                    ),
                    None,
                )
                if speech_end is not None:
                    q.call.ready_t = max(q.call.ready_t, speech_end)
                held.append(q)
                continue
            if (
                self._current is not None
                and self._preempts(call, self._current)
                and self._mid_sentence(self._current, now)
            ):
                interrupted = self._current
                for sink in self.sinks:
                    sink.cancel(interrupted.id)
                if "reply" in interrupted.tags and "menu" not in interrupted.tags:
                    if not interrupted.requeued:
                        interrupted.requeued = True
                        interrupted.t = now
                        interrupted.not_before = now
                        self._queue_call(interrupted)
                        self._log_call(interrupted, "requeued", "preempted")
                    else:
                        self._log_call(interrupted, "suppressed", "preempted")
                else:
                    self._log_call(interrupted, "suppressed", "preempted")
                self._current = None
            spoken_t = self.clock.now()
            muted = call.screen_only or self._silenced(call)
            for sink in self.sinks:
                if prompt and not sink.speaks_audio:
                    continue
                if muted and sink.speaks_audio:
                    continue
                sink.speak(call)
            self._current = call
            if prompt:
                self._booking_undo.pop(call.id, None)
                continue  # menu item names: audio only, not logged or repeatable
            self.metrics.note_trigger_to_speak(call.trigger_t, spoken_t)
            self._spoken_calls.append((call, now + len(call.text) / _CHARS_PER_SECOND))
            self._spoken_calls = self._spoken_calls[-16:]
            self._log_call(call, "fired", None)
            self._booking_undo.pop(call.id, None)
            emitted.append(call)
        for q in held:
            heapq.heappush(self._queue, q)
        return emitted

    def _effective_class(self, call: Call) -> str:
        if call.rank == _URGENCY_RANK["reply"] and call.urgency != "reply":
            return "reply"
        return call.urgency

    def _preempts(self, incoming: Call, current: Call) -> bool:
        if "menu" in current.tags:
            return False
        new_class = self._effective_class(incoming)
        current_class = self._effective_class(current)
        if new_class == "safety":
            return not (current_class == "execution" and current.promoted)
        if new_class == "execution":
            if current_class in ("info", "coaching"):
                return True
            return current_class == "tactical" and incoming.promoted
        if new_class == "reply":
            return current_class in ("info", "coaching")
        if new_class == "tactical":
            return current_class in ("info", "coaching")
        return False

    def _mid_sentence(self, call: Call, now: float) -> bool:
        return any(spoken.id == call.id and now < end_t for spoken, end_t in self._spoken_calls)

    def _coherence_conflict(self, call: Call, now: float) -> bool:
        for spoken, end_t in reversed(self._spoken_calls):
            spoken_t = end_t - len(spoken.text) / _CHARS_PER_SECOND
            if now - spoken_t > self.policy.coherence_window_s:
                break
            if now >= spoken_t and (spoken.urgency == "safety" or "traffic" in spoken.tags):
                return True
        return False

    def _battle_active(self, snapshot: Snapshot | None) -> bool:
        if snapshot is None:
            return False
        return any(
            0 < gap <= self.policy.digest_battle_gap_s
            for gap in (snapshot.gap_ahead_s, snapshot.gap_behind_s)
        )

    def _digest_call_ready(self, call: Call, now: float, on_straight: bool) -> bool:
        deadline_ms = call.deadline_ms
        if call.priority == 3 and self.policy.p3_straight_only:
            deadline_ms += int(self.policy.p3_straight_wait_s * 1000)
        deadline_start = max(call.t, call.not_before, call.ready_t)
        if (now - deadline_start) * 1000 > deadline_ms:
            return False
        if now < call.not_before:
            return False
        if call.priority == 3 and self.policy.p3_straight_only and not on_straight:
            return False
        if call.still_true is not None and self.latest_snapshot is not None:
            try:
                if not call.still_true(self.latest_snapshot):
                    return False
            except Exception:
                pass
        return not (call.location_ref and self._coherence_conflict(call, now))

    def _digest_allowed(self, call: Call, now: float, on_straight: bool) -> bool:
        if self._battle_active(self.latest_snapshot):
            return False
        if any(item.call.rank <= 3 for item in self._queue):
            return False
        return self._digest_call_ready(call, now, on_straight)

    def _make_digest(self, calls: list[Call], now: float, snapshot: Snapshot | None) -> Call:
        all_calls = sorted(calls, key=lambda item: item.t, reverse=True)
        maximum = self.policy.digest_max_items
        selected = all_calls[:maximum]
        selected_ids = {call.id for call in selected}
        grouped_ids = {call.id for call in all_calls}
        self._queue = [item for item in self._queue if item.call.id not in grouped_ids]
        heapq.heapify(self._queue)
        prefix = ""
        prefixes = self.policy.digest_prefixes
        if prefixes:
            prefix = prefixes[self._digest_prefix_index % len(prefixes)]
            self._digest_prefix_index += 1
        briefs = [call.brief or call.text for call in selected]
        non_p1_count = sum(call.priority != 1 for call in all_calls)
        if non_p1_count:
            self._calls_this_lap = max(0, self._calls_this_lap - non_p1_count + 1)
        digest = Call(
            id=f"c-{next(self._counter)}",
            rule_id="digest",
            priority=3 if non_p1_count else 1,
            text=prefix + ". ".join(briefs),
            tags=[],
            deadline_ms=3000,
            lap=snapshot.lap_num if snapshot is not None else selected[0].lap,
            t=now,
            trigger_t=min(call.trigger_t for call in selected),
            urgency="info",
            rank=_URGENCY_RANK["info"],
            inputs={"digest_ids": [call.id for call in selected]},
            merged_ids=[call.id for call in selected],
            count=len(selected),
            brief=prefix + ". ".join(briefs),
        )
        for source in all_calls:
            source.inputs["digest_id"] = digest.id
            self._booking_undo.pop(source.id, None)
            if source.id in selected_ids:
                self._log_call(source, "digested", "digest")
            else:
                self._log_call(source, "digest_overflow", "digest")
        return digest

    def _silenced(self, call: Call) -> bool:
        if not self.silent or "reply" in call.tags:
            return False
        return not (call.priority == 1 and self.input.silent_keeps_p1)

    def toggle_silent(self, snapshot: Snapshot) -> None:
        """Radio silent on/off: leave the driver alone; the screen keeps the radio."""
        self.silent = not self.silent
        now = snapshot.now
        self._log_press(now, snapshot, "silent_on" if self.silent else "silent_off", None, None)
        pool = self.input.silent_on_replies if self.silent else self.input.silent_off_replies
        self._reply(list(pool), snapshot)
        self._broadcast_press(
            {"kind": "silent" if self.silent else "unsilent", "lap": snapshot.lap_num, "text": ""}
        )

    def announce_mindset(self, name: str, snapshot: Snapshot) -> None:
        """Log + voice-confirm a live mindset switch (docs/12)."""
        self.log.mindset = name
        self._log_press(snapshot.now, snapshot, f"mindset_{name}", None, None)
        reply = self.input.mindset_replies.get(name)
        self._reply([reply] if reply else [f"Copy, {name}."], snapshot)
        self._broadcast_press({"kind": "mindset", "lap": snapshot.lap_num, "text": name})

    # -- driver menu (docs/12) -------------------------------------------------

    def menu_prompt(self, text: str, snapshot: Snapshot) -> None:
        """Speak a highlighted menu item: short, replaces any earlier prompt."""
        self.cancel_menu_prompt()
        self._push_reply(text, "menu", ["reply", "menu"], 1500, snapshot)

    def menu_reply(
        self, text: str, rule_id: str, snapshot: Snapshot, related_rules: list[str] | None = None
    ) -> None:
        """Pit-wall answer to a menu pick: P1 reply, bypasses budget and silence."""
        self._push_reply(
            text,
            rule_id,
            ["reply", "menu_answer"],
            4000,
            snapshot,
            related_rules=related_rules or [],
        )

    def cancel_menu_prompt(self) -> None:
        kept = [q for q in self._queue if "menu" not in q.call.tags]
        if len(kept) != len(self._queue):
            for queued in self._queue:
                if "menu" in queued.call.tags:
                    self._booking_undo.pop(queued.call.id, None)
            self._queue = kept
            heapq.heapify(self._queue)
        if self._current is not None and "menu" in self._current.tags:
            for sink in self.sinks:
                sink.cancel(self._current.id)
            self._current = None

    def _push_reply(
        self,
        text: str,
        rule_id: str,
        tags: list[str],
        deadline_ms: int,
        snapshot: Snapshot,
        related_rules: list[str] | None = None,
    ) -> None:
        now = snapshot.now
        call = Call(
            id=f"c-{next(self._counter)}",
            rule_id=rule_id,
            priority=1,
            text=text,
            tags=tags,
            deadline_ms=deadline_ms,
            lap=snapshot.lap_num,
            t=now,
            trigger_t=now,
            urgency="reply",
            rank=_URGENCY_RANK["reply"],
            related_rules=list(related_rules or []),
        )
        if self._apply_reply_absorption(call):
            self._log_call(call, "suppressed", "absorbed")
            return
        self._queue_call(call)

    def purge(self, reason: str, now: float | None = None) -> int:
        """Drop every queued (not yet spoken) call, logging each as suppressed."""
        purged = 0
        while self._queue:
            call = heapq.heappop(self._queue).call
            self._booking_undo.pop(call.id, None)
            self._log_call(call, "suppressed", reason)
            purged += 1
        return purged

    def reset_session(self, session_uid: int = 0) -> None:
        """New session: clear the queue and per-stint/per-lap budgets."""
        self._queue.clear()
        self._booking_undo.clear()
        self._fires_this_stint.clear()
        self._calls_this_lap = 0
        self._calls_lap = (0, 0, 0)
        self._run = 0
        self._run_phase = ""
        self._current = None
        self.quiet_until = None
        self._focus_until = 0.0
        self._rng = random.Random(session_uid)
        self._digest_prefix_index = 0
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
        if press.kind == "silent" or (
            press.kind == "bookmark" and self.input.long_press == "silent"
        ):
            self.toggle_silent(snapshot)
            return
        target: Call | None = None
        for call, end_t in reversed(self._spoken_calls):
            if "reply" in call.tags:
                continue
            d = self._defs.get(call.rule_id)
            window = self.input.response_window_s
            if d is not None and d.response_window_s is not None:
                window = d.response_window_s
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
            self._log_press(
                now,
                snapshot,
                "bookmark",
                None,
                None,
                extra={"kind": "hold", "context": snapshot_scalars(snapshot)},
            )
            self._broadcast_press(payload)
            return
        if target is None:
            if press.kind == "ack" and self.quiet_until is not None and now < self.quiet_until:
                self.quiet_until = None
                self._log_press(now, snapshot, "quiet_off", None, None)
                self._reply(["Radio's back on.", "Back with you."], snapshot)
            elif press.kind == "ack":
                # Say again: re-speak the last spoken call without booking it.
                said = [(c, t) for c, t in self._spoken_calls if "reply" not in c.tags]
                if (
                    self.input.say_again
                    and said
                    and now - said[-1][1] <= self.input.say_again_window_s
                ):
                    last, _ = said[-1]
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
                        inputs={**last.inputs, "repeat_of": last.id},
                        urgency=last.urgency,
                        rank=last.rank,
                        decision_point=last.decision_point,
                        decision_m=last.decision_m,
                        decision_s=last.decision_s,
                        promoted=last.promoted,
                        outcome_score=last.outcome_score,
                        location_ref=last.location_ref,
                        resolved_by=list(last.resolved_by),
                        rotate_with=list(last.rotate_with),
                        flushes_queue=last.flushes_queue,
                        related_rules=list(last.related_rules),
                        brief=last.brief,
                    )
                    self._queue_call(replay)
                else:
                    payload["kind"] = "bookmark"
                    self._log_press(
                        now,
                        snapshot,
                        "bookmark",
                        None,
                        None,
                        extra={"kind": "tap", "context": snapshot_scalars(snapshot)},
                    )
                    self._reply(self.input.bookmark_replies, snapshot)
            else:  # neg with no target: quiet for quiet_minutes
                self.quiet_until = now + self.input.quiet_minutes * 60
                self._log_press(now, snapshot, "quiet_until", None, None)
                mins = f"{self.input.quiet_minutes:g}"
                self._reply(
                    [f"Copy, going quiet for {mins} minutes. Click to undo."],
                    snapshot,
                )
            self._broadcast_press(payload)
            return
        grade = "good" if press.kind == "ack" else "noise"
        self._log_press(
            now,
            snapshot,
            press.kind,
            target,
            press.kind,
            extra={
                "grade": grade,
                "grade_source": "press",
                "grade_call_id": target.inputs.get("repeat_of") or target.id,
            },
        )
        d = self._defs.get(target.rule_id)
        own = (d.on_ack if press.kind == "ack" else d.on_neg) if d is not None else ""
        pool = [own] if isinstance(own, str) and own else list(own) if own else []
        generic = self.input.ack_replies if press.kind == "ack" else self.input.neg_replies
        self._reply(pool or generic, snapshot)
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

    def _reply(self, pool: list[str], snapshot: Snapshot) -> None:
        """Queue a short spoken reply to a press; bypasses quiet, silent and budgets."""
        if not self.input.spoken_replies or not pool:
            return
        now = snapshot.now
        n = self._reply_n.get(pool[0], 0)
        self._reply_n[pool[0]] = n + 1
        reply = Call(
            id=f"c-{next(self._counter)}",
            rule_id="reply",
            priority=1,
            text=pool[n % len(pool)],
            tags=["reply"],
            deadline_ms=3000,
            lap=snapshot.lap_num,
            t=now,
            trigger_t=now,
            urgency="reply",
            rank=_URGENCY_RANK["reply"],
        )
        self._queue_call(reply)

    def _log_press(
        self,
        now: float,
        snap: Snapshot,
        outcome: str,
        call: Call | None,
        by: str | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        record: dict[str, Any] = {
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
        record.update(extra or {})
        self.log.write(record)

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
                "urgency": cand.rule.defn.urgency_class(cand.priority),
                "rank": _URGENCY_RANK[cand.rule.defn.urgency_class(cand.priority)],
                "outcome": outcome,
                "suppressed_by": by,
                "inputs": cand.inputs,
                "text": cand.text,
                "active_plan": snap.active_plan,
                "on_plan": snap.on_plan if snap.active_plan else None,
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
                "urgency": call.urgency,
                "rank": call.rank,
                "outcome": outcome,
                "suppressed_by": by,
                "inputs": {
                    **call.inputs,
                    "decision_m": call.decision_m,
                    "decision_s": call.decision_s,
                    "promoted": call.promoted,
                    "merged_ids": list(call.merged_ids),
                    "count": call.count,
                },
                "text": call.text,
                "active_plan": snap.active_plan if snap else "",
                "on_plan": snap.on_plan if snap and snap.active_plan else None,
            }
        )
