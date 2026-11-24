"""Safety-car verdict (docs/27): which dry compound reaches the flag with
fighting pace under SC/VSC, or the two-stop that beats waiting, or stay out.
The engine boxes or stays out on this verdict and logs the compound ladder as
a suppressed decision record. Pure and deterministic like the other strategy
modules: every input is a plain value."""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass
from statistics import NormalDist

from pitwall.config.thresholds import threshold as _f
from pitwall.strategy.plans import DRY, WORDS, PlanInputs, derive, evaluate, reactive

_NORMAL = NormalDist()

# Ladder order: hardest compound first; the fitted set's row comes last.
_COMPOUNDS = (18, 17, 16)


@dataclass(frozen=True, slots=True)
class ScRow:
    """One compound-ladder row. `current` marks the fitted set staying out;
    the other rows are fresh-set stop candidates."""

    compound: int  # visual compound
    current: bool
    sets: int  # fresh (wear == 0) sets of this compound, -1 = unknown
    pace_life_laps: float  # laps of pace life left
    usable_life_laps: int  # game-reported set life, 0 = no Tyre Sets packet
    laps_needed: float
    margin_laps: float  # shorter of pace/wear life minus laps needed
    survival: float
    survives: bool
    race_time_s: float | None


@dataclass(frozen=True, slots=True)
class ScVerdict:
    pick: int  # visual compound to box for, 0 = stay out
    reason: str
    laps_needed: float
    saving_s: float
    survive_min: float
    rows: tuple[ScRow, ...]
    sequence: tuple[int, ...]  # visual compounds from the current set, () = stay out
    to_flag: bool  # the pick survives to the flag
    hold: bool = False  # rain is due: no stop now, the weather rules own the call


def _two_compound_ok(inp: PlanInputs, compound: int, th: Mapping[str, object]) -> bool:
    """feasible()'s two-dry-compound rule for a one-stop sequence ending on
    `compound`."""
    if not _f(th, "plan_two_compound_rule", 1):
        return True
    raced = inp.used | {inp.current, compound}
    if raced - set(DRY):
        return True
    return len(raced & set(DRY)) >= 2


def sc_verdict(
    inp: PlanInputs,
    *,
    fresh_sets: Mapping[int, int],
    usable_life: Mapping[int, int],
    cur_wear_pct: float,
    cur_wear_per_lap_pct: float,
    stacking: bool,
    th: Mapping[str, object],
    rain_due: bool = False,
    penalty_s: float = 0.0,
) -> ScVerdict:
    """The verdict and ladder for one SC/VSC lap. `fresh_sets` maps a visual
    compound to its wear == 0 sets (-1 when the count is unknown); `usable_life`
    maps it to the game's reported laps (absent = no wear gate). The current
    lap counts at `sc_verdict_current_lap_weight` inside `laps_needed`.
    `penalty_s` is served in the pit lane, so it adds to every stop's loss."""
    if penalty_s > 0:
        inp = dataclasses.replace(inp, sc_loss_s=inp.sc_loss_s + penalty_s)
    cliff_ms = _f(th, "tyre_cliff_ms", 1500)
    survive_min = _f(th, "sc_survive_min", 0.60)
    laps_needed = max(0.0, inp.laps_remaining - 1 + _f(th, "sc_verdict_current_lap_weight", 0.5))
    stack_s = _f(th, "double_stack_loss_s", 4.0) if stacking else 0.0
    saving_s = inp.green_loss_s - inp.sc_loss_s - inp.wing_change_s - stack_s
    wear_cliff = _f(th, "wear_cliff_pct", 70.0)
    models = {c.visual: c for c in inp.compounds}

    def pace_survival(deg: float, sd: float, total_laps: float) -> float:
        """P(the slope leaves cliff_ms of pace life for a stint of total_laps).
        With no posterior (sd == 0) it is the step 1/0 of the mean."""
        if total_laps <= 0:
            return 1.0
        limit = cliff_ms / total_laps
        if sd <= 0:
            return 1.0 if deg <= limit else 0.0
        return _NORMAL.cdf((limit - deg) / sd)

    def row(
        compound: int,
        current: bool,
        deg: float,
        sd: float,
        total_laps: float,
        pace_life: float,
        wear_life: float,
        sets_n: int,
        race_time_s: float | None,
    ) -> ScRow:
        survival = pace_survival(deg, sd, total_laps)
        if wear_life < laps_needed:
            survival = 0.0
        return ScRow(
            compound=compound,
            current=current,
            sets=sets_n,
            pace_life_laps=round(pace_life, 2),
            usable_life_laps=usable_life.get(compound, 0),
            laps_needed=round(laps_needed, 2),
            margin_laps=round(min(pace_life, wear_life) - laps_needed, 2),
            survival=round(survival, 3),
            survives=survival >= survive_min,
            race_time_s=None if race_time_s is None else round(race_time_s, 2),
        )

    rows: list[ScRow] = []
    for v in _COMPOUNDS:
        m = models.get(v)
        if m is None:
            continue
        life = usable_life.get(v, 0)
        e = evaluate(inp, (inp.current, v), th, stop_now=True, first_loss_s=inp.sc_loss_s)
        rows.append(
            row(
                compound=v,
                current=False,
                deg=m.deg_ms_per_lap,
                sd=m.deg_posterior_sd_ms_per_lap,
                total_laps=laps_needed,
                pace_life=math.inf if m.deg_ms_per_lap <= 0 else cliff_ms / m.deg_ms_per_lap,
                wear_life=float(life) if life > 0 else math.inf,
                sets_n=fresh_sets.get(v, -1),
                race_time_s=e.race_time_s if e is not None else None,
            )
        )
    if inp.current:
        cur = inp.cur
        e = evaluate(inp, (inp.current,), th)
        rows.append(
            row(
                compound=inp.current,
                current=True,
                deg=cur.deg_ms_per_lap,
                sd=cur.deg_posterior_sd_ms_per_lap,
                total_laps=inp.tyre_age + laps_needed,
                pace_life=(
                    math.inf
                    if cur.deg_ms_per_lap <= 0
                    else max(0.0, cliff_ms / cur.deg_ms_per_lap - inp.tyre_age)
                ),
                wear_life=(
                    math.inf
                    if cur_wear_per_lap_pct <= 0
                    else max(0.0, (wear_cliff - cur_wear_pct) / cur_wear_per_lap_pct)
                ),
                sets_n=fresh_sets.get(inp.current, -1),
                race_time_s=e.race_time_s if e is not None else None,
            )
        )

    can_stop = saving_s > 0 and not rain_due
    pick = 0
    pick_t = math.inf
    eligible: list[ScRow] = []
    for r in rows:
        if r.current or not r.survives or not can_stop:
            continue
        if fresh_sets.get(r.compound, -1) == 0:
            continue
        if not _two_compound_ok(inp, r.compound, th):
            continue
        if r.race_time_s is None:
            continue
        eligible.append(r)
        if r.race_time_s < pick_t:
            pick, pick_t = r.compound, r.race_time_s
    cur_row = next((r for r in rows if r.current), None)
    # A second compound owed makes staying out illegal, so the pick stands.
    faster_out = (
        pick != 0
        and cur_row is not None
        and cur_row.survives
        and cur_row.race_time_s is not None
        and cur_row.race_time_s <= pick_t
        and _two_compound_ok(inp, inp.current, th)
    )
    if faster_out:
        pick = 0
    legal_stay = cur_row is not None and cur_row.survives and _two_compound_ok(inp, inp.current, th)
    two_stop = None
    if can_stop and not legal_stay and not eligible:
        best_one = min((e.race_time_s for e in derive(inp, th)), default=math.inf)
        no_fresh = frozenset(c for c in DRY if fresh_sets.get(c, -1) == 0)
        two_stop = reactive(inp, th, min_stops=2, no_fresh_first=no_fresh)
        if two_stop is not None and two_stop.race_time_s >= best_one:
            two_stop = None
    sequence: tuple[int, ...] = ()
    to_flag = False
    if rain_due:
        reason = "hold, rain is due"
    elif pick:
        reason = f"{WORDS.get(pick, '?')} to the flag"
        sequence, to_flag = (inp.current, pick), True
    elif two_stop is not None:
        pick, sequence = two_stop.compounds[1], two_stop.compounds
        reason = f"{WORDS.get(pick, '?')}, two stops, nothing reaches the flag"
    elif faster_out:
        reason = "stay out, the current set is faster"
    elif not can_stop:
        reason = "stay out, the stop saves nothing"
    elif any(r.survives and not r.current for r in rows):
        reason = "stay out, survivors have no legal stop"
    else:
        reason = "stay out, nothing reaches the flag"
    return ScVerdict(
        pick=pick,
        reason=reason,
        laps_needed=round(laps_needed, 2),
        saving_s=round(saving_s, 2),
        survive_min=survive_min,
        rows=tuple(rows),
        sequence=sequence,
        to_flag=to_flag,
        hold=rain_due,
    )
