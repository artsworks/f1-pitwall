"""The one pit decision of the lap (docs/27 generalisation): every pit call —
the safety-car verdict's cheap stop or stay-out, and the green-flag plans
`box_now`, `box_in_n`, `free_stop`, `undercut`, `cover`, `defend` — folds into
a single `PitDecision` the engine writes on the snapshot once per evaluation.
Rules freeze `decision_key` in `unchanged`, so a queued call dies when the
decision changes and the driver gets one instruction per decision.

`action` is the coarse instruction: 'box' (stop this lap), 'later' (a stop is
planned for `box_lap`), 'stay' (no stop), 'hold' (the weather rules own the
call) or '' (no decision). `compound` is the visual compound to fit, 0 = none.
Pure and deterministic like the other strategy modules."""

from __future__ import annotations

from dataclasses import dataclass

# Pit plans that mean "box this lap".
BOX_NOW_PLANS = frozenset({"cheap_stop", "box_now", "free_stop", "undercut", "cover"})
# Pit plans that mean "stop on a later lap".
LATER_PLANS = frozenset({"box_in_n", "overcut", "defend"})


@dataclass(frozen=True, slots=True)
class PitDecision:
    action: str  # 'box' | 'later' | 'stay' | 'hold' | ''
    plan: str  # the pit plan or verdict detail behind the action
    compound: int  # visual compound to fit, 0 = no stop compound
    box_lap: int  # lap the stop is for, 0 = no stop
    through_lap: int  # last lap this instruction stands
    reason: str = ""
    gain_s: float = 0.0
    confidence: float = 0.0
    risk: float = 0.0


def decision_key(d: PitDecision) -> str:
    """Stable identity of the instruction for `unchanged` freezing. A reprice
    keeps the key; a new action, compound or stop lap changes it, which drops
    queued calls so exactly one instruction stands per decision."""
    if d.action == "box":
        return f"box:{d.compound}"
    if d.action == "later":
        return f"later:{d.compound}:{d.box_lap}"
    return d.action


def decide(
    *,
    lap_num: int,
    pit_plan: str,
    plan_lap: int,
    sc_hold: bool,
    compound: int,
    reason: str,
    gain_s: float,
    confidence: float,
    risk: float,
) -> PitDecision:
    """Fold the pit plan (and the verdict's rain hold) into the lap's decision.
    `compound` is the visual compound the stop fits; the caller resolves it
    from the verdict pick or the active plan's next compound."""
    if sc_hold:
        return PitDecision(
            "hold", pit_plan or "stay", 0, 0, lap_num, reason, gain_s, confidence, risk
        )
    if pit_plan in BOX_NOW_PLANS:
        return PitDecision(
            "box", pit_plan, compound, lap_num, lap_num, reason, gain_s, confidence, risk
        )
    if pit_plan in LATER_PLANS:
        return PitDecision(
            "later", pit_plan, compound, plan_lap, plan_lap, reason, gain_s, confidence, risk
        )
    if pit_plan in ("stay", "no_stop"):
        return PitDecision("stay", pit_plan, 0, 0, lap_num, reason, gain_s, confidence, risk)
    return PitDecision("", pit_plan, 0, 0, 0, reason, gain_s, confidence, risk)
