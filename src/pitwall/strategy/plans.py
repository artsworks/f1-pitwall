"""Named strategy plans (docs/03 "Strategy plans"): a small ranked set of
whole-race strategies (Plan A / B / C) plus the tracker that holds the active
plan, detects off-plan and switches. Pure and deterministic: the Engine feeds
plain values, the tracker emits plain values for rules, JSON and SQLite."""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from pitwall.model.deg import DegFit

DRY = (16, 17, 18)
LETTERS = {16: "S", 17: "M", 18: "H", 7: "I", 8: "W"}
WORDS = {16: "soft", 17: "medium", 18: "hard", 7: "inter", 8: "wet"}


def _f(src: Mapping[str, object], name: str, default: float) -> float:
    v = src.get(name, default)
    return float(v) if isinstance(v, int | float) else default


@dataclass(frozen=True, slots=True)
class CompoundModel:
    visual: int
    base_ms: float  # fresh-set lap time at age 0
    deg_ms_per_lap: float
    sets: int  # usable sets left, -1 = unknown (no Tyre Sets packet)


@dataclass(frozen=True, slots=True)
class PlanInputs:
    lap_num: int
    laps_remaining: int
    tyre_age: int
    current: int  # visual compound fitted now
    cur: DegFit
    cur_cliff_age: float
    compounds: tuple[CompoundModel, ...]
    used: frozenset[int]  # visual compounds already raced this session
    green_loss_s: float
    sc_loss_s: float


@dataclass(frozen=True, slots=True)
class PlanEval:
    compounds: tuple[int, ...]
    stop_laps: tuple[int, ...]
    window: tuple[int, int]  # first-stop window, (0, 0) when no stop
    race_time_s: float


@dataclass(frozen=True, slots=True)
class StrategyPlan:
    id: str
    kind: str  # primary | alternative | reactive
    compounds: tuple[int, ...]  # remaining stints, the fitted set first
    stop_laps: tuple[int, ...]
    window: tuple[int, int]
    race_time_s: float
    delta_s: float  # vs the active plan's race time (negative = faster)
    feasible: bool = True

    @property
    def stops(self) -> int:
        return len(self.compounds) - 1

    @property
    def sequence(self) -> str:
        return "-".join(LETTERS.get(c, "?") for c in self.compounds)

    @property
    def label(self) -> str:
        head = "no-stop" if self.stops == 0 else f"{self.stops}-stop"
        if self.kind == "reactive":
            return f"SC/VSC box now, {head} {self.sequence}"
        return f"{head} {self.sequence}"

    @property
    def window_text(self) -> str:
        a, b = self.window
        if a <= 0:
            return ""
        return f"L{a}" if a == b else f"L{a}-{b}"


@dataclass(frozen=True, slots=True)
class PlanEvent:
    lap: int
    kind: str  # set | switch | off | on
    from_plan: str
    to_plan: str
    reason: str
    delta_s: float
    sequence: str


@dataclass(frozen=True, slots=True)
class PlanState:
    plans: tuple[StrategyPlan, ...] = ()
    active: str = ""
    on_plan: bool = True
    off_s: float = 0.0  # active plan vs the best strategy available now
    off_reason: str = ""
    switch_count: int = 0
    switched_from: str = ""
    switch_reason: str = ""
    switch_lap: int = 0
    target_lap: int = 0  # active plan's next stop, 0 = no stop left
    window: tuple[int, int] = (0, 0)
    target_shift: int = 0  # target_lap now vs when the plan was set
    sequence: str = ""
    stops_left: int = 0

    @property
    def active_plan(self) -> StrategyPlan | None:
        return next((p for p in self.plans if p.id == self.active), None)


NO_STATE = PlanState()


def _cum(base: float, deg: float, age0: int, n: int, cliff_age: float, pen: float) -> list[float]:
    out = [0.0] * (n + 1)
    for i in range(n):
        age = age0 + i
        out[i + 1] = out[i] + base + deg * age + pen * max(0.0, age - cliff_age)
    return out


def feasible(inp: PlanInputs, seq: tuple[int, ...], th: Mapping[str, object]) -> bool:
    """Tyre sets left and the mandatory two-dry-compound rule."""
    if not seq or seq[0] != inp.current:
        return False
    models = {c.visual: c for c in inp.compounds}
    for c in set(seq[1:]):
        m = models.get(c)
        if m is None:
            return False
        if m.sets >= 0 and seq[1:].count(c) > m.sets:
            return False
    if _f(th, "plan_two_compound_rule", 1) and not (inp.used - set(DRY)):
        raced = inp.used | set(seq)
        if not raced - set(DRY) and len(raced & set(DRY)) < 2:
            return False
    return True


def evaluate(
    inp: PlanInputs,
    seq: tuple[int, ...],
    th: Mapping[str, object],
    *,
    stop_now: bool = False,
    first_loss_s: float | None = None,
) -> PlanEval | None:
    """Best stop laps for a compound sequence; None when it can't fit."""
    stops = len(seq) - 1
    r = inp.laps_remaining
    if stops < 0 or stops > 2 or r <= 0:
        return None
    pen = _f(th, "cliff_ms_per_lap", 1000)
    cliff_ms = _f(th, "tyre_cliff_ms", 1500)
    mn = int(_f(th, "plan_min_stint_laps", 3))
    last_k = r - max(mn, int(_f(th, "pit_min_laps_left", 2)))
    warm = _f(th, "outlap_warmup_s", 0.8)
    loss_ms = (inp.green_loss_s + warm) * 1000.0
    first_ms = (first_loss_s + warm) * 1000.0 if first_loss_s is not None else loss_ms
    cur = _cum(inp.cur.base_ms, inp.cur.deg_ms_per_lap, inp.tyre_age, r, inp.cur_cliff_age, pen)
    if stops == 0:
        return PlanEval(seq, (), (0, 0), cur[r] / 1000.0)
    models = {c.visual: c for c in inp.compounds}
    fresh = []
    for c in seq[1:]:
        m = models[c]
        cliff = math.inf if m.deg_ms_per_lap <= 0 else cliff_ms / m.deg_ms_per_lap
        fresh.append(_cum(m.base_ms, m.deg_ms_per_lap, 0, r, cliff, pen))
    k1s = [0] if stop_now else list(range(0, last_k + 1))
    per_k1: dict[int, tuple[float, tuple[int, ...]]] = {}
    for k1 in k1s:
        if k1 > last_k:
            continue
        if stops == 1:
            per_k1[k1] = (cur[k1] + fresh[0][r - k1] + first_ms, (k1,))
            continue
        best: tuple[float, tuple[int, ...]] | None = None
        for k2 in range(k1 + mn, last_k + 1):
            t = cur[k1] + fresh[0][k2 - k1] + fresh[1][r - k2] + first_ms + loss_ms
            if best is None or t < best[0]:
                best = (t, (k1, k2))
        if best is not None:
            per_k1[k1] = best
    if not per_k1:
        return None
    k_best = min(per_k1, key=lambda k: (per_k1[k][0], k))
    t_best, ks = per_k1[k_best]
    slack = _f(th, "plan_window_s", 1.5) * 1000.0
    lo = hi = k_best
    while lo - 1 in per_k1 and per_k1[lo - 1][0] - t_best <= slack:
        lo -= 1
    while hi + 1 in per_k1 and per_k1[hi + 1][0] - t_best <= slack:
        hi += 1
    lap = inp.lap_num
    return PlanEval(seq, tuple(lap + k for k in ks), (lap + lo, lap + hi), t_best / 1000.0)


def _sequences(inp: PlanInputs, th: Mapping[str, object]) -> list[tuple[int, ...]]:
    options = tuple(c.visual for c in inp.compounds) if inp.current in DRY else (inp.current,)
    max_stops = min(2, int(_f(th, "plan_max_stops", 2)))
    out: list[tuple[int, ...]] = []
    for n in range(max_stops + 1):
        for tail in itertools.product(options, repeat=n):
            seq = (inp.current, *tail)
            if feasible(inp, seq, th):
                out.append(seq)
    return out


def derive(inp: PlanInputs, th: Mapping[str, object]) -> list[PlanEval]:
    """Every feasible sequence, fastest first (ties: fewer stops, then order)."""
    evals = [e for s in _sequences(inp, th) if (e := evaluate(inp, s, th)) is not None]
    return sorted(evals, key=lambda e: (round(e.race_time_s, 3), len(e.compounds)))


def reactive(inp: PlanInputs, th: Mapping[str, object]) -> PlanEval | None:
    """Plan C: box this lap under SC/VSC at the neutralised loss."""
    best: PlanEval | None = None
    for seq in _sequences(inp, th):
        if len(seq) < 2:
            continue
        e = evaluate(inp, seq, th, stop_now=True, first_loss_s=inp.sc_loss_s)
        if e is not None and (best is None or e.race_time_s < best.race_time_s):
            best = e
    return best


def alternative(ranked: list[PlanEval], primary: PlanEval) -> PlanEval | None:
    """Plan B: best with a different stop count, else a different sequence."""
    for e in ranked:
        if len(e.compounds) != len(primary.compounds):
            return e
    return next((e for e in ranked if e.compounds != primary.compounds), None)


@dataclass
class PlanTracker:
    """Holds Plan A/B (frozen compound sequences) and the active plan across
    recomputes. Plan C is re-derived every time until it's taken."""

    seqs: dict[str, tuple[int, tuple[int, ...]]] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)
    active: str = ""
    on_plan: bool = True
    off_reason: str = ""
    switch_count: int = 0
    switched_from: str = ""
    switch_reason: str = ""
    switch_lap: int = 0
    target_ref: int = 0
    state: PlanState = NO_STATE
    events: list[PlanEvent] = field(default_factory=list)

    def reset(self) -> None:
        self.seqs.clear()
        self.kinds.clear()
        self.active = ""
        self.on_plan = True
        self.off_reason = ""
        self.switch_count = 0
        self.switched_from = ""
        self.switch_reason = ""
        self.switch_lap = 0
        self.target_ref = 0
        self.state = NO_STATE
        self.events.clear()

    def drain(self) -> list[PlanEvent]:
        out = list(self.events)
        self.events.clear()
        return out

    def _remaining(self, pid: str, stops_done: int) -> tuple[int, ...]:
        base, seq = self.seqs[pid]
        i = stops_done - base
        return seq[i:] if 0 <= i < len(seq) else ()

    def _set(self, pid: str, kind: str, stops_done: int, e: PlanEval) -> None:
        self.seqs[pid] = (stops_done, e.compounds)
        self.kinds[pid] = kind

    def _switch(self, to: str, reason: str, lap: int, off: float, e: PlanEval | None) -> None:
        self.events.append(PlanEvent(lap, "switch", self.active, to, reason, off, _seq_text(e)))
        if self.active == "C" and to != "C":
            self.seqs.pop("C", None)
        self.switched_from = self.active
        self.active = to
        self.switch_reason = reason
        self.switch_count += 1
        self.switch_lap = lap
        self.on_plan = True
        self.off_reason = ""
        self.target_ref = e.stop_laps[0] if e is not None and e.stop_laps else 0

    def update(
        self,
        inp: PlanInputs,
        th: Mapping[str, object],
        *,
        stops_done: int,
        neutralised: bool,
        cheap_stop: bool,
    ) -> PlanState:
        ranked = derive(inp, th)
        if not ranked:
            return self.state
        lap = inp.lap_num
        if not self.seqs:
            a = ranked[0]
            self._set("A", "primary", stops_done, a)
            b = alternative(ranked, a)
            if b is not None:
                self._set("B", "alternative", stops_done, b)
            self.active = "A"
            self.target_ref = a.stop_laps[0] if a.stop_laps else 0
            self.events.append(PlanEvent(lap, "set", "", "A", "start", 0.0, _seq_text(a)))

        evals: dict[str, PlanEval | None] = {}
        for pid in self.seqs:
            rem = self._remaining(pid, stops_done)
            evals[pid] = evaluate(inp, rem, th) if feasible(inp, rem, th) else None
        c_now = evals.get("C") if "C" in self.seqs else reactive(inp, th)
        best = ranked[0]
        switch_s = _f(th, "plan_switch_s", 5.0)
        off_s = _f(th, "plan_off_s", 3.0)
        hyst = _f(th, "plan_off_hysteresis_s", 1.0)

        if self.active == "C" and not neutralised and self.seqs["C"][0] == stops_done:
            evals["C"] = None  # neutralisation over and the cheap stop wasn't taken
        cur = evals.get(self.active)
        if cheap_stop and neutralised and self.active != "C" and c_now is not None:
            self._set("C", "reactive", stops_done, c_now)
            self._switch("C", "sc", lap, 0.0, c_now)
            evals["C"] = c_now
        elif cur is None:
            valid = [(e.race_time_s, pid) for pid, e in evals.items() if e is not None]
            if valid:
                to = min(valid)[1]
            else:
                to = "B" if self.active != "B" else "A"
                self._set(to, "alternative", stops_done, best)
                evals[to] = best
            self._switch(to, "invalid", lap, 0.0, evals[to])
        elif not neutralised:
            gap = cur.race_time_s - best.race_time_s
            if gap > off_s:
                other = [
                    pid
                    for pid, e in evals.items()
                    if e is not None and pid != self.active and e.compounds == best.compounds
                ]
                to = other[0] if other else ("B" if self.active != "B" else "A")
                if gap > switch_s:
                    if not other:
                        self._set(to, "alternative", stops_done, best)
                        evals[to] = best
                    self._switch(to, "pace", lap, gap, best)
                elif not other and to != "C":
                    self._set(to, "alternative", stops_done, best)
                    evals[to] = best

        cur = evals.get(self.active)
        gap = cur.race_time_s - best.race_time_s if cur is not None else 0.0
        was_on = self.on_plan
        if self.on_plan and gap > off_s:
            self.on_plan = False
            self.off_reason = "pace"
        elif not self.on_plan and gap <= off_s - hyst:
            self.on_plan = True
            self.off_reason = ""
        if was_on != self.on_plan:
            kind = "on" if self.on_plan else "off"
            self.events.append(
                PlanEvent(lap, kind, self.active, self.active, self.off_reason, gap, _seq_text(cur))
            )

        ref = cur.race_time_s if cur is not None else best.race_time_s
        plans: list[StrategyPlan] = []
        for pid in sorted({*self.seqs, "C"}):
            e = c_now if pid == "C" else evals.get(pid)
            if e is None:
                continue
            plans.append(
                StrategyPlan(
                    id=pid,
                    kind=self.kinds.get(pid, "reactive"),
                    compounds=e.compounds,
                    stop_laps=e.stop_laps,
                    window=e.window,
                    race_time_s=round(e.race_time_s, 3),
                    delta_s=round(e.race_time_s - ref, 1),
                )
            )
        target = cur.stop_laps[0] if cur is not None and cur.stop_laps else 0
        self.state = PlanState(
            plans=tuple(plans),
            active=self.active,
            on_plan=self.on_plan,
            off_s=round(gap, 1),
            off_reason=self.off_reason,
            switch_count=self.switch_count,
            switched_from=self.switched_from,
            switch_reason=self.switch_reason,
            switch_lap=self.switch_lap,
            target_lap=target,
            window=cur.window if cur is not None else (0, 0),
            target_shift=target - self.target_ref if target and self.target_ref else 0,
            sequence=_seq_text(cur),
            stops_left=len(cur.compounds) - 1 if cur is not None else 0,
        )
        return self.state


def _seq_text(e: PlanEval | None) -> str:
    return "-".join(LETTERS.get(c, "?") for c in e.compounds) if e is not None else ""


_STOP_WORDS = {0: "no stop", 1: "one stop", 2: "two stop"}


def spoken(plan: StrategyPlan | None) -> str:
    """Radio form of a plan: "one stop, medium then hard"."""
    if plan is None:
        return ""
    head = _STOP_WORDS.get(plan.stops, f"{plan.stops} stop")
    if plan.stops == 0:
        return f"{head}, {WORDS.get(plan.compounds[0], 'current')} to the end"
    return f"{head}, " + " then ".join(WORDS.get(c, "?") for c in plan.compounds)


@dataclass(frozen=True, slots=True)
class PlanFields:
    """Rule-facing plan scalars, mirrored on ModelView and Snapshot."""

    plans: tuple[StrategyPlan, ...]
    active_plan: str
    on_plan: bool
    plan_label: str
    plan_spoken: str
    plan_stops_left: int
    plan_target_lap: int
    plan_window_start: int
    plan_window_end: int
    plan_window_text: str
    plan_window_open: bool
    plan_next_compound: str
    plan_off_s: float
    plan_switch_count: int
    plan_switched_from: str
    plan_switch_reason: str
    plan_switch_lap: int
    plan_target_shift: int
    plan_b_spoken: str
    plan_b_delta_s: float
    plan_c_spoken: str
    plan_c_delta_s: float


def view_fields(st: PlanState, lap_num: int) -> PlanFields:
    """Rule-facing scalars (ModelView / Snapshot) for a plan state."""
    active = st.active_plan
    by_id = {p.id: p for p in st.plans}
    b = by_id.get("B" if st.active != "B" else "A")
    c = by_id.get("C") if st.active != "C" else None
    start, end = st.window
    return PlanFields(
        plans=st.plans,
        active_plan=st.active,
        on_plan=st.on_plan,
        plan_label=active.label if active is not None else "",
        plan_spoken=spoken(active),
        plan_stops_left=st.stops_left,
        plan_target_lap=st.target_lap,
        plan_window_start=start,
        plan_window_end=end,
        plan_window_text=active.window_text if active is not None else "",
        plan_window_open=st.stops_left > 0 and start > 0 and start <= lap_num <= end,
        plan_next_compound=(
            WORDS.get(active.compounds[1], "") if active is not None and active.stops else ""
        ),
        plan_off_s=st.off_s,
        plan_switch_count=st.switch_count,
        plan_switched_from=st.switched_from,
        plan_switch_reason=st.switch_reason,
        plan_switch_lap=st.switch_lap,
        plan_target_shift=st.target_shift,
        plan_b_spoken=spoken(b),
        plan_b_delta_s=b.delta_s if b is not None else 0.0,
        plan_c_spoken=spoken(c),
        plan_c_delta_s=c.delta_s if c is not None else 0.0,
    )
