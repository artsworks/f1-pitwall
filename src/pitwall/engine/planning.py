from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np

from pitwall.config.models import (
    Settings,
    resolve_mindset,
)
from pitwall.config.thresholds import threshold
from pitwall.engine.context import EngineContext
from pitwall.engine.model import SessionModel
from pitwall.model.deg import (
    DegFit,
    cliff_age,
    planning_fit,
    usable_laps,
)
from pitwall.model.driver import (
    DriverScale,
)
from pitwall.state.model_view import ModelView
from pitwall.state.session import Snapshot
from pitwall.store.db import lap_row_from_summary
from pitwall.strategy.montecarlo import McResult, plan_agreement, scale_plan
from pitwall.strategy.pitwindow import NO_PLAN, PitPlan, RivalView, optimise
from pitwall.strategy.plans import (
    DRY,
    WORDS,
    CompoundModel,
    PlanEvent,
    PlanFields,
    PlanInputs,
    PlanState,
    PlanTracker,
    compound_stop_owed,
    view_fields,
)
from pitwall.strategy.scverdict import ScVerdict, sc_verdict


def _true_behind_view(snap: Snapshot) -> tuple[RivalView | None, float]:
    idx = snap.true_rival_behind_idx
    if idx < 0:
        return None, math.inf
    pace_ms = snap.rival_behind_pace_ms if idx == snap.rival_behind_idx else 0
    return (
        RivalView(
            idx,
            snap.true_rival_behind_name,
            pace_ms,
            snap.true_rival_behind_pitted,
        ),
        snap.true_rival_behind_gap_s,
    )


def _with_plan[T: (ModelView, Snapshot)](obj: T, f: PlanFields) -> T:
    # Every PlanFields field must also exist on ModelView and Snapshot.
    return dataclasses.replace(
        obj,
        **{field.name: getattr(f, field.name) for field in dataclasses.fields(PlanFields)},
    )


# Pit plans that end in a stop. "stay" and "no_stop" are plans too, but the
# wing change at the stop only makes sense when a stop is coming.
_STOP_PLANS = frozenset(
    {"box_now", "box_in_n", "cheap_stop", "free_stop", "undercut", "overcut", "cover"}
)

_NO_PIT_FIELDS: dict[str, Any] = {
    "pit_plan": "",
    "pit_plan_lap": 0,
    "pit_plan_gain_s": 0.0,
    "pit_plan_confidence": 0.0,
    "pit_plan_risk": 0.0,
    "pit_plan_risky": False,
    "pit_plan_rival_idx": -1,
    "pit_plan_rival_name": "",
    "pit_plan_reason": "",
    "pit_window_start": 0,
    "pit_window_end": 0,
    "undercut_s": 0.0,
    "overcut_s": 0.0,
}


def _hold_cover_plan(
    held: tuple[PitPlan, int] | None,
    plan: PitPlan,
    snap: Snapshot,
    pit_min_laps_left: float,
) -> tuple[PitPlan, tuple[PitPlan, int] | None]:
    if plan.plan in ("cover", "defend"):
        if held is None or held[0].rival_idx != plan.rival_idx:
            held = (plan, snap.lap_num)
        return plan, held
    if held is None:
        return plan, None
    held_plan, start_lap = held
    if (
        snap.phase == "racing"
        and snap.safety_car_status == 0
        and snap.lap_num <= start_lap + 1
        and snap.laps_remaining > pit_min_laps_left
        and plan.plan != "cheap_stop"
    ):
        return held_plan, held
    return plan, None


def _usable_life(snap: Snapshot) -> dict[int, int]:
    """Game-reported set life per dry compound; empty without the packet."""
    if not snap.tyre_sets:
        return {}
    return {
        v: max((s.usable_life for s in snap.tyre_sets if s.visual == v), default=0) for v in DRY
    }


class Planner:
    def __init__(
        self,
        ctx: EngineContext,
        model: SessionModel,
        driver_scale: Callable[[Settings, Mapping[str, Any]], DriverScale],
        plan_thresholds: Callable[[Snapshot, bool], Mapping[str, object]],
        mindset: Callable[[], str],
    ) -> None:
        self.ctx = ctx
        self.model = model
        self._driver_scale = driver_scale
        self._plan_thresholds = plan_thresholds
        self._mindset = mindset
        self._held_plan: tuple[PitPlan, int] | None = None
        self._plan_mc_cache: dict[tuple[Any, ...], McResult | None] = {}
        self.plan_tracker = PlanTracker()
        self._plan_key: tuple[Any, ...] | None = None
        self._damage_pace_ms = 0.0
        self._slow_run: tuple[int, float, float | None] | None = None
        self._damage_stops = 0
        self._stops_done = 0
        self._sc_counted_stops = -1
        self._prev_num_pit_stops: int | None = None
        self._red_restart = False
        self._prev_pit_status = 0
        self._stop_expect: tuple[str, str, int] | None = None
        self._stop_check: tuple[str, str, int, float] | None = None
        self._used_compounds: set[int] = set()
        self._prev_race_phase = ""
        self._sc_verdict_laps: set[tuple[Any, int]] = set()
        self._sc_verdict: ScVerdict | None = None

    def reset(self) -> None:
        self._held_plan = None
        self.plan_tracker.reset()
        self._plan_key = None
        self._damage_pace_ms = 0.0
        self._slow_run = None
        self._damage_stops = 0
        self._plan_mc_cache.clear()
        self._stops_done = 0
        self._sc_counted_stops = -1
        self._prev_num_pit_stops = None
        self._red_restart = False
        self._prev_pit_status = 0
        self._stop_expect = None
        self._stop_check = None
        self._used_compounds.clear()
        self._prev_race_phase = ""
        self._sc_verdict_laps.clear()
        self._sc_verdict = None

    def rewind(self, t: float) -> None:
        self._held_plan = None
        self._sc_verdict = None
        self._slow_run = None
        self._stop_expect = None
        self._prev_pit_status = -1  # -1: pit state unknown until the next tick
        if self._stop_check is not None and t < self._stop_check[3]:
            self._stop_check = None

    def _plan_damage(self, snap: Snapshot, fit: DegFit) -> tuple[str, float, float]:
        if snap.num_pit_stops != self._damage_stops:
            self._damage_pace_ms = 0.0
            self._slow_run = None
            self._damage_stops = snap.num_pit_stops
        if snap.race_phase not in ("racing", "sc", "vsc"):
            return "", 0.0, 0.0
        wing_pct = max(snap.damage.front_left_wing, snap.damage.front_right_wing)
        wing = wing_pct >= self.ctx.th("plan_damage_wing_pct", 15)
        tyre = bool(snap.puncture_corner)
        last = self.ctx.state.laps[-1] if self.ctx.state.laps else None
        measured = None
        if (
            last is not None
            and usable_laps(
                [lap_row_from_summary(snap.session_uid, 0, last)],
                self.model._thermal_hi_c(snap.track_id, last.compound, self.ctx.store.current()),
            )
            and last.lap_num > 1
            and last.sc_status == 0
            and last.lap_num == snap.lap_num - 1
            and snap.tyre_age_laps > 0
        ):
            age = last.tyre_age_laps
            predicted = fit.base_ms + fit.deg_ms_per_lap * age + fit.deg2_ms_per_lap2 * age**2
            measured = max(0.0, last.lap_time_ms - predicted)
        lap = snap.lap_num - 1
        if measured is None or measured < self.ctx.th("plan_damage_pace_ms", 1500):
            self._slow_run = None
        elif self._slow_run is None or self._slow_run[0] not in (lap, lap - 1):
            self._slow_run = (lap, measured, None)
        elif self._slow_run[0] == lap - 1:
            self._slow_run = (lap, measured, min(self._slow_run[1], measured))
        run = self._slow_run
        confirmed = run[2] if run is not None else None
        positions = (
            0 < snap.lap_num <= self.ctx.th("plan_rebuild_max_lap", 2)
            and snap.grid_position > 0
            and snap.position > 0
            and snap.position - snap.grid_position >= self.ctx.th("plan_rebuild_positions_lost", 3)
        )
        cause = (
            "damage" if wing or tyre else "pace" if confirmed else "positions" if positions else ""
        )
        penalty = 0.0
        if wing or tyre:
            penalty = max(
                measured or 0.0,
                wing_pct * self.ctx.th("damage_pace_ms_per_wing_pct", 20) if wing else 0.0,
                self.ctx.th("damage_tyre_pace_ms", 1500) if tyre and measured is None else 0.0,
            )
        elif confirmed is not None:
            penalty = confirmed
        self._damage_pace_ms = max(self._damage_pace_ms, penalty)
        penalty = self._damage_pace_ms
        if not cause and penalty > 0:
            cause = "pace"
        return cause, penalty, self.ctx.th("pit_wing_change_s", 5.0) if wing else 0.0

    def _plan(self, snap: Snapshot) -> Snapshot:
        """Run the pit-window optimiser on the fresh snapshot and fold the
        plan into both the snapshot and the next ModelView."""
        fit = self.ctx.deg_fit
        if not snap.race_phase or self.ctx.fresh_prior is None:
            return snap
        if fit is None:
            if not self._sc_gate(snap) and self._plan_damage(snap, self.ctx.fresh_prior)[1] <= 0:
                return self._plans(snap, self.ctx.fresh_prior, NO_PLAN)
            fit = self.ctx.fresh_prior
        fit = planning_fit(fit, self.ctx.fresh_prior, self.ctx.th("deg_rmse_bad_ms", 800))
        settings = self.ctx.store.current()
        ahead = (
            RivalView(
                snap.rival_ahead_idx,
                snap.rival_ahead_name,
                snap.rival_ahead_pace_ms,
                snap.rival_ahead_pitted,
            )
            if snap.rival_ahead_idx >= 0
            else None
        )
        behind, behind_gap_s = _true_behind_view(snap)
        raw_mode = resolve_mindset(settings.mindsets, self._mindset())
        drv = self._driver_scale(settings, raw_mode)
        mode = self.ctx.mode()
        used = frozenset(c for c in self._used_compounds | {snap.tyre_visual} if c)
        cause, damage_pace_ms, wing_loss_s = self._plan_damage(snap, fit)
        if (snap.is_sprint or snap.short_race) and cause not in ("damage", "pace"):
            self.ctx.pit_plan = NO_PLAN
            self.ctx.state.set_model(dataclasses.replace(self.ctx.state.model, **_NO_PIT_FIELDS))
            return self._plans(dataclasses.replace(snap, **_NO_PIT_FIELDS), fit, NO_PLAN)
        vsc_ending = snap.race_phase == "vsc" and snap.sc_ending
        pit_loss_s = snap.pit_loss_s + wing_loss_s
        sc_status = snap.safety_car_status if snap.race_phase in ("sc", "vsc") else 0
        if vsc_ending:
            pit_loss_s = (self.ctx.green_pit_loss_s or snap.pit_loss_s) + wing_loss_s
            sc_status = 0
        verdict = self._verdict(snap, fit, damage_pace_ms, wing_loss_s)
        plan = optimise(
            lap_num=snap.lap_num,
            laps_remaining=snap.laps_remaining,
            tyre_age=snap.tyre_age_laps,
            wear_mean=snap.wear_mean_pct,
            fit=fit,
            fresh=self.ctx.fresh_prior,
            laps_of_pace=snap.laps_of_pace,
            pit_loss_s=pit_loss_s,
            damage_pace_ms=damage_pace_ms,
            pit_loss_source=snap.pit_loss_source,
            green_pit_loss_s=self.ctx.green_pit_loss_s,
            sc_status=sc_status,
            rival_ahead=ahead,
            rival_behind=behind,
            gap_ahead_s=snap.gap_ahead_s,
            gap_behind_s=snap.gap_behind_s,
            rival_behind_gap_s=behind_gap_s,
            pit_exit_clean=snap.pit_exit_clean,
            restricted=snap.rival_data_restricted,
            stop_owed=self.plan_tracker.state.stops_left > 0,
            stop_mandatory=compound_stop_owed(used, self._plan_thresholds(snap, False)),
            driver=drv,
            mode=mode,
            th=settings.thresholds,
            teammate_pitting=snap.teammate_stacking,
            field=snap.field_cars,
            sc_verdict=verdict if sc_status != 0 else None,
        )
        if snap.safety_car_status == 0:
            session_uid = self.ctx.state.session_uid
            raw_cadence = mode.get("mc_every_n_laps", 1)
            mc_every = max(1, int(raw_cadence) if isinstance(raw_cadence, int | float) else 1)
            raw_sims = mode.get("plan_mc_sims", settings.thresholds.get("plan_mc_sims", 500))
            mc_sims = int(raw_sims) if isinstance(raw_sims, int | float) else 500
            lap_slot = snap.lap_num if mc_every == 1 else (snap.lap_num - 1) // mc_every
            age_slot = snap.tyre_age_laps if mc_every == 1 else snap.lap_num - snap.tyre_age_laps
            key = (
                session_uid,
                lap_slot,
                plan.lap,
                age_slot,
                threshold(mode, "pit_gain_min_s", 1.0),
                damage_pace_ms,
                wing_loss_s,
                plan.traffic_penalties_s,
                plan.plan,
                plan.rival_idx,
                snap.teammate_stacking,
                mc_sims,
            )
            if key not in self._plan_mc_cache:
                uid = session_uid if session_uid is not None else 0
                rng = np.random.default_rng(
                    [uid & 0xFFFFFFFF, (uid >> 32) & 0xFFFFFFFF, snap.lap_num]
                )
                self._plan_mc_cache[key] = plan_agreement(
                    lap_num=snap.lap_num,
                    laps_remaining=snap.laps_remaining,
                    tyre_age=snap.tyre_age_laps,
                    fit=fit,
                    fresh=self.ctx.fresh_prior,
                    cur_cliff_age=snap.tyre_age_laps + snap.laps_of_pace,
                    pit_loss_s=snap.pit_loss_s + wing_loss_s,
                    damage_pace_ms=damage_pace_ms,
                    stack_s=(
                        threshold(settings.thresholds, "double_stack_loss_s", 4.0)
                        if snap.teammate_stacking
                        else 0.0
                    ),
                    pit_loss_samples_s=self.ctx.green_pit_samples_s,
                    sc_pit_loss_s=self.ctx.sc_pit_loss_s,
                    plan_lap=plan.lap,
                    mode=mode,
                    th=settings.thresholds,
                    rng=rng,
                    plan_name=plan.plan,
                    cover_gap_s=behind_gap_s,
                    traffic_penalties_s=plan.traffic_penalties_s,
                )
            plan = scale_plan(plan, self._plan_mc_cache[key], lap_num=snap.lap_num)
        plan, self._held_plan = _hold_cover_plan(
            self._held_plan, plan, snap, self.ctx.th("pit_min_laps_left", 2)
        )
        sc_call = ""
        if verdict is not None and sc_status != 0:
            if plan.plan == "cheap_stop":
                sc_call = "box"
            elif plan.plan == "stay" and damage_pace_ms <= 0 and not verdict.hold:
                sc_call = "stay"
        self.ctx.pit_plan = plan
        self.ctx.state.set_model(
            dataclasses.replace(
                self.ctx.state.model,
                pit_plan=plan.plan,
                pit_plan_lap=plan.lap,
                pit_plan_gain_s=plan.gain_s,
                pit_plan_confidence=plan.confidence,
                pit_plan_risk=plan.risk,
                pit_plan_risky=plan.risky,
                pit_plan_rival_idx=plan.rival_idx,
                pit_plan_rival_name=plan.rival_name,
                pit_plan_reason=plan.reason,
                pit_window_start=plan.window[0],
                pit_window_end=plan.window[1],
                undercut_s=plan.undercut_s,
                overcut_s=plan.overcut_s,
            )
        )
        snap = dataclasses.replace(
            snap,
            pit_plan=plan.plan,
            pit_plan_lap=plan.lap,
            pit_plan_gain_s=plan.gain_s,
            pit_plan_confidence=plan.confidence,
            pit_plan_risk=plan.risk,
            pit_plan_risky=plan.risky,
            pit_plan_rival_idx=plan.rival_idx,
            pit_plan_rival_name=plan.rival_name,
            pit_plan_reason=plan.reason,
            pit_window_start=plan.window[0],
            pit_window_end=plan.window[1],
            undercut_s=plan.undercut_s,
            overcut_s=plan.overcut_s,
            sc_call=sc_call,
        )
        return self._plans(snap, fit, plan)

    def _compound_models(self, snap: Snapshot, cur: DegFit) -> tuple[CompoundModel, ...]:
        """Fresh-set pace per dry compound: learned/overlay deg for the set's
        actual compound when known, else the current slope scaled by the
        per-compound factor; sets left from the Tyre Sets packet."""
        settings = self.ctx.store.current()
        current = snap.tyre_visual
        names = {16: "soft", 17: "medium", 18: "hard"}
        usable = _usable_life(snap)
        out: list[CompoundModel] = []
        for v in DRY:
            actual = next((s.actual for s in snap.tyre_sets if s.visual == v and s.actual), 0)
            deg = cur.deg_ms_per_lap * (
                self.ctx.th(f"plan_deg_factor_{names[v]}", 1.0)
                / self.ctx.th(f"plan_deg_factor_{names.get(current, 'medium')}", 1.0)
            )
            deg_sd = cur.deg_posterior_sd_ms_per_lap * (
                self.ctx.th(f"plan_deg_factor_{names[v]}", 1.0)
                / self.ctx.th(f"plan_deg_factor_{names.get(current, 'medium')}", 1.0)
            )
            if actual:
                prior = self.model._compound_priors.get(actual)
                if prior is None:
                    prior = self.model._deg_prior(snap.track_id, actual, settings)
                    self.model._compound_priors[actual] = prior
                if prior.source in ("learned", "overlay"):
                    deg = prior.deg_ms_per_lap
                    deg_sd = 0.0
            off = self.ctx.th(f"plan_pace_{names[v]}_ms", 0.0)
            off_cur = (
                self.ctx.th(f"plan_pace_{names[current]}_ms", 0.0) if current in names else 0.0
            )
            sets = (
                sum(1 for s in snap.tyre_sets if s.visual == v and s.available and not s.fitted)
                if snap.tyre_sets
                else -1
            )
            out.append(
                CompoundModel(
                    v,
                    cur.base_ms + off - off_cur,
                    deg,
                    sets,
                    deg_posterior_sd_ms_per_lap=deg_sd,
                    usable_life_laps=usable.get(v, 0),
                )
            )
        return tuple(out)

    def _plans(self, snap: Snapshot, cur: DegFit, pit: PitPlan) -> Snapshot:
        """Named strategy plans: recompute on each lap / stop / SC change,
        persist set/switch/off events, fold scalars into snapshot + model."""
        tracker = self.plan_tracker
        if snap.pit_status != 0 and self._prev_pit_status == 0:
            previous = view_fields(tracker.state, snap.lap_num)
            self._stop_expect = (
                snap.active_plan or previous.active_plan,
                (snap.plan_next_compound or previous.plan_next_compound).upper(),
                snap.num_pit_stops,
            )
            self._stop_check = None
        elif snap.pit_status == 0 and self._prev_pit_status > 0:
            expect = self._stop_expect
            # A drive-through or stop-go also passes the pit lane; only a real stop
            # raises num_pit_stops (the game counts it in the box, before pit exit).
            if expect is not None and expect[1] and snap.num_pit_stops > expect[2]:
                self._stop_check = (expect[0], expect[1], snap.lap_num, snap.session_time)
            self._stop_expect = None
        if self._stop_check is not None and snap.lap_num != self._stop_check[2]:
            self._stop_check = None
        self._prev_pit_status = snap.pit_status
        snap = dataclasses.replace(
            snap,
            stop_plan=self._stop_check[0] if self._stop_check is not None else "",
            stop_expected_compound=self._stop_check[1] if self._stop_check is not None else "",
        )
        phase = snap.race_phase
        stops = snap.num_pit_stops
        if (
            phase in ("sc", "vsc")
            and self._prev_num_pit_stops is not None
            and stops > self._prev_num_pit_stops
        ):
            self._stops_done += 1
            self._sc_counted_stops = stops
        if (
            phase == "out_lap"
            and self._prev_race_phase != "out_lap"
            and stops != self._sc_counted_stops
        ):
            self._stops_done += 1
        self._prev_race_phase = phase
        self._prev_num_pit_stops = stops
        if phase == "red_flag":
            self._red_restart = True  # rebuild the plan from the restart tyres
            self._plan_key = None
        if phase in ("racing", "sc", "vsc") and snap.tyre_visual and snap.laps_remaining > 0:
            self._used_compounds.add(snap.tyre_visual)
            neutral = phase in ("sc", "vsc") and not (phase == "vsc" and snap.sc_ending)
            cheap = pit.plan == "cheap_stop"
            invalidate, damage_pace_ms, wing_loss_s = self._plan_damage(snap, cur)
            key = (
                snap.lap_num,
                self._stops_done,
                phase if neutral else "",
                cheap,
                snap.tyre_visual,
                self._mindset(),
                invalidate,
                round(damage_pace_ms / 100),
                round(wing_loss_s, 1),
                self._sc_verdict.sequence if self._sc_verdict is not None else None,
                snap.is_sprint,
            )
            if key != self._plan_key:
                if self._plan_key is not None and self._plan_key[-1] != snap.is_sprint:
                    tracker.reset()  # the weekend format arrived: a fresh plan, not a switch
                reseed = self._plan_key is not None and self._plan_key[5] != self._mindset()
                self._plan_key = key
                inp = self._plan_inputs(snap, cur, damage_pace_ms, wing_loss_s)
                tracker.update(
                    inp,
                    self._plan_thresholds(snap, invalidate in ("damage", "pace")),
                    stops_done=self._stops_done,
                    neutralised=neutral,
                    cheap_stop=cheap,
                    reseed=reseed,
                    invalidate=invalidate,
                    restart=self._red_restart,
                    sc_sequence=None if self._sc_verdict is None else self._sc_verdict.sequence,
                )
                self._red_restart = False
                for ev in tracker.drain():
                    self._persist_plan_event(snap, ev, tracker.state)
        if not self._sc_gate(snap):
            self._sc_verdict = None
        snap = dataclasses.replace(
            snap,
            sc_verdict=self._sc_verdict,
            sc_call=snap.sc_call if self._sc_verdict is not None else "",
        )
        st: PlanState = tracker.state
        if not st.active:
            return snap
        fields = view_fields(st, snap.lap_num)
        self.ctx.state.set_model(_with_plan(self.ctx.state.model, fields))
        return _with_plan(snap, fields)

    def _sc_gate(self, snap: Snapshot) -> bool:
        """A dry race under SC or VSC with laps left to plan for (docs/27)."""
        phase = snap.race_phase
        return (
            phase in ("sc", "vsc")
            and not (phase == "vsc" and snap.sc_ending)
            and not snap.is_sprint
            and not snap.short_race
            and snap.laps_remaining > self.ctx.th("sc_stop_min_laps_left", 3)
            and snap.tyre_visual in DRY
        )

    def _plan_inputs(
        self, snap: Snapshot, cur: DegFit, damage_pace_ms: float, wing_loss_s: float
    ) -> PlanInputs:
        lop = snap.laps_of_pace
        cliff = self.ctx.th("tyre_cliff_ms", 1500)
        cur_cliff_age = snap.tyre_age_laps + lop if math.isfinite(lop) else cliff_age(cur, cliff)
        return PlanInputs(
            lap_num=snap.lap_num,
            laps_remaining=snap.laps_remaining,
            tyre_age=snap.tyre_age_laps,
            current=snap.tyre_visual,
            cur=cur,
            cur_cliff_age=cur_cliff_age,
            compounds=self._compound_models(snap, cur),
            used=frozenset(c for c in self._used_compounds | {snap.tyre_visual} if c),
            green_loss_s=self.ctx.green_pit_loss_s or snap.pit_loss_s,
            sc_loss_s=self.ctx.sc_pit_loss_s or snap.pit_loss_s,
            wing_change_s=wing_loss_s,
            damage_pace_ms=damage_pace_ms,
        )

    def _verdict(
        self, snap: Snapshot, cur: DegFit, damage_pace_ms: float, wing_loss_s: float
    ) -> ScVerdict | None:
        """The safety-car verdict (docs/27), computed once per lap. Each lap
        writes one suppressed decision record with the pick and the ladder."""
        if not self._sc_gate(snap):
            self._sc_verdict = None
            return None
        uid = self.ctx.state.session_uid
        if (uid, snap.lap_num) in self._sc_verdict_laps:
            return self._sc_verdict
        self._sc_verdict_laps.add((uid, snap.lap_num))
        inp = self._plan_inputs(snap, cur, damage_pace_ms, wing_loss_s)
        if snap.tyre_sets:
            sets = {
                16: snap.fresh_sets_soft,
                17: snap.fresh_sets_medium,
                18: snap.fresh_sets_hard,
            }
        else:
            sets = {}
        usable = _usable_life(snap)
        th = self._plan_thresholds(snap, False)
        learned = threshold(th, "sc_survive_min", 0.60) + self.ctx.sc_survive_offset
        survive_min = min(
            threshold(th, "sc_survive_min_ceiling", 0.85),
            max(threshold(th, "sc_survive_min_floor", 0.45), learned),
        )
        verdict = sc_verdict(
            inp,
            fresh_sets=sets,
            usable_life=usable,
            cur_wear_pct=snap.wear_max_pct,
            cur_wear_per_lap_pct=snap.wear_per_lap_pct,
            stacking=snap.teammate_stacking,
            th={**th, "sc_survive_min": round(survive_min, 3)},
            rain_due=snap.weather_crossover in ("to_inter", "to_wet"),
            penalty_s=float(snap.penalty_s),
        )
        self._sc_verdict = verdict
        self.ctx.dispatcher.log.write(
            {
                "t": snap.now,
                "session_time": snap.session_time,
                "lap": snap.lap_num,
                "lap_distance": snap.lap_distance,
                "call_id": f"sc_verdict:{snap.lap_num}",
                "rule_id": "sc_verdict",
                "priority": 2,
                "urgency": "tactical",
                "rank": 3,
                "outcome": "suppressed",
                "suppressed_by": "record_only",
                "text": "",
                "inputs": {
                    "pick": verdict.pick,
                    "pick_compound": WORDS.get(verdict.pick, ""),
                    "reason": verdict.reason,
                    "laps_needed": verdict.laps_needed,
                    "saving_s": verdict.saving_s,
                    "survive_min": verdict.survive_min,
                    "ladder": [dataclasses.asdict(row) for row in verdict.rows],
                    "sequence": list(verdict.sequence),
                    "to_flag": verdict.to_flag,
                    "hold": verdict.hold,
                    "penalty_s": snap.penalty_s,
                },
                "active_plan": snap.active_plan,
                "on_plan": snap.on_plan if snap.active_plan else None,
            }
        )
        return verdict

    def _persist_plan_event(self, snap: Snapshot, ev: PlanEvent, st: PlanState) -> None:
        plans = [
            {
                "id": p.id,
                "sequence": p.sequence,
                "window": list(p.window),
                "delta_s": p.delta_s,
                "delta_sd_s": p.delta_sd_s,
                "switch_prob": p.switch_prob,
            }
            for p in st.plans
        ]
        record = {
            "t": snap.now,
            "session_time": snap.session_time,
            "lap": ev.lap,
            "rule_id": None,
            "outcome": "plan",
            "kind": ev.kind,
            "from_plan": ev.from_plan,
            "to_plan": ev.to_plan,
            "reason": ev.reason,
            "delta_s": ev.delta_s,
            "sequence": ev.sequence,
            "plans": plans,
            "off_prob": st.off_prob,
        }
        self.ctx.dispatcher.log.write(record)
        if self.ctx.db is not None and self.ctx.state.session_uid is not None:
            self.ctx.db.insert_plan_event(self.ctx.state.session_uid, record)
