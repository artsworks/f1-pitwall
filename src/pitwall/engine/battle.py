from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable

from pitwall.engine.context import EngineContext
from pitwall.engine.model import SessionModel
from pitwall.model.driver import (
    DriverScale,
)
from pitwall.state.session import Snapshot
from pitwall.strategy.battle import (
    HOLD,
    PASS_COMPOUND,
    PASS_NO_OT,
    PASS_OT,
    Battle,
    BattleInputs,
    BattleTracker,
    Episode,
)
from pitwall.strategy.pace import pace_words


def _rival_pace(snap: Snapshot, idx: int, closing_s: float) -> str:
    """Pace words for a rival (+ = he is faster): last lap against ours when
    both are representative, else the measured closing rate."""
    if not 0 <= idx < len(snap.cars):
        return ""
    his = snap.cars[idx].last_lap_time_ms
    if his and snap.player_last_lap_ms:
        delta_s = (snap.player_last_lap_ms - his) / 1000.0
        if abs(delta_s) <= 1.5:  # beyond that one of the laps was a pit or incident lap
            return pace_words(delta_s)
    if abs(closing_s) > 1.5:
        return ""  # a gap trend this steep spans a pass or a pit stop, not pace
    return pace_words(closing_s)


class BattleRunner:
    def __init__(
        self, ctx: EngineContext, model: SessionModel, driver_scale: Callable[[], DriverScale]
    ) -> None:
        self.ctx = ctx
        self.model = model
        self._driver_scale = driver_scale
        self.battle_tracker = BattleTracker()

    def reset(self) -> None:
        self.battle_tracker.reset()

    def _battle(self, snap: Snapshot) -> Snapshot:
        """Battle mode + attack/defend episodes; episode outcomes fold into
        the track's pass model and are written to the decision log."""
        if snap.session_kind != "race" or snap.race_phase != "racing":
            return snap
        attack = self.ctx.mode().get("attack_window_s", 1.0)
        drv = self._driver_scale()
        own = snap.predicted_lap_ms or int(snap.base_pace_ms)
        inp = BattleInputs(
            now=snap.now,
            lap_num=snap.lap_num,
            position=snap.position,
            laps_remaining=snap.laps_remaining,
            ahead_idx=snap.rival_ahead_idx,
            behind_idx=-1 if snap.rival_behind_in_pit_lane else snap.rival_behind_idx,
            gap_ahead_s=snap.gap_ahead_s,
            gap_behind_s=math.inf if snap.rival_behind_in_pit_lane else snap.gap_behind_s,
            trend_ahead_s=snap.gap_trend_ahead_s,
            trend_behind_s=snap.gap_trend_behind_s,
            own_pace_ms=own,
            ahead_pace_ms=snap.rival_ahead_pace_ms,
            behind_pace_ms=snap.rival_behind_pace_ms,
            own_age=snap.tyre_age_laps,
            ahead_age=snap.rival_ahead_age,
            behind_age=snap.rival_behind_age,
            ahead_age_known=snap.rival_ahead_age_known,
            behind_age_known=snap.rival_behind_age_known,
            overtake_active=bool(snap.overtake_active)
            if snap.regulations_2026
            else snap.drs_available,
            attack_gap_s=float(attack) if isinstance(attack, int | float) else 1.0,
            positions=tuple(c.car_position for c in snap.cars),
            pitting=frozenset(i for i, c in enumerate(snap.cars) if c.pit_status != 0),
            regulations_2026=snap.regulations_2026,
            closing_sd_s=snap.gap_closing_sd_ahead_s,
            pass_prob_min=drv.pass_prob_min,
            driver=drv,
        )
        tracker = self.battle_tracker
        th = self.ctx.store.current().thresholds
        b: Battle = tracker.update(inp, th, self.model.battle_rates(snap.track_id))
        for ep in tracker.drain():
            self._persist_episode(snap, ep)
        name = ""
        if 0 <= b.result_rival_idx < len(snap.participants):
            name = snap.participants[b.result_rival_idx].name
        return dataclasses.replace(
            snap,
            battle_mode=b.mode,
            battle_mode_laps=b.mode_laps,
            battle_catch_laps=b.catch_laps,
            battle_threat_laps=b.threat_laps,
            battle_closing_ahead_s=b.closing_ahead_s,
            battle_closing_behind_s=b.closing_behind_s,
            battle_tyre_offset_ahead=b.tyre_offset_ahead,
            battle_tyre_offset_behind=b.tyre_offset_behind,
            battle_tyre_known_ahead=b.tyre_known_ahead,
            battle_tyre_known_behind=b.tyre_known_behind,
            battle_behind_changed=b.behind_changed,
            battle_pass_prob=b.pass_prob,
            battle_attack_pass_prob=b.attack_pass_prob,
            battle_catch_prob=b.catch_prob,
            battle_chase_prob=b.chase_prob,
            battle_hold_prob=b.hold_prob,
            battle_result=b.result,
            battle_result_recent=b.result_recent,
            battle_result_name=name,
            battle_swaps=tracker.swap_count(inp.now, th),
            battle_pace_ahead=_rival_pace(snap, snap.rival_ahead_idx, -b.closing_ahead_s),
            battle_pace_behind=_rival_pace(snap, snap.rival_behind_idx, b.closing_behind_s),
        )

    def _persist_episode(self, snap: Snapshot, ep: Episode) -> None:
        self.ctx.dispatcher.log.write(
            {
                "t": snap.now,
                "session_time": snap.session_time,
                "lap": ep.end_lap,
                "rule_id": None,
                "outcome": "battle",
                "kind": ep.kind,
                "rival_idx": ep.rival_idx,
                "start_lap": ep.start_lap,
                "overtake": ep.overtake,
                "result": ep.result,
            }
        )
        if self.ctx.db is None or snap.track_id < 0 or self.ctx.synthetic_session():
            return
        name = HOLD if ep.kind == "defend" else PASS_OT if ep.overtake else PASS_NO_OT
        cap = self.ctx.th("param_weight_cap", 50.0)
        self.ctx.db.fold_param(
            snap.track_id,
            PASS_COMPOUND,
            name,
            1.0 if ep.success else 0.0,
            1.0,
            param_weight_cap=cap,
        )
        self.model._battle_rates = None
