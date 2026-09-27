"""Battle state and racecraft coaching (docs/20 L3).

A deterministic per-tick classification of the fight around the player:

    free_air | catching | attacking | defending | under_threat | managing

plus attack / defend episodes whose outcomes (passed / failed / held / lost)
feed a per-track pass model in model_params, so "is it worth attacking here?"
is learned from our own battles rather than guessed."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

FREE, CATCHING, ATTACKING, DEFENDING, THREAT, MANAGING = (
    "free_air",
    "catching",
    "attacking",
    "defending",
    "under_threat",
    "managing",
)

PASS_DRS, PASS_NODRS, HOLD = "battle_pass_drs", "battle_pass_nodrs", "battle_hold"
PASS_COMPOUND = 0  # model_params compound slot for battle params (per track)


def _th(th: Mapping[str, object], name: str, default: float) -> float:
    v = th.get(name, default)
    return float(v) if isinstance(v, int | float) else default


@dataclass(frozen=True, slots=True)
class BattleInputs:
    now: float
    lap_num: int
    position: int
    laps_remaining: int
    ahead_idx: int
    behind_idx: int
    gap_ahead_s: float
    gap_behind_s: float
    trend_ahead_s: float  # + = gap to the car ahead shrinking per lap
    trend_behind_s: float  # + = car behind closing per lap
    own_pace_ms: int
    ahead_pace_ms: int
    behind_pace_ms: int
    own_age: int
    ahead_age: int
    behind_age: int
    drs_available: bool
    attack_gap_s: float  # mindset attack_window_s


@dataclass(frozen=True, slots=True)
class BattleRates:
    """Learned (or prior) probabilities for this track."""

    pass_drs: float = 0.35
    pass_nodrs: float = 0.15
    hold: float = 0.7


@dataclass(frozen=True, slots=True)
class Battle:
    mode: str = FREE
    mode_laps: int = 0
    catch_laps: float = math.inf  # laps until within attack range of the car ahead
    threat_laps: float = math.inf  # laps until the car behind is within defend range
    closing_ahead_s: float = 0.0  # + = we gain on the car ahead per lap
    closing_behind_s: float = 0.0  # + = the car behind gains on us per lap
    tyre_offset_ahead: int = 0  # + = rival ahead's tyres are older than ours
    tyre_offset_behind: int = 0  # + = rival behind's tyres are older than ours
    pass_prob: float = 0.0
    hold_prob: float = 0.0
    result: str = ""  # last episode: passed | failed | held | lost
    result_recent: bool = False
    result_rival_idx: int = -1


@dataclass(frozen=True, slots=True)
class Episode:
    kind: str  # attack | defend
    rival_idx: int
    start_lap: int
    end_lap: int
    drs: bool
    result: str

    @property
    def success(self) -> bool:
        return self.result in ("passed", "held")


def _finite(x: float) -> bool:
    return math.isfinite(x) and x > 0


def closing(trend_s: float, pace_delta_ms: int) -> float:
    """Measured line-to-line gap change when available, else the pace delta."""
    if trend_s != 0.0:
        return trend_s
    return pace_delta_ms / 1000.0


def closings(inp: BattleInputs) -> tuple[float, float]:
    """(closing on the car ahead, car behind closing on us), s/lap."""
    ahead = inp.ahead_pace_ms - inp.own_pace_ms if inp.ahead_pace_ms and inp.own_pace_ms else 0
    behind = inp.own_pace_ms - inp.behind_pace_ms if inp.behind_pace_ms and inp.own_pace_ms else 0
    return closing(inp.trend_ahead_s, ahead), closing(inp.trend_behind_s, behind)


def classify(inp: BattleInputs, th: Mapping[str, object], prev: str = FREE) -> str:
    hyst = _th(th, "gap_hysteresis_s", 0.3)
    defend_gap = _th(th, "battle_defend_gap_s", 1.0) + (hyst if prev == DEFENDING else 0.0)
    attack_gap = inp.attack_gap_s + (hyst if prev == ATTACKING else 0.0)
    threat_gap = _th(th, "battle_threat_gap_s", 3.0)
    catch_gap = _th(th, "battle_catch_gap_s", 5.0)
    min_rate = _th(th, "battle_closing_min_s", 0.2)
    ahead = inp.ahead_idx >= 0 and _finite(inp.gap_ahead_s)
    behind = inp.behind_idx >= 0 and _finite(inp.gap_behind_s)
    c_ahead, c_behind = closings(inp)
    if behind and inp.gap_behind_s <= defend_gap:
        return DEFENDING
    if ahead and inp.gap_ahead_s <= attack_gap:
        return ATTACKING
    if behind and inp.gap_behind_s <= threat_gap and c_behind >= min_rate:
        if (inp.gap_behind_s - defend_gap) / c_behind <= inp.laps_remaining:
            return THREAT
    if ahead and inp.gap_ahead_s <= catch_gap and c_ahead >= min_rate:
        if (inp.gap_ahead_s - attack_gap) / c_ahead <= inp.laps_remaining:
            return CATCHING
    if (ahead and inp.gap_ahead_s <= catch_gap) or (behind and inp.gap_behind_s <= threat_gap):
        return MANAGING
    return FREE


def attack_result(start_pos: int, pos: int, rival: int, ahead_idx: int, behind_idx: int) -> str:
    if pos < start_pos and ahead_idx != rival:
        return "passed"
    return "failed"


def defend_result(start_pos: int, pos: int, rival: int, ahead_idx: int) -> str:
    if pos > start_pos or ahead_idx == rival:
        return "lost"
    return "held"


@dataclass
class BattleTracker:
    """Mode with hysteresis plus attack / defend episode bookkeeping."""

    mode: str = FREE
    since_lap: int = 0
    kind: str = ""
    rival: int = -1
    start_lap: int = 0
    start_pos: int = 0
    start_t: float = 0.0
    drs: bool = False
    result: str = ""
    result_t: float = -math.inf
    result_rival: int = -1
    episodes: list[Episode] = field(default_factory=list)

    def reset(self) -> None:
        self.mode, self.since_lap, self.kind, self.rival = FREE, 0, "", -1
        self.result, self.result_t, self.result_rival = "", -math.inf, -1
        self.episodes.clear()

    def drain(self) -> list[Episode]:
        out, self.episodes = self.episodes, []
        return out

    def _close(self, inp: BattleInputs, th: Mapping[str, object]) -> None:
        if not self.kind:
            return
        if self.kind == "attack":
            res = attack_result(
                self.start_pos, inp.position, self.rival, inp.ahead_idx, inp.behind_idx
            )
        else:
            res = defend_result(self.start_pos, inp.position, self.rival, inp.ahead_idx)
        long_enough = inp.now - self.start_t >= _th(th, "battle_min_episode_s", 5.0)
        if long_enough or res in ("passed", "lost"):
            self.episodes.append(
                Episode(self.kind, self.rival, self.start_lap, inp.lap_num, self.drs, res)
            )
            self.result, self.result_t, self.result_rival = res, inp.now, self.rival
        self.kind, self.rival, self.drs = "", -1, False

    def update(self, inp: BattleInputs, th: Mapping[str, object], rates: BattleRates) -> Battle:
        mode = classify(inp, th, self.mode)
        target = inp.ahead_idx if mode == ATTACKING else inp.behind_idx if mode == DEFENDING else -1
        kind = "attack" if mode == ATTACKING else "defend" if mode == DEFENDING else ""
        if self.kind and (kind != self.kind or target != self.rival):
            self._close(inp, th)
        if kind and not self.kind:
            self.kind, self.rival, self.start_lap = kind, target, inp.lap_num
            self.start_pos, self.start_t = inp.position, inp.now
        if self.kind == "attack" and inp.drs_available:
            self.drs = True
        if mode != self.mode:
            self.mode, self.since_lap = mode, inp.lap_num

        c_ahead, c_behind = closings(inp)
        catch = math.inf
        if inp.ahead_idx >= 0 and _finite(inp.gap_ahead_s) and c_ahead > 0:
            catch = max(0.0, (inp.gap_ahead_s - inp.attack_gap_s) / c_ahead)
        threat = math.inf
        if inp.behind_idx >= 0 and _finite(inp.gap_behind_s) and c_behind > 0:
            threat = max(0.0, (inp.gap_behind_s - _th(th, "battle_defend_gap_s", 1.0)) / c_behind)
        drs_now = inp.drs_available or (
            _finite(inp.gap_ahead_s) and inp.gap_ahead_s <= _th(th, "drs_detection_gap_s", 1.0)
        )
        recent = inp.now - self.result_t <= _th(th, "battle_result_hold_s", 20.0)
        return Battle(
            mode=mode,
            mode_laps=max(0, inp.lap_num - self.since_lap),
            catch_laps=round(catch, 1),
            threat_laps=round(threat, 1),
            closing_ahead_s=round(c_ahead, 2),
            closing_behind_s=round(c_behind, 2),
            tyre_offset_ahead=inp.ahead_age - inp.own_age if inp.ahead_idx >= 0 else 0,
            tyre_offset_behind=inp.behind_age - inp.own_age if inp.behind_idx >= 0 else 0,
            pass_prob=round(rates.pass_drs if drs_now else rates.pass_nodrs, 2),
            hold_prob=round(rates.hold, 2),
            result=self.result if recent else "",
            result_recent=recent and bool(self.result),
            result_rival_idx=self.result_rival if recent else -1,
        )


def shrink(rate: float | None, n: float, prior: float, weight: float) -> float:
    """Beta-style shrinkage of an observed success rate toward the prior."""
    if rate is None or n <= 0:
        return prior
    return (rate * n + prior * weight) / (n + weight)
