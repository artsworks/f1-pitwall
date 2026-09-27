"""Hindsight grader (docs/20 L1): label every fired call and plan event against
what actually happened, from SQLite alone. Deterministic and recomputed per
session, so re-running `pitwall digest` is idempotent."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from pitwall.store.db import Database, LapRow
from pitwall.strategy.plans import LETTERS

STOP_RULES = ("box_now", "plan_target_lap", "plan_sc_box")
TACTICAL_PLANS = ("cheap_stop", "free_stop", "undercut")
FUEL_RULES = ("fuel_short", "fuel_marginal", "fuel_spare")
TYRE_RULES = ("tyre_life",)

GOOD, WRONG, IGNORED, CENSORED, NA = "good", "wrong", "ignored", "censored", "n/a"


@dataclass(frozen=True, slots=True)
class Outcome:
    call_id: str
    rule_id: str
    lap: int
    metric: str  # stop_cost_s | stop_taken | lap_ms | laps_of_pace | fuel_margin | plan_followed
    predicted: float | None
    actual: float | None
    error: float | None
    label: str  # good | wrong | ignored | censored | n/a
    detail: str = ""

    def row(self) -> dict[str, object]:
        return asdict(self)


def _th(th: Mapping[str, object], name: str, default: float) -> float:
    v = th.get(name, default)
    return float(v) if isinstance(v, int | float) else default


def _num(inputs: Mapping[str, object], name: str) -> float | None:
    v = inputs.get(name)
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def stop_laps(laps: Sequence[LapRow]) -> list[int]:
    """In-laps of the player's stops: flagged 'pitted', else a tyre-age reset."""
    out = {lap.lap_num for lap in laps if "pitted" in lap.invalid_reasons}
    for prev, cur in zip(laps, laps[1:], strict=False):
        if cur.lap_num == prev.lap_num + 1 and cur.tyre_age_laps < prev.tyre_age_laps:
            if not any(s in out for s in (prev.lap_num, cur.lap_num)):
                out.add(prev.lap_num)
    return sorted(out)


def stints(laps: Sequence[LapRow], stops: Sequence[int]) -> list[list[LapRow]]:
    """Split the player's laps at each in-lap (the in-lap closes a stint)."""
    out: list[list[LapRow]] = [[]]
    for lap in laps:
        out[-1].append(lap)
        if lap.lap_num in stops:
            out.append([])
    return [s for s in out if s]


def stint_compound(stint: Sequence[LapRow]) -> str:
    """The stint's compound letter from its first lap; the in-lap can already
    carry the new set once tyres are changed before the timing line."""
    return LETTERS.get(stint[0].compound, "?")


def _green(stint: Sequence[LapRow]) -> list[LapRow]:
    return [lap for lap in stint if lap.valid == 1 and lap.sc_status == 0 and lap.lap_time_ms > 0]


def linear_deg(stint: Sequence[LapRow]) -> tuple[float, float, int] | None:
    """(base_ms at age 0, deg ms/lap, n) from the stint's green valid laps."""
    pts = [(float(lap.tyre_age_laps), float(lap.lap_time_ms)) for lap in _green(stint)]
    n = len(pts)
    if n < 2:
        return None
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    if sxx <= 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pts) / sxx
    return my - slope * mx, max(0.0, slope), n


def stop_cost_s(
    before: Sequence[LapRow], after: Sequence[LapRow], min_stint: int, extrapolate: int = 3
) -> tuple[float, int] | None:
    """Hindsight cost of the actual stop lap vs the best one, holding both
    stints' fitted pace and the total laps fixed (pit loss cancels out).
    Neither stint is stretched more than `extrapolate` laps past what was
    actually driven on it, so an unseen cliff can't make a later stop look
    better. Returns (cost seconds, best in-lap)."""
    f1 = linear_deg(before)
    f2 = linear_deg(after)
    if f1 is None or f2 is None:
        return None
    first = before[0].lap_num
    age0 = before[0].tyre_age_laps
    total = len(before) + len(after)
    actual_k = len(before)

    def t(k: int) -> float:
        s1 = sum(f1[0] + f1[1] * (age0 + i) for i in range(k))
        s2 = sum(f2[0] + f2[1] * j for j in range(total - k))
        return s1 + s2

    lo = max(max(1, min_stint), total - len(after) - extrapolate)
    hi = min(total - max(1, min_stint), len(before) + extrapolate)
    ks = range(lo, hi + 1)
    if actual_k not in ks:
        return None
    best = min(ks, key=lambda k: (t(k), k))
    return (t(actual_k) - t(best)) / 1000.0, first + best - 1


def _cliff_laps(
    stint: Sequence[LapRow], call_lap: int, end_lap: int, cliff_ms: float
) -> tuple[float, int | None] | None:
    """Laps after the call until pace is cliff_ms slower than the stint's
    fitted age-0 pace (the model's own laps_of_pace reference), from the
    green laps up to the call. None if there's no reference; (base, None)
    if the cliff never came before end_lap."""
    fit = linear_deg([lap for lap in stint if lap.lap_num <= call_lap])
    if fit is None:
        return None
    base = fit[0]
    for lap in _green(stint):
        if call_lap < lap.lap_num <= end_lap and lap.lap_time_ms - base >= cliff_ms:
            return base, lap.lap_num - call_lap
    return base, None


def grade_session(db: Database, uid: int, th: Mapping[str, object]) -> list[Outcome]:
    laps = db.laps_for(uid, 0)
    if not laps:
        return []
    last_lap = laps[-1].lap_num
    total_laps = int((db.session_row(uid) or {}).get("total_laps") or 0)
    finished = total_laps > 0 and last_lap >= total_laps
    stops = stop_laps(laps)
    parts = stints(laps, stops)
    by_lap = {lap.lap_num: lap for lap in laps}
    stop_window = int(_th(th, "hind_stop_window_laps", 1))
    stop_tol = _th(th, "hind_stop_tol_s", 1.5)
    lap_tol = _th(th, "hind_lap_tol_ms", 500)
    laps_tol = _th(th, "hind_laps_tol", 2)
    fuel_tol = _th(th, "hind_fuel_tol_laps", 0.5)
    cliff_ms = _th(th, "tyre_cliff_ms", 1500)
    min_stint = int(_th(th, "plan_min_stint_laps", 3))
    extrapolate = int(_th(th, "hind_extrapolate_laps", 3))
    out: list[Outcome] = []
    fired = [c for c in db.calls_for_session(uid) if c.get("outcome") == "fired"]

    for n, call in enumerate(fired):
        rule = str(call.get("rule_id") or "")
        cid = str(call.get("call_id") or "")
        lap_n = int(call.get("lap") or 0)
        raw = call.get("inputs")
        inputs: dict[str, object] = json.loads(raw) if isinstance(raw, str) else {}

        if rule in STOP_RULES:
            taken = next((s for s in stops if lap_n <= s <= lap_n + stop_window), None)
            refire = next(
                (int(c.get("lap") or 0) for c in fired[n + 1 :] if c.get("rule_id") == rule),
                None,
            )
            if (
                refire is not None
                and refire <= lap_n + stop_window + 1
                and not any(lap_n <= s < refire for s in stops)
            ):
                # One intended stop, called again: grade only the last call.
                out.append(
                    Outcome(
                        cid, rule, lap_n, "stop_taken", None, None, None, NA, f"refired L{refire}"
                    )
                )
            elif taken is None:
                out.append(Outcome(cid, rule, lap_n, "stop_taken", 1.0, 0.0, None, IGNORED))
            else:
                i = stops.index(taken)
                in_lap = by_lap.get(taken)
                out_lap = by_lap.get(taken + 1)
                neutralised = any(x is not None and x.sc_status != 0 for x in (in_lap, out_lap))
                tactical = rule == "plan_sc_box" or inputs.get("pit_plan") in TACTICAL_PLANS
                if neutralised or tactical:
                    # Pit loss / track position was the point; deg alone can't judge it.
                    why = "neutralised stop" if neutralised else f"{inputs.get('pit_plan') or rule}"
                    out.append(
                        Outcome(cid, rule, lap_n, "stop_cost_s", float(taken), None, None, NA, why)
                    )
                    continue
                res = (
                    stop_cost_s(parts[i], parts[i + 1], min_stint, extrapolate)
                    if i + 1 < len(parts)
                    else None
                )
                if res is None:
                    out.append(Outcome(cid, rule, lap_n, "stop_cost_s", None, None, None, NA))
                else:
                    cost, best = res
                    label = GOOD if cost <= stop_tol else WRONG
                    out.append(
                        Outcome(
                            cid,
                            rule,
                            lap_n,
                            "stop_cost_s",
                            float(taken),
                            float(best),
                            round(cost, 2),
                            label,
                            f"stopped L{taken}, best in hindsight L{best}",
                        )
                    )

        pred_ms = _num(inputs, "predicted_lap_ms")
        nxt = by_lap.get(lap_n)
        if pred_ms and nxt is not None and nxt.valid == 1 and nxt.sc_status == 0:
            err = pred_ms - nxt.lap_time_ms
            label = GOOD if abs(err) <= lap_tol else WRONG
            out.append(
                Outcome(cid, rule, lap_n, "lap_ms", pred_ms, float(nxt.lap_time_ms), err, label)
            )

        lop = _num(inputs, "laps_of_pace")
        if rule in TYRE_RULES and lop is not None:
            end = next((s for s in stops if s >= lap_n), last_lap)
            stint = next((p for p in parts if p[0].lap_num <= lap_n <= p[-1].lap_num), [])
            res_cliff = _cliff_laps(stint, lap_n, end, cliff_ms)
            cliff = res_cliff[1] if res_cliff is not None else None
            if res_cliff is None:
                out.append(Outcome(cid, rule, lap_n, "laps_of_pace", lop, None, None, NA))
            elif cliff is None:
                # Right-censored: the tyre lasted `ran` laps without a cliff.
                out.append(
                    Outcome(
                        cid,
                        rule,
                        lap_n,
                        "laps_of_pace",
                        lop,
                        float(end - lap_n),
                        None,
                        CENSORED,
                        "no cliff before the stop / flag",
                    )
                )
            else:
                err = lop - cliff
                label = GOOD if abs(err) <= laps_tol else WRONG
                out.append(Outcome(cid, rule, lap_n, "laps_of_pace", lop, float(cliff), err, label))

        margin = _num(inputs, "fuel_margin_laps")
        if rule in FUEL_RULES and margin is not None and not finished:
            out.append(
                Outcome(cid, rule, lap_n, "fuel_margin", margin, None, None, CENSORED, "no finish")
            )
        elif rule in FUEL_RULES and margin is not None and laps[-1].fuel_remaining_laps:
            actual = laps[-1].fuel_remaining_laps
            err = margin - actual
            label = GOOD if abs(err) <= fuel_tol else WRONG
            out.append(
                Outcome(
                    cid, rule, lap_n, "fuel_margin", margin, round(actual, 2), round(err, 2), label
                )
            )

    executed = [stint_compound(p) for p in parts]
    starts = [p[0].lap_num for p in parts]
    events = [e for e in db.plan_events_for_session(uid) if e.get("kind") in ("set", "switch")]
    for k, ev in enumerate(events):
        lap_n = int(ev.get("lap") or 0)
        if k + 1 < len(events):
            next_lap = int(events[k + 1].get("lap") or 0)
            if not any(lap_n <= s < next_lap for s in stops):
                # Replaced before any stop was made on it: nothing to grade.
                out.append(
                    Outcome(
                        f"plan:{ev.get('id')}",
                        f"plan_{ev.get('kind')}",
                        lap_n,
                        "plan_followed",
                        None,
                        None,
                        None,
                        NA,
                        f"plan {ev.get('to_plan')} superseded L{next_lap}",
                    )
                )
                continue
        seq = [c for c in str(ev.get("sequence") or "").split("-") if c]
        i = max((j for j, s in enumerate(starts) if s <= lap_n), default=0)
        run_seq = executed[i:]
        followed = bool(seq) and run_seq[: len(seq)] == seq
        out.append(
            Outcome(
                f"plan:{ev.get('id')}",
                f"plan_{ev.get('kind')}",
                lap_n,
                "plan_followed",
                None,
                1.0 if followed else 0.0,
                None,
                GOOD if followed else WRONG,
                f"plan {ev.get('to_plan')} {'-'.join(seq)}, ran {'-'.join(run_seq)}",
            )
        )
    return out


def grade_and_store(db: Database, uid: int, th: Mapping[str, object]) -> list[Outcome]:
    outcomes = grade_session(db, uid, th)
    db.replace_outcomes(uid, [o.row() for o in outcomes])
    return outcomes
