"""Call arbitration (ADR 0010): choose which queued P2/P3 call goes first.

The rules produce every candidate, fact and word. An arbitrator only orders
calls that already passed the dispatcher's suppression layers; P1 never
reaches it. `HeapArbitrator` is the status quo `(priority, t)` order.
`JevArbitrator` asks Jev (TypeSafe, via the Vercel AI Gateway evaluate API) for
a `best_call` choice and applies it only through a confidence and margin gate,
a sticky window and flap detection; anything else falls back to the heap."""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol

import httpx

from pitwall.config.models import JevSettings
from pitwall.state.session import Snapshot

ArbitratedBy = Literal[
    "heap", "jev", "jev_timeout", "jev_error", "jev_abstain", "jev_margin", "jev_flap"
]

NONE_CHOICE = "none"

DOCTRINE: tuple[str, ...] = (
    "Only the listed calls exist. Never assume another call or another wording.",
    "Pick the call the driver should hear first, given this lap and this stint.",
    "A call that changes what the driver does in the next lap beats a status update.",
    "A time-boxed call (open pit window, closing gap, a battle) beats one that can wait.",
    "Keep the previous pick unless the situation behind it changed.",
    "Answer none when the rules' order (heap_order) is as good as any other order.",
)

_PICK_INSTRUCTIONS = (
    "You are a race engineer's call arbitrator. The rules queued these radio calls at "
    "the same time. Choose the one call the driver should hear first, or none to keep "
    "the rules' order. Follow the doctrine."
)

_VERDICT_INSTRUCTIONS = (
    "The pit wall spoke `pick` first out of these queued radio calls. Grade that choice "
    "against the doctrine and the situation."
)


@dataclass(frozen=True, slots=True)
class ArbCandidate:
    """One queued call as the arbitrator sees it (names already masked)."""

    call_id: str
    rule_id: str
    priority: int
    t: float
    lap: int
    session_time: float
    text: str
    tags: tuple[str, ...] = ()
    inputs: Mapping[str, Any] = field(default_factory=dict)

    @property
    def token(self) -> str:
        """Identity that survives a replay (call ids restart per process)."""
        return f"{self.rule_id}@{self.lap}:{self.session_time:.2f}"

    def to_json(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "rule_id": self.rule_id,
            "priority": self.priority,
            "t": self.t,
            "lap": self.lap,
            "session_time": self.session_time,
            "text": self.text,
            "tags": list(self.tags),
            "inputs": dict(self.inputs),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ArbCandidate:
        return cls(
            call_id=str(data["call_id"]),
            rule_id=str(data["rule_id"]),
            priority=int(data["priority"]),
            t=float(data.get("t", 0.0)),
            lap=int(data.get("lap", 0)),
            session_time=float(data.get("session_time", 0.0)),
            text=str(data.get("text", "")),
            tags=tuple(data.get("tags", ())),
            inputs=dict(data.get("inputs", {})),
        )


@dataclass(frozen=True, slots=True)
class Ranking:
    """Speaking order for one decision point. `order[0]` goes first."""

    order: tuple[str, ...]
    arbitrated_by: ArbitratedBy
    pick: str | None = None  # Jev's applied pick; None when the heap order stands
    confidence: float | None = None
    latency_ms: float | None = None
    model: str = ""

    def fields(self) -> dict[str, Any]:
        """Arbitration fields carried on decision-log / calls records."""
        return {
            "arbitrated_by": self.arbitrated_by,
            "arb_pick": self.pick,
            "arb_confidence": self.confidence,
            "arb_latency_ms": self.latency_ms,
            "arb_model": self.model or None,
        }


def heap_order(candidates: Sequence[ArbCandidate]) -> tuple[str, ...]:
    return tuple(c.call_id for c in sorted(candidates, key=lambda c: (c.priority, c.t)))


def decision_key(session_uid: int, candidates: Sequence[ArbCandidate]) -> str:
    """Replay-stable key of a decision point: session plus candidates in heap order."""
    by_id = {c.call_id: c for c in candidates}
    return f"{session_uid}/" + "|".join(by_id[i].token for i in heap_order(candidates))


def promote(order: Sequence[str], call_id: str) -> tuple[str, ...]:
    return (call_id, *(i for i in order if i != call_id))


class Arbitrator(Protocol):
    @property
    def model(self) -> str: ...

    def rank(self, candidates: Sequence[ArbCandidate], digest: Mapping[str, Any]) -> Ranking: ...


class HeapArbitrator:
    """Status quo: `(priority, t)` order, no model."""

    model = ""

    def rank(self, candidates: Sequence[ArbCandidate], digest: Mapping[str, Any]) -> Ranking:
        return Ranking(heap_order(candidates), "heap")


# -- digest -------------------------------------------------------------------


def _finite(x: float) -> float | None:
    return round(x, 3) if math.isfinite(x) else None


def name_labels(snap: Snapshot) -> dict[str, str]:
    """Driver names on the snapshot -> neutral labels (league privacy)."""
    pairs = (
        (snap.rival_ahead_name, "the car ahead"),
        (snap.rival_behind_name, "the car behind"),
        (snap.teammate_name, "your teammate"),
        (snap.rival_pit_exit_name, "the pit-exit rival"),
        (snap.pit_plan_rival_name, "the plan rival"),
        (snap.penalty_threat_name, "the penalty rival"),
        (snap.fastest_lap_name, "the fastest-lap holder"),
        (snap.contact_name, "the other car"),
        (snap.battle_result_name, "the battle rival"),
    )
    out: dict[str, str] = {}
    for name, label in pairs:
        if len(name) >= 3:
            out.setdefault(name, label)
    return out


def mask_names(value: Any, labels: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        for name, label in labels.items():
            value = value.replace(name, label)
        return value
    if isinstance(value, Mapping):
        return {k: mask_names(v, labels) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [mask_names(v, labels) for v in value]
    return value


def _stint_phase(snap: Snapshot) -> str:
    if snap.plan_window_open:
        return "pit_window"
    if snap.tyre_age_laps <= 2:
        return "early"
    if math.isfinite(snap.laps_of_pace) and snap.laps_of_pace <= 3:
        return "late"
    return "mid"


def _lap_trend_ms(snap: Snapshot) -> float | None:
    """Mean lap-to-lap change over the last valid laps on this compound (- = faster)."""
    laps = [lap for lap in snap.laps if lap.valid and lap.compound == snap.tyre_compound]
    laps = laps[-4:]
    if len(laps) < 2:
        return None
    deltas = [b.lap_time_ms - a.lap_time_ms for a, b in zip(laps, laps[1:], strict=False)]
    return round(sum(deltas) / len(deltas), 1)


def build_digest(snap: Snapshot) -> dict[str, Any]:
    """Facts the rules already computed, as Jev's shared state. No driver names."""
    return {
        "now": snap.now,
        "session_uid": snap.session_uid,
        "session": snap.session_kind,
        "lap": snap.lap_num,
        "total_laps": snap.total_laps,
        "laps_remaining": snap.laps_remaining,
        "position": snap.position,
        "race_phase": snap.race_phase,
        "safety_car": snap.safety_car_status,
        "stint_phase": _stint_phase(snap),
        "tyres": {
            "compound": snap.tyre_compound,
            "age_laps": snap.tyre_age_laps,
            "wear_max_pct": round(snap.wear_max_pct, 1),
            "deg_ms_per_lap": round(snap.deg_ms_per_lap, 1),
            "laps_of_pace": _finite(snap.laps_of_pace),
            "graining": snap.graining,
            "overheat": snap.overheat,
        },
        "gaps": {
            "ahead_s": _finite(snap.gap_ahead_s),
            "behind_s": _finite(snap.gap_behind_s),
            "trend_ahead_s": round(snap.gap_trend_ahead_s, 3),
            "trend_behind_s": round(snap.gap_trend_behind_s, 3),
        },
        "track_evolution": {
            "lap_trend_ms": _lap_trend_ms(snap),
            "weather": snap.weather,
            "rain_pct": snap.rain_pct_now,
        },
        "plan": {
            "active": snap.active_plan,
            "on_plan": snap.on_plan,
            "target_lap": snap.plan_target_lap,
            "window_open": snap.plan_window_open,
            "next_compound": snap.plan_next_compound,
        },
    }


def arb_candidate(
    call_id: str,
    rule_id: str,
    priority: int,
    t: float,
    lap: int,
    session_time: float,
    text: str,
    tags: Sequence[str],
    inputs: Mapping[str, Any],
    labels: Mapping[str, str],
) -> ArbCandidate:
    return ArbCandidate(
        call_id=call_id,
        rule_id=rule_id,
        priority=priority,
        t=t,
        lap=lap,
        session_time=session_time,
        text=str(mask_names(text, labels)),
        tags=tuple(tags),
        inputs=mask_names(dict(inputs), labels),
    )


# -- Jev ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Verdict:
    verdict: str  # right | wrong | unclear
    confidence: float
    best_call: str | None
    model: str


class JevError(Exception):
    pass


class JevArbitrator:
    """Ranks via `POST /v1/evaluate` with one `best_call` choice question."""

    def __init__(
        self,
        settings: JevSettings,
        *,
        api_key: str,
        client: httpx.Client | None = None,
        wall: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._api_key = api_key
        self._client = client or httpx.Client()
        self._wall = wall
        self._last_pick: tuple[str, float] | None = None  # (call_id, digest now)
        self._changes: list[float] = []  # digest times the pick changed inside sticky_s

    @property
    def model(self) -> str:
        return self.settings.model

    # -- request ------------------------------------------------------------

    def _state(
        self, candidates: Sequence[ArbCandidate], digest: Mapping[str, Any]
    ) -> dict[str, Any]:
        now = float(digest.get("now", 0.0))
        last = self._last_pick
        return {
            "situation": dict(digest),
            "candidates": [c.to_json() for c in candidates],
            "heap_order": list(heap_order(candidates)),
            "last_pick": last[0] if last else None,
            "last_pick_age_s": round(now - last[1], 2) if last else None,
            "doctrine": list(DOCTRINE),
        }

    def _evaluate(
        self, state: Mapping[str, Any], questions: Mapping[str, Any]
    ) -> tuple[dict[str, Any], str]:
        body = {"model": self.settings.model, "state": state, "questions": questions}
        resp = self._client.post(
            self.settings.endpoint,
            content=json.dumps(body, default=str),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.settings.timeout_ms / 1000,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
            raise JevError("response has no answers")
        return data["answers"], str(data.get("model") or self.settings.model)

    @staticmethod
    def _choice(
        answers: Mapping[str, Any], key: str, allowed: set[str]
    ) -> tuple[str, dict[str, float]]:
        ans = answers.get(key)
        if not isinstance(ans, dict) or ans.get("type") != "choice":
            raise JevError(f"{key}: not a choice answer")
        choice = ans.get("choice")
        probs_raw = ans.get("probabilities") or {}
        if choice not in allowed or not isinstance(probs_raw, dict):
            raise JevError(f"{key}: unknown choice {choice!r}")
        probs = {str(k): float(v) for k, v in probs_raw.items() if str(k) in allowed}
        return str(choice), probs

    @staticmethod
    def _criteria(candidates: Sequence[ArbCandidate]) -> dict[str, str]:
        crit = {c.call_id: f"P{c.priority} {c.rule_id}: {c.text}" for c in candidates}
        crit[NONE_CHOICE] = "No call is clearly better than the rules' order; keep it."
        return crit

    # -- ranking ------------------------------------------------------------

    def rank(self, candidates: Sequence[ArbCandidate], digest: Mapping[str, Any]) -> Ranking:
        candidates = sorted(candidates, key=lambda c: (c.priority, c.t))
        candidates = candidates[: self.settings.max_candidates]
        order = heap_order(candidates)
        if len(order) < 2:
            return Ranking(order, "heap")
        questions = {
            "best_call": {
                "type": "choice",
                "instructions": _PICK_INSTRUCTIONS,
                "criteria": self._criteria(candidates),
            }
        }
        t0 = self._wall()
        try:
            answers, model = self._evaluate(self._state(candidates, digest), questions)
            choice, probs = self._choice(answers, "best_call", {*order, NONE_CHOICE})
        except httpx.TimeoutException:
            ms = round((self._wall() - t0) * 1000, 1)
            return Ranking(order, "jev_timeout", latency_ms=ms, model=self.model)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, JevError):
            ms = round((self._wall() - t0) * 1000, 1)
            return Ranking(order, "jev_error", latency_ms=ms, model=self.model)
        ms = round((self._wall() - t0) * 1000, 1)
        ranking = self.gate(order, choice, probs, float(digest.get("now", 0.0)))
        return replace(ranking, latency_ms=ms, model=model)

    def gate(
        self, order: Sequence[str], choice: str, probs: Mapping[str, float], now: float
    ) -> Ranking:
        """Margin gate, sticky window and flap detection on one Jev answer."""
        order = tuple(order)
        conf = probs.get(choice, 0.0)
        if choice == NONE_CHOICE:
            return Ranking(order, "jev_abstain", confidence=conf)
        incumbent = order[0]
        if conf < self.settings.confidence_threshold or (
            choice != incumbent and conf < probs.get(incumbent, 0.0) + self.settings.margin
        ):
            return Ranking(order, "jev_margin", confidence=conf)
        last = self._last_pick
        if (
            last is not None
            and last[0] != choice
            and last[0] in order
            and now - last[1] < self.settings.sticky_s
        ):
            self._changes = [t for t in self._changes if now - t < self.settings.sticky_s]
            self._changes.append(now)
            if len(self._changes) >= 2:
                return Ranking(order, "jev_flap", confidence=conf)
            return Ranking(promote(order, last[0]), "jev", last[0], probs.get(last[0], 0.0))
        if last is None or last[0] != choice:
            self._last_pick = (choice, now)
        return Ranking(promote(order, choice), "jev", choice, conf)

    # -- grading (`pitwall tune --judge jev`) -------------------------------

    def judge(
        self, candidates: Sequence[ArbCandidate], digest: Mapping[str, Any], pick: str
    ) -> Verdict:
        ids = {c.call_id for c in candidates}
        state = {
            "situation": dict(digest),
            "candidates": [c.to_json() for c in candidates],
            "pick": pick,
            "doctrine": list(DOCTRINE),
        }
        questions = {
            "verdict": {
                "type": "choice",
                "instructions": _VERDICT_INSTRUCTIONS,
                "criteria": {
                    "right": "the pick was the best call to hear first",
                    "wrong": "another listed call should have gone first",
                    "unclear": "the situation does not separate the calls",
                },
            },
            "best_call": {
                "type": "choice",
                "instructions": _PICK_INSTRUCTIONS,
                "criteria": self._criteria(candidates),
            },
        }
        answers, model = self._evaluate(state, questions)
        verdict, vprobs = self._choice(answers, "verdict", {"right", "wrong", "unclear"})
        best, _ = self._choice(answers, "best_call", {*ids, NONE_CHOICE})
        return Verdict(
            verdict=verdict,
            confidence=vprobs.get(verdict, 0.0),
            best_call=None if best == NONE_CHOICE else best,
            model=model,
        )


# -- replay -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordedPick:
    """A live decision point as the decision log recorded it."""

    order: tuple[int, ...]  # indices into the heap order, speaking order first
    arbitrated_by: ArbitratedBy
    confidence: float | None
    model: str

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> RecordedPick:
        by = str(record.get("arbitrated_by") or "heap")
        conf = record.get("arb_confidence")
        return cls(
            order=tuple(int(i) for i in record.get("arb_order") or ()),
            arbitrated_by=_ARBITRATED_BY.get(by, "heap"),
            confidence=None if conf is None else float(conf),
            model=str(record.get("arb_model") or ""),
        )


_ARBITRATED_BY: dict[str, ArbitratedBy] = {
    "heap": "heap",
    "jev": "jev",
    "jev_timeout": "jev_timeout",
    "jev_error": "jev_error",
    "jev_abstain": "jev_abstain",
    "jev_margin": "jev_margin",
    "jev_flap": "jev_flap",
}


class RecordedArbitrator:
    """Replay: repeat the recorded live ranking; heap order where none was recorded."""

    model = ""

    def __init__(self, picks: Mapping[str, RecordedPick]) -> None:
        self.picks = dict(picks)
        self.hits = 0
        self.misses = 0

    def rank(self, candidates: Sequence[ArbCandidate], digest: Mapping[str, Any]) -> Ranking:
        heap = heap_order(candidates)
        rec = self.picks.get(decision_key(int(digest.get("session_uid", 0)), candidates))
        if rec is None or sorted(rec.order) != list(range(len(heap))):
            self.misses += 1
            return Ranking(heap, "heap")
        self.hits += 1
        order = tuple(heap[i] for i in rec.order)
        pick = order[0] if rec.arbitrated_by == "jev" else None
        return Ranking(order, rec.arbitrated_by, pick, rec.confidence, model=rec.model)


# -- shadow mode (`pitwall replay --arbitrate shadow`) -----------------------


@dataclass(frozen=True, slots=True)
class ShadowDecision:
    key: str
    lap: int
    candidates: tuple[ArbCandidate, ...]
    heap: tuple[str, ...]
    jev: Ranking


class ShadowArbitrator:
    """Asks the inner arbitrator at every decision point, keeps the heap order."""

    model = ""

    def __init__(self, inner: Arbitrator) -> None:
        self.inner = inner
        self.decisions: list[ShadowDecision] = []

    def rank(self, candidates: Sequence[ArbCandidate], digest: Mapping[str, Any]) -> Ranking:
        heap = heap_order(candidates)
        jev = self.inner.rank(candidates, digest)
        self.decisions.append(
            ShadowDecision(
                key=decision_key(int(digest.get("session_uid", 0)), candidates),
                lap=int(digest.get("lap", 0)),
                candidates=tuple(candidates),
                heap=heap,
                jev=jev,
            )
        )
        return Ranking(heap, "heap")


def shadow_report(decisions: Sequence[ShadowDecision]) -> dict[str, Any]:
    by: dict[str, int] = {}
    diffs: list[dict[str, Any]] = []
    for d in decisions:
        by[d.jev.arbitrated_by] = by.get(d.jev.arbitrated_by, 0) + 1
        if d.jev.order and d.jev.order[0] != d.heap[0]:
            rules = {c.call_id: c.rule_id for c in d.candidates}
            diffs.append(
                {
                    "lap": d.lap,
                    "heap_first": rules[d.heap[0]],
                    "jev_first": rules[d.jev.order[0]],
                    "confidence": d.jev.confidence,
                    "candidates": [rules[i] for i in d.heap],
                }
            )
    return {
        "decision_points": len(decisions),
        "by": dict(sorted(by.items())),
        "reordered": len(diffs),
        "diffs": diffs,
    }


def format_shadow_report(report: Mapping[str, Any]) -> str:
    lines = [
        f"shadow arbitration: {report['decision_points']} decision points, "
        f"{report['reordered']} where Jev would go first with a different call",
        "  " + ", ".join(f"{k}={v}" for k, v in report["by"].items()),
    ]
    for d in report["diffs"]:
        conf = "?" if d["confidence"] is None else f"{d['confidence']:.2f}"
        lines.append(
            f"  lap {d['lap']}: heap {d['heap_first']} -> jev {d['jev_first']} "
            f"(conf {conf}; queued {', '.join(d['candidates'])})"
        )
    return "\n".join(lines)


def api_key(settings: JevSettings) -> str:
    return os.environ.get(settings.api_key_env, "")


def build_live_arbitrator(settings: JevSettings) -> JevArbitrator | None:
    """Jev for the live dispatcher, or None (heap order) unless enabled + arbitrate + key."""
    if not (settings.enabled and settings.arbitrate):
        return None
    key = api_key(settings)
    if not key:
        print(f"jev: {settings.api_key_env} is not set; calls keep the heap order", flush=True)
        return None
    return JevArbitrator(settings, api_key=key)
