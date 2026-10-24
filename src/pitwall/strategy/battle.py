"""Battle state and racecraft coaching (docs/18 Battle state).

A deterministic per-tick classification of the fight around the player:

    free_air | catching | attacking | defending | under_threat | managing

plus attack / defend episodes whose outcomes (passed / failed / held / lost)
feed a per-track pass model in model_params, so "is it worth attacking here?"
is learned from our own battles rather than guessed."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from statistics import NormalDist

from pitwall.config.thresholds import threshold as _th
from pitwall.model.driver import NEUTRAL, DriverScale

FREE, CATCHING, ATTACKING, DEFENDING, THREAT, MANAGING = (
    "free_air",
    "catching",
    "attacking",
    "defending",
    "under_threat",
    "managing",
)

PASS_OT, PASS_NO_OT = "battle_pass_overtake", "battle_pass_no_overtake"
HOLD = "battle_hold"
PASS_COMPOUND = 0  # model_params compound slot for battle params (per track)


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
    ahead_age_known: bool
    behind_age_known: bool
    overtake_active: bool
    attack_gap_s: float  # mindset attack_window_s
    positions: tuple[int, ...] = ()  # car_position per car idx (0 = unknown)
    pitting: frozenset[int] = frozenset()  # car idxs in the pit lane
    regulations_2026: bool = False
    closing_sd_s: float = 0.0
    pass_prob_min: float = 0.0
    driver: DriverScale = NEUTRAL


@dataclass(frozen=True, slots=True)
class BattleRates:
    """Learned (or prior) probabilities for this track."""

    pass_overtake: float = 0.6
    pass_no_overtake: float = 0.6
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
    tyre_known_ahead: bool = False
    tyre_known_behind: bool = False
    behind_changed: bool = False
    pass_prob: float = 0.0
    attack_pass_prob: float = 0.0
    hold_prob: float = 0.0
    result: str = ""  # last episode: passed | failed | held | lost
    result_recent: bool = False
    result_rival_idx: int = -1
    catch_prob: float = 0.0
    chase_prob: float = 0.0


@dataclass(frozen=True, slots=True)
class Episode:
    kind: str  # attack | defend
    rival_idx: int
    start_lap: int
    end_lap: int
    overtake: bool
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


def _effective_attack_gap(
    inp: BattleInputs,
    th: Mapping[str, object],
    raw_pass_prob: float | None = None,
    driver: DriverScale | None = None,
) -> float:
    drv = inp.driver if driver is None else driver
    floor = (
        inp.pass_prob_min
        if inp.pass_prob_min > 0
        else drv.pass_prob_min
        if drv is not NEUTRAL
        else 0.0
    )
    if floor <= 0.0:
        return inp.attack_gap_s
    if raw_pass_prob is not None and raw_pass_prob * drv.confidence_factor < floor:
        return min(inp.attack_gap_s, _th(th, "battle_defend_gap_s", 1.0))
    if drv.confidence_factor < 1.0:
        return min(inp.attack_gap_s, _th(th, "battle_defend_gap_s", 1.0))
    return inp.attack_gap_s


def classify(
    inp: BattleInputs,
    th: Mapping[str, object],
    prev: str = FREE,
    *,
    raw_pass_prob: float | None = None,
    driver: DriverScale | None = None,
) -> str:
    hyst = _th(th, "gap_hysteresis_s", 0.3)
    defend_gap = _th(th, "battle_defend_gap_s", 1.0) + (hyst if prev == DEFENDING else 0.0)
    base_attack_gap = _effective_attack_gap(inp, th, raw_pass_prob, driver)
    attack_gap = base_attack_gap + (hyst if prev == ATTACKING else 0.0)
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
    if behind and inp.gap_behind_s <= threat_gap and c_behind > 0 and c_behind >= min_rate:
        if (inp.gap_behind_s - defend_gap) / c_behind <= inp.laps_remaining:
            return THREAT
    if ahead and inp.gap_ahead_s <= catch_gap and c_ahead > 0 and c_ahead >= min_rate:
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
class _Open:
    kind: str  # attack | defend
    rival: int
    start_lap: int
    start_pos: int
    start_t: float
    overtake: bool = False


@dataclass
class _Pending:
    episode: Episode
    t: float


@dataclass
class BattleTracker:
    """Mode with hysteresis plus independent attack (car ahead) and defend
    (car behind) episodes. A pass or a lost place is only reported once it
    has stuck for battle_result_confirm_s, and always names the car of the
    episode that produced it."""

    mode: str = FREE
    since_lap: int = 0
    prev_behind_idx: int = -1
    attack: _Open | None = None
    defend: _Open | None = None
    pending: list[_Pending] = field(default_factory=list)
    undone_t: dict[int, float] = field(default_factory=dict)  # rival -> swap-back time
    swaps: list[tuple[float, int]] = field(default_factory=list)  # (t, rival) per place change
    scrap_t: float = -math.inf
    result: str = ""
    result_t: float = -math.inf
    result_rival: int = -1
    episodes: list[Episode] = field(default_factory=list)

    def reset(self) -> None:
        self.mode, self.since_lap = FREE, 0
        self.prev_behind_idx = -1
        self.attack = self.defend = None
        self.pending.clear()
        self.undone_t.clear()
        self.swaps.clear()
        self.scrap_t = -math.inf
        self.result, self.result_t, self.result_rival = "", -math.inf, -1
        self.episodes.clear()

    def swap_count(self, now: float, th: Mapping[str, object]) -> int:
        """Place changes inside battle_swap_window_s (for the dashboard)."""
        window = _th(th, "battle_swap_window_s", 120.0)
        return sum(1 for t, _ in self.swaps if now - t <= window)

    def drain(self) -> list[Episode]:
        out, self.episodes = self.episodes, []
        return out

    @staticmethod
    def _rival_pos(inp: BattleInputs, rival: int) -> int:
        return inp.positions[rival] if 0 <= rival < len(inp.positions) else 0

    def _outcome(self, ep: _Open, inp: BattleInputs) -> str:
        rp = self._rival_pos(inp, ep.rival)
        if ep.kind == "attack":
            if rp and inp.position:
                return "passed" if rp > inp.position else "failed"
            return attack_result(
                ep.start_pos, inp.position, ep.rival, inp.ahead_idx, inp.behind_idx
            )
        if rp and inp.position:
            return "lost" if rp < inp.position else "held"
        return defend_result(ep.start_pos, inp.position, ep.rival, inp.ahead_idx)

    def _announce(
        self,
        ep: Episode,
        now: float,
        th: Mapping[str, object] | None = None,
        last_lap: bool = False,
    ) -> None:
        res = ep.result
        if res in ("passed", "lost") and th is not None:
            window = _th(th, "battle_swap_window_s", 120.0)
            self.swaps = [(t, r) for t, r in self.swaps if now - t <= window]
            self.swaps.append((now, ep.rival_idx))
            same = sum(1 for _, r in self.swaps if r == ep.rival_idx)
            rivals = {r for _, r in self.swaps}
            if same >= _th(th, "battle_swap_count", 3):
                res = "swap_ahead" if res == "passed" else "swap_behind"
            elif (
                len(rivals) >= 2
                and len(self.swaps) >= _th(th, "battle_scrap_count", 4)
                and now - self.scrap_t > window
            ):
                res, self.scrap_t = "scrap", now
            if last_lap:
                res = ep.result
        self.result, self.result_t, self.result_rival = res, now, ep.rival_idx

    def _close(self, ep: _Open, inp: BattleInputs, th: Mapping[str, object]) -> None:
        if ep.rival in inp.pitting:
            return  # he boxed: neither a pass nor a hold
        res = self._outcome(ep, inp)
        done = Episode(ep.kind, ep.rival, ep.start_lap, inp.lap_num, ep.overtake, res)
        if res in ("passed", "lost"):
            wait = _th(th, "battle_result_confirm_s", 0.0)
            if inp.now - self.undone_t.get(ep.rival, -math.inf) > wait:
                self.pending.append(_Pending(done, inp.now))
            return
        if inp.now - ep.start_t < _th(th, "battle_min_episode_s", 5.0):
            return
        self.episodes.append(done)
        # "Held" only when he dropped out of range still directly behind us,
        # not when a place change elsewhere swapped the car behind.
        if res == "held" and inp.behind_idx == ep.rival:
            self._announce(done, inp.now)

    def _undone(self, ep: Episode, inp: BattleInputs) -> bool:
        rp = self._rival_pos(inp, ep.rival_idx)
        if not (rp and inp.position):
            return False
        return (ep.result == "passed" and rp < inp.position) or (
            ep.result == "lost" and rp > inp.position
        )

    def _confirm(self, inp: BattleInputs, th: Mapping[str, object]) -> None:
        wait = _th(th, "battle_result_confirm_s", 0.0)
        # swapped straight back: that rival's pending results are noise
        undone = {p.episode.rival_idx for p in self.pending if self._undone(p.episode, inp)}
        for r in undone:
            self.undone_t[r] = inp.now
        keep: list[_Pending] = []
        for p in self.pending:
            if p.episode.rival_idx in undone:
                continue
            if p.episode.rival_idx in inp.pitting:
                continue  # he boxed before the result held: a pit cycle, not a pass
            if inp.now - p.t >= wait:
                self.episodes.append(p.episode)
                self._announce(p.episode, inp.now, th, inp.laps_remaining <= 1)
            else:
                keep.append(p)
        self.pending = keep

    def _track(
        self,
        cur: _Open | None,
        kind: str,
        rival: int,
        inp: BattleInputs,
        th: Mapping[str, object],
    ) -> _Open | None:
        if cur is not None and cur.rival != rival:
            self._close(cur, inp, th)
            cur = None
        if cur is None and rival >= 0:
            cur = _Open(kind, rival, inp.lap_num, inp.position, inp.now)
        return cur

    def update(
        self,
        inp: BattleInputs,
        th: Mapping[str, object],
        rates: BattleRates,
        driver: DriverScale | None = None,
    ) -> Battle:
        # Filtered index: pit-lane rivals read as -1.
        behind_changed = inp.behind_idx >= 0 and inp.behind_idx != self.prev_behind_idx
        self.prev_behind_idx = inp.behind_idx
        drv = inp.driver if driver is None else driver
        # The gap fallback covers pre-2026 data only. F1 26 sends overtake_active.
        overtake_now = inp.overtake_active or (
            not inp.regulations_2026
            and _finite(inp.gap_ahead_s)
            and inp.gap_ahead_s <= _th(th, "drs_detection_gap_s", 1.0)
        )
        raw_pass_prob = rates.pass_overtake if overtake_now else rates.pass_no_overtake
        mode = classify(inp, th, self.mode, raw_pass_prob=raw_pass_prob, driver=drv)
        hyst = _th(th, "gap_hysteresis_s", 0.3)
        eff_attack_gap = _effective_attack_gap(inp, th, raw_pass_prob, drv)
        a_gap = eff_attack_gap + (hyst if self.attack is not None else 0.0)
        d_gap = _th(th, "battle_defend_gap_s", 1.0) + (hyst if self.defend is not None else 0.0)
        a_rival = (
            inp.ahead_idx
            if inp.ahead_idx >= 0 and _finite(inp.gap_ahead_s) and inp.gap_ahead_s <= a_gap
            else -1
        )
        d_rival = (
            inp.behind_idx
            if inp.behind_idx >= 0 and _finite(inp.gap_behind_s) and inp.gap_behind_s <= d_gap
            else -1
        )
        self._confirm(inp, th)
        self.attack = self._track(self.attack, "attack", a_rival, inp, th)
        self.defend = self._track(self.defend, "defend", d_rival, inp, th)
        if self.attack is not None and inp.overtake_active:
            self.attack.overtake = True
        self._confirm(inp, th)
        if mode != self.mode:
            self.mode, self.since_lap = mode, inp.lap_num

        c_ahead, c_behind = closings(inp)
        catch = math.inf
        if inp.ahead_idx >= 0 and _finite(inp.gap_ahead_s) and c_ahead > 0:
            catch = max(0.0, (inp.gap_ahead_s - eff_attack_gap) / c_ahead)
        threat = math.inf
        if inp.behind_idx >= 0 and _finite(inp.gap_behind_s) and c_behind > 0:
            threat = max(0.0, (inp.gap_behind_s - _th(th, "battle_defend_gap_s", 1.0)) / c_behind)
        recent = inp.now - self.result_t <= _th(th, "battle_result_hold_s", 20.0)
        need = inp.gap_ahead_s - eff_attack_gap
        if inp.ahead_idx < 0 or not math.isfinite(inp.gap_ahead_s) or inp.laps_remaining <= 0:
            catch_prob = 0.0
        elif need <= 0:
            catch_prob = 1.0
        elif inp.closing_sd_s > 0:
            catch_prob = NormalDist().cdf(
                (c_ahead * inp.laps_remaining - need)
                / (inp.closing_sd_s * math.sqrt(inp.laps_remaining))
            )
        else:
            catch_prob = 1.0 if c_ahead > 0 and need / c_ahead <= inp.laps_remaining else 0.0

        floor = (
            inp.pass_prob_min
            if inp.pass_prob_min > 0
            else drv.pass_prob_min
            if drv is not NEUTRAL
            else 0.0
        )
        base_floor = _th(th, "battle_attack_prob_min", 0.3)
        scaled_pass = raw_pass_prob * drv.confidence_factor
        if floor > 0.0:
            if scaled_pass < floor:
                eff_pass = min(scaled_pass, max(0.0, base_floor - 0.01))
            elif floor < base_floor:
                eff_pass = max(scaled_pass, base_floor + (scaled_pass - floor))
            else:
                eff_pass = scaled_pass
        else:
            eff_pass = scaled_pass
        chase_pass = rates.pass_overtake * drv.confidence_factor
        chase_prob = 0.0 if (floor > 0.0 and chase_pass < floor) else catch_prob * chase_pass
        return Battle(
            mode=mode,
            mode_laps=max(0, inp.lap_num - self.since_lap),
            catch_laps=round(catch, 1),
            threat_laps=round(threat, 1),
            closing_ahead_s=round(c_ahead, 2),
            closing_behind_s=round(c_behind, 2),
            tyre_offset_ahead=(
                inp.ahead_age - inp.own_age if inp.ahead_idx >= 0 and inp.ahead_age_known else 0
            ),
            tyre_offset_behind=(
                inp.behind_age - inp.own_age if inp.behind_idx >= 0 and inp.behind_age_known else 0
            ),
            tyre_known_ahead=inp.ahead_idx >= 0 and inp.ahead_age_known,
            tyre_known_behind=inp.behind_idx >= 0 and inp.behind_age_known,
            behind_changed=behind_changed,
            pass_prob=round(scaled_pass, 2),
            attack_pass_prob=round(eff_pass, 2),
            hold_prob=round(rates.hold, 2),
            result=self.result if recent else "",
            result_recent=recent and bool(self.result),
            result_rival_idx=self.result_rival if recent else -1,
            catch_prob=round(catch_prob, 2),
            chase_prob=round(chase_prob, 2),
        )


def shrink(rate: float | None, n: float, prior: float, weight: float) -> float:
    """Beta-style shrinkage of an observed success rate toward the prior."""
    if rate is None or n <= 0:
        return prior
    return (rate * n + prior * weight) / (n + weight)
