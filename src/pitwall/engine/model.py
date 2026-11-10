from __future__ import annotations

import collections
import dataclasses
import logging
import math
from collections.abc import Callable
from statistics import median
from typing import Any

from pitwall.engine.context import EngineContext
from pitwall.model.budget import EnergyBudget, energy_budget, fuel_budget
from pitwall.model.deg import (
    DEG_FUEL_REF,
    DegFit,
    Prior,
    corner_wear_life,
    fit_is_clean,
    fit_laps,
    fit_stint,
    fuel_adjusted_deg,
    laps_of_pace,
    planning_fit,
    resolve_prior,
    scoped,
    session_base_ms,
)
from pitwall.model.driver import (
    DRIVER_COMPOUND,
    GLOBAL_TRACK_ID,
    PARAM_NAMES,
    RATING,
    DriverInputs,
    compute_profile,
    fold_weight,
    learned_rating,
)
from pitwall.model.pitloss import current_pit_loss
from pitwall.state.lap import LapSummary
from pitwall.state.model_view import ModelView
from pitwall.state.session import ErsLapTotals, thermal_window, widen_window
from pitwall.store.db import LapRow
from pitwall.strategy.battle import (
    HOLD,
    PASS_COMPOUND,
    PASS_NO_OT,
    PASS_OT,
    BattleRates,
    shrink,
)

log = logging.getLogger(__name__)

_NON_GREEN_REASONS = frozenset({"first_lap", "pitted", "after_in_lap", "safety_car", "flashback"})


@dataclasses.dataclass(frozen=True, slots=True)
class _EnergyLapLatch:
    lap_num: int
    start_store_j: float
    start_laps_remaining: int
    gradable: bool


class _EnergyLapTracker:
    def __init__(self) -> None:
        self._latch: _EnergyLapLatch | None = None
        self._finishing: _EnergyLapLatch | None = None
        self._graded_seq: int | None = None
        self._partial_next_latch = False
        self.prev_lap: EnergyBudget | None = None

    def reset(self) -> None:
        self._latch = None
        self._finishing = None
        self._graded_seq = None
        self._partial_next_latch = True
        self.prev_lap = None

    def _start_latch(
        self,
        lap_num: int,
        laps_remaining: int,
        store_j: float,
        *,
        gradable: bool,
    ) -> None:
        self._latch = _EnergyLapLatch(lap_num, store_j, laps_remaining, gradable)
        self._partial_next_latch = False

    def _grade(
        self,
        lap: _EnergyLapLatch,
        counters: tuple[float, float],
        *,
        store_j: float,
        store_capacity_j: float,
        soc_floor_pct: float,
        over_tolerance_j: float,
        attack_ok: bool,
    ) -> None:
        self.prev_lap = None
        if not lap.gradable:
            return
        deployed_j, harvested_j = counters
        self.prev_lap = energy_budget(
            store_j=store_j,
            allowance_store_j=lap.start_store_j,
            store_capacity_j=store_capacity_j,
            laps_remaining=lap.start_laps_remaining,
            deployed_this_lap_j=deployed_j,
            harvested_this_lap_j=harvested_j,
            soc_floor_pct=soc_floor_pct,
            over_tolerance_j=over_tolerance_j,
            attack_ok=attack_ok,
        )

    def observe(
        self,
        lap_num: int,
        laps_remaining: int,
        store_j: float,
        dep: float,
        harv: float,
        *,
        counters_current: bool,
        finished: ErsLapTotals | None,
        store_capacity_j: float,
        soc_floor_pct: float,
        over_tolerance_j: float,
        attack_ok: bool,
    ) -> tuple[float, float, float | None, int]:
        if lap_num < 1:
            if self._latch is not None or self._finishing is not None:
                self.reset()
            live_dep, live_harv = (dep, harv) if counters_current else (0.0, 0.0)
            return live_dep, live_harv, None, laps_remaining

        if self._latch is None:
            self._start_latch(
                lap_num,
                laps_remaining,
                store_j,
                gradable=lap_num == 1 and not self._partial_next_latch,
            )
        elif lap_num == self._latch.lap_num + 1:
            self._finishing = self._latch
            self.prev_lap = None
            self._start_latch(lap_num, laps_remaining, store_j, gradable=True)
        elif lap_num != self._latch.lap_num:
            self._finishing = None
            self.prev_lap = None
            self._start_latch(lap_num, laps_remaining, store_j, gradable=False)

        if finished is not None and finished.seq != self._graded_seq:
            finishing = self._finishing
            if finishing is not None and finished.lap_num == finishing.lap_num:
                self._grade(
                    finishing,
                    (finished.deployed_j, finished.harvested_j),
                    store_j=store_j,
                    store_capacity_j=store_capacity_j,
                    soc_floor_pct=soc_floor_pct,
                    over_tolerance_j=over_tolerance_j,
                    attack_ok=attack_ok,
                )
                self._finishing = None
            self._graded_seq = finished.seq

        return self._live_values(
            laps_remaining,
            dep,
            harv,
            counters_current=counters_current,
        )

    def _live_values(
        self,
        laps_remaining: int,
        dep: float,
        harv: float,
        *,
        counters_current: bool,
    ) -> tuple[float, float, float | None, int]:
        live_dep, live_harv = (dep, harv) if counters_current else (0.0, 0.0)
        lap = self._latch
        if lap is not None and lap.gradable:
            return live_dep, live_harv, lap.start_store_j, lap.start_laps_remaining
        return live_dep, live_harv, None, laps_remaining


class SessionModel:
    def __init__(
        self, ctx: EngineContext, apply_mode: Callable[[], None], laps_written: Callable[[], int]
    ) -> None:
        self.ctx = ctx
        self._apply_mode = apply_mode
        self._laps_written = laps_written
        self._compound_priors: dict[int, DegFit] = {}
        self._battle_rates: tuple[int, BattleRates] | None = None
        self._energy_lap_tracker = _EnergyLapTracker()
        self._weekend_prior_cache: dict[tuple[int, int], tuple[float, int]] = {}
        self._fuel_last_kg: float | None = None
        self._session_fuel_last_kg: float | None = None
        self._session_fuel_deltas: list[tuple[float, float]] = []
        self._run_fuel_seq = ctx.state.run_seq
        self._run_fuel_deltas: list[tuple[float, float]] = []
        self._run_fuel_last_kg: float | None = None
        self._stint_fuel_ref_laps = 0.0
        self._model_key: tuple[int | None, int, int, int] | None = None
        self._fuel_prior = Prior(0.0, 0.0, "default")
        self._wear_per_lap = 0.0
        self._stint_wear0: tuple[float, ...] = ()
        self._stint_age0 = 0.0
        self._stint_compound0 = -1

    def reset(self) -> None:
        self._reset_energy_lap_tracking()
        self._weekend_prior_cache.clear()
        self._fuel_last_kg = None
        self._session_fuel_last_kg = None
        self._session_fuel_deltas = []
        self._run_fuel_seq = self.ctx.state.run_seq
        self._run_fuel_deltas = []
        self._run_fuel_last_kg = None
        self._battle_rates = None
        self._stint_wear0 = ()
        self._stint_compound0 = -1
        self._stint_fuel_ref_laps = 0.0

    def rewind(self, t: float) -> None:
        self._run_fuel_deltas = [entry for entry in self._run_fuel_deltas if entry[0] <= t]
        self._run_fuel_last_kg = None
        self._session_fuel_deltas = [entry for entry in self._session_fuel_deltas if entry[0] <= t]
        self._session_fuel_last_kg = None
        self._reset_energy_lap_tracking()

    def _reset_energy_lap_tracking(self) -> None:
        self._energy_lap_tracker.reset()
        self.ctx.energy_prev_lap = None

    def _fit_current_stint(
        self,
        tail: list[LapRow],
        track_id: int,
        settings: Any,
        fuel_fixed: float | None,
    ) -> DegFit:
        self.ctx.stint_laps = tail
        compound = tail[0].compound
        prior = self._deg_prior(track_id, compound, settings)
        thermal_hi_c = self._thermal_hi_c(track_id, compound, settings)
        min_laps = int(self.ctx.th("deg_min_laps", 3))
        usable = fit_laps(tail, thermal_hi_c, min_laps)
        if usable and all(lap.fuel_kg > 0 for lap in usable):
            self.ctx.stint_fuel_ref = max(lap.fuel_kg for lap in usable)
            self._stint_fuel_ref_laps = 0.0
        elif usable:
            self.ctx.stint_fuel_ref = 0.0
            self._stint_fuel_ref_laps = max(lap.fuel_remaining_laps for lap in usable)
        else:
            self.ctx.stint_fuel_ref = max((lap.fuel_kg for lap in tail), default=0.0)
            self._stint_fuel_ref_laps = 0.0
        fit = fit_stint(
            tail,
            prior,
            min_laps=min_laps,
            fuel_coeff_fixed=fuel_fixed,
            deg_max_ms_per_lap=self.ctx.th("deg_max_ms_per_lap", 600),
            deg_rmse_bad_ms=self.ctx.th("deg_rmse_bad_ms", 800),
            thermal_hi_c=thermal_hi_c,
            quadratic=True,
            quad_min_laps=int(self.ctx.th("deg_quad_min_laps", 8)),
            quad_min_t=self.ctx.th("deg_quad_min_t", 2.0),
        )
        self.ctx.deg_fit = fit
        return fit

    def _sync_run_fuel(self) -> None:
        if self._run_fuel_seq != self.ctx.state.run_seq:
            self._run_fuel_seq = self.ctx.state.run_seq
            self._run_fuel_deltas = []
            self._run_fuel_last_kg = None

    def _fit_closed_stint(
        self, tail: list[LapRow], track_id: int, settings: Any, fuel_fixed: float | None
    ) -> DegFit:
        prior = self._deg_prior(track_id, tail[0].compound, settings)
        return fit_stint(
            tail,
            prior,
            min_laps=int(self.ctx.th("deg_min_laps", 3)),
            fuel_coeff_fixed=fuel_fixed,
            deg_max_ms_per_lap=self.ctx.th("deg_max_ms_per_lap", 600),
            deg_rmse_bad_ms=self.ctx.th("deg_rmse_bad_ms", 800),
            thermal_hi_c=self._thermal_hi_c(track_id, tail[0].compound, settings),
        )

    def _fit_player_stint(self, tail: list[LapRow], track_id: int, settings: Any) -> DegFit:
        compound = tail[0].compound
        fuel_name = self._learned_name(track_id, compound, "fuel_ms_per_lap")
        fuel_param = self.ctx.db.get_param(track_id, compound, fuel_name)
        fuel_fixed = (
            fuel_param.value
            if fuel_param is not None and fuel_param.weight >= self.ctx.th("prior_min_weight", 2)
            else None
        )
        fit = self._fit_current_stint(tail, track_id, settings, fuel_fixed)
        if fit.deg2_ms_per_lap2 > 0:
            return self._fit_closed_stint(tail, track_id, settings, fuel_fixed)
        return fit

    def _fold_lap_fuel(self, uid: int, lap: LapSummary, track_id: int) -> None:
        if lap.valid and lap.fuel_kg > 0:
            if self._fuel_last_kg is not None:
                delta = self._fuel_last_kg - lap.fuel_kg
                if (
                    track_id >= 0
                    and not self.ctx.synthetic_session(uid)
                    and self.ctx.th("fuel_delta_min_kg", 0)
                    < delta
                    < self.ctx.th("fuel_delta_max_kg", 10)
                ):
                    self.ctx.db.fold_param(
                        track_id,
                        0,
                        "fuel_kg_per_lap",
                        delta,
                        param_weight_cap=self.ctx.th("param_weight_cap", 50),
                    )
            self._fuel_last_kg = lap.fuel_kg
        elif lap.fuel_kg > 0:
            self._fuel_last_kg = lap.fuel_kg

    def _note_session_fuel(self, lap: LapSummary) -> None:
        """Burn per lap measured this session; pit, SC and flashback laps break the chain."""
        self._sync_run_fuel()
        if lap.fuel_kg <= 0:
            return
        last = self._session_fuel_last_kg
        green = lap.sc_status == 0 and not set(lap.invalid_reasons) & _NON_GREEN_REASONS
        t = self.ctx.state.last_packet_t or 0.0
        if green and last is not None and 0.0 < last - lap.fuel_kg < 10.0:
            self._session_fuel_deltas.append((t, last - lap.fuel_kg))
        self._session_fuel_last_kg = lap.fuel_kg if green else None
        # A run counts from the end of its out lap. When the out lap is not a counted
        # lap, the hot lap carries its pit flags, so start it from the crossing fuel.
        run_last, run_green = self._run_fuel_last_kg, green
        hot_base = self.ctx.state.hot_lap_fuel_base
        if hot_base is not None and hot_base[0] == lap.lap_num:
            run_last = hot_base[1]
            run_green = lap.sc_status == 0 and not set(lap.invalid_reasons) & (
                _NON_GREEN_REASONS - {"first_lap", "pitted"}
            )
        if run_green and run_last is not None and 0.0 < run_last - lap.fuel_kg < 10.0:
            self._run_fuel_deltas.append((t, run_last - lap.fuel_kg))
        self._run_fuel_last_kg = lap.fuel_kg

    def _fuel_per_lap(self, laps_remaining: int) -> tuple[float, str]:
        """This session's measured burn, else the game's own MFD estimate, else the prior."""
        deltas = self._session_fuel_deltas
        if len(deltas) >= int(self.ctx.th("fuel_session_min_laps", 2)):
            return median(delta for _, delta in deltas[-8:]), "session"
        state = self.ctx.state
        laps_in_tank = laps_remaining + state.fuel_remaining_laps
        if laps_remaining > 0 and state.fuel_in_tank > 0 and laps_in_tank > 0.5:
            per_lap = state.fuel_in_tank / laps_in_tank
            if 0.2 <= per_lap <= 5.0:
                return per_lap, "mfd"
        return self._fuel_prior.value, self._fuel_prior.source

    def _stint_rows(self, laps: list[LapRow]) -> list[LapRow]:
        """Trailing contiguous stint (same compound, increasing tyre age)."""
        tail: list[LapRow] = []
        for row in reversed(laps):
            if tail and (
                row.compound != tail[-1].compound or row.tyre_age_laps >= tail[-1].tyre_age_laps
            ):
                break
            tail.append(row)
        tail.reverse()
        return tail

    def _deg_prior(self, track_id: int, compound: int, settings: Any) -> DegFit:
        overlay = settings.track
        min_w = self.ctx.th("prior_min_weight", 2)
        uid = self.ctx.state.session_uid
        fuel_p = resolve_prior(
            self.ctx.db,
            track_id,
            compound,
            self._learned_name(track_id, compound, "fuel_ms_per_lap"),
            overlay_value=None,
            default=self.ctx.th("fuel_ms_per_lap_default", 30),
            min_weight=min_w,
        )
        deg_p: Prior | None = None
        if self.ctx.db is not None and uid is not None:
            cache_key = (uid, compound)
            weekend = self._weekend_prior_cache.get(cache_key)
            if weekend is None:
                rmse_bad = self.ctx.th("deg_rmse_bad_ms", 800)
                stints = [
                    stint
                    for stint in self.ctx.db.weekend_stints(uid, track_id, compound)
                    if stint.rmse_ms <= rmse_bad
                ]
                n = sum(stint.n_valid_laps for stint in stints)
                if n:
                    value = (
                        sum(
                            fuel_adjusted_deg(
                                stint.deg_ms_per_lap, stint.fuel_ms_per_lap, fuel_p.value
                            )
                            * stint.n_valid_laps
                            for stint in stints
                        )
                        / n
                    )
                else:
                    value = 0.0
                weekend = (value, n)
                self._weekend_prior_cache[cache_key] = weekend
            if weekend[1] >= self.ctx.th("weekend_min_laps", 6):
                deg_p = Prior(weekend[0], float(weekend[1]), "weekend")
        if deg_p is None:
            deg_p = resolve_prior(
                self.ctx.db,
                track_id,
                compound,
                self._learned_name(track_id, compound, "deg_ms_per_lap"),
                overlay_value=(
                    overlay.deg_ms_per_lap.get(compound) if overlay is not None else None
                ),
                default=self.ctx.th("deg_default_ms_per_lap", 80),
                min_weight=min_w,
            )
        base_p = resolve_prior(
            self.ctx.db,
            track_id,
            compound,
            self._learned_name(track_id, compound, "base_ms"),
            overlay_value=(
                float(overlay.base_pace_ms)
                if overlay is not None and overlay.base_pace_ms > 0
                else None
            ),
            default=self.ctx.th("release_fallback_lap_s", 95.0) * 1000.0,
            min_weight=min_w,
        )
        deg = deg_p.value
        if deg_p.source == "learned" and self.ctx.db is not None:
            deg_name = self._learned_name(track_id, compound, "deg_ms_per_lap")
            suffix = deg_name.removeprefix("deg_ms_per_lap")
            ref = self.ctx.db.get_param(track_id, compound, DEG_FUEL_REF + suffix)
            deg = fuel_adjusted_deg(
                deg, ref.value if ref is not None and ref.weight >= min_w else None, fuel_p.value
            )
        confidence = {"weekend": 0.45, "learned": 0.5, "overlay": 0.35}.get(deg_p.source, 0.2)
        if deg_p.source == "learned":
            # A thin learned prior (few folded laps) is pulled toward the
            # overlay/default so one steep stint cannot set the plan alone.
            full = self.ctx.th("prior_full_weight", 12)
            trust = min(deg_p.weight / full, 1.0) if full > 0 else 1.0
            fallback = overlay.deg_ms_per_lap.get(compound) if overlay is not None else None
            if fallback is None:
                fallback = self.ctx.th("deg_default_ms_per_lap", 80)
            deg = trust * deg + (1.0 - trust) * float(fallback)
            confidence = 0.2 + (confidence - 0.2) * trust
        if base_p.source == "default" and self.ctx.db is not None and uid is not None:
            seeded = session_base_ms(self.ctx.db.laps_for(uid, 0), deg)
            if seeded > 0:
                base_p = Prior(seeded, 0.0, "session")
        return DegFit(
            base_ms=base_p.value,
            deg_ms_per_lap=deg,
            fuel_ms_per_lap=fuel_p.value,
            n=0,
            rmse_ms=0.0,
            confidence=confidence,
            source=deg_p.source,
        )

    def _fold_fit(self, track_id: int, compound: int, fit: DegFit) -> None:
        """Fold a stint fit into the learned priors when it is a clean fit on
        an identified track; noisy stints stay in `stints` only."""
        if (
            self.ctx.db is None
            or track_id < 0
            or self.ctx.synthetic_session()
            or not fit_is_clean(
                fit,
                deg_max_ms_per_lap=self.ctx.th("deg_max_ms_per_lap", 600),
                deg_rmse_bad_ms=self.ctx.th("deg_rmse_bad_ms", 800),
                base_min_ms=self.ctx.th("learn_base_min_ms", 40_000),
                base_max_ms=self.ctx.th("learn_base_max_ms", 200_000),
            )
        ):
            return
        race_laps = self._race_laps()
        params = [
            ("deg_ms_per_lap", fit.deg_ms_per_lap),
            (DEG_FUEL_REF, fit.fuel_ms_per_lap),
            ("base_ms", fit.base_ms),
        ]
        if fit.fuel_fitted:
            params.append(("fuel_ms_per_lap", fit.fuel_ms_per_lap))
        for name, value in params:
            self.ctx.db.fold_param(
                track_id,
                compound,
                scoped(name, race_laps),
                value,
                weight=float(fit.n),
                param_weight_cap=self.ctx.th("param_weight_cap", 50),
            )

    def _learned_name(self, track_id: int, compound: int, name: str, *, db: Any = None) -> str:
        """The race-distance prior once it has weight, else the practice one."""
        source_db = self.ctx.db if db is None else db
        name_at = scoped(name, self._race_laps())
        if name_at == name or source_db is None:
            return name
        p = source_db.get_param(track_id, compound, name_at)
        return name_at if p is not None and p.weight >= self.ctx.th("prior_min_weight", 2) else name

    def _thermal_hi_c(self, track_id: int, compound: int, settings: Any) -> float:
        learned_hi = (
            self.ctx.db.get_param(track_id, compound, "thermal_hi_c")
            if self.ctx.db is not None
            else None
        )
        min_weight = self.ctx.th("prior_min_weight", 2)
        if learned_hi is not None and learned_hi.weight >= min_weight:
            learned_lo = self.ctx.db.get_param(track_id, compound, "thermal_lo_c")
            if learned_lo is not None and learned_lo.weight >= min_weight:
                return widen_window(
                    float(learned_lo.value),
                    float(learned_hi.value),
                    self.ctx.th("tyre_window_min_width_c", 15.0),
                )[1]
            return float(learned_hi.value)
        return thermal_window(settings.thresholds, compound)[1]

    def _race_laps(self) -> int:
        return self.ctx.race_laps()

    def _refresh_driver_rating(self, track_id: int) -> None:
        if self.ctx.driver_db is None:
            self.ctx.driver_rating = (0.5, 0.0, "")
        else:
            self.ctx.driver_rating = learned_rating(
                self.ctx.driver_db.params_for_track(track_id),
                self.ctx.driver_db.params_for_track(GLOBAL_TRACK_ID),
                self.ctx.store.current().thresholds,
            )
        self._apply_mode()

    def _lap_fraction(self) -> float:
        state = self.ctx.state
        if state.track_length_m <= 0:
            return 0.0
        return min(max(state.lap_distance / state.track_length_m, 0.0), 1.0)

    def _update_model(self) -> None:
        """Refresh priors (once per lap) and budgets (every tick), then hand
        the whole model view to SessionState for the rules-facing snapshot."""
        state = self.ctx.state
        settings = self.ctx.store.current()
        mode = self.ctx.mode()
        state.set_mode_offsets(
            thermal_warn_offset_c=float(mode.get("thermal_warn_offset_c", 0) or 0),
            sector_loss_call_ms=float(mode.get("sector_loss_call_ms", 100) or 100),
        )
        track_id = state.track_id
        overlay = settings.track
        key = (
            state.session_uid,
            self._laps_written(),
            state.lap_num,
            state.safety_car_status,
        )
        if key != self._model_key:
            self._model_key = key
            self.ctx.pit_loss_prior = current_pit_loss(
                self.ctx.db,
                state.session_uid,
                track_id,
                state.safety_car_status,
                overlay,
                settings.thresholds,
            )
            self._fuel_prior = resolve_prior(
                self.ctx.db,
                track_id,
                0,
                "fuel_kg_per_lap",
                overlay_value=(
                    overlay.fuel_kg_per_lap
                    if overlay is not None and overlay.fuel_kg_per_lap > 0
                    else None
                ),
                default=self.ctx.th("fuel_kg_per_lap_default", 1.7),
                min_weight=self.ctx.th("prior_min_weight", 2),
            )
            green = current_pit_loss(
                self.ctx.db, state.session_uid, track_id, 0, overlay, settings.thresholds
            )
            self.ctx.green_pit_loss_s = green.value / 1000.0
            self.ctx.green_pit_samples_s = (
                [event.loss_ms / 1000.0 for event in self.ctx.db.pit_events_for_track(track_id, 0)]
                if self.ctx.db is not None
                else []
            )
            sc = current_pit_loss(
                self.ctx.db,
                state.session_uid,
                track_id,
                2 if state.safety_car_status == 2 else 1,
                overlay,
                settings.thresholds,
            )
            self.ctx.sc_pit_loss_s = sc.value / 1000.0
            self._compound_priors.clear()
            self.ctx.fresh_prior = self._deg_prior(track_id, state.tyre_compound, settings)
            # Wear rate this stint: median of positive consecutive deltas.
            wear_deltas = [
                b.wear_pct - a.wear_pct
                for a, b in zip(self.ctx.stint_laps, self.ctx.stint_laps[1:], strict=False)
                if a.wear_pct > 0 and b.wear_pct > a.wear_pct
            ]
            self._wear_per_lap = (
                median(wear_deltas) if wear_deltas else self.ctx.th("wear_per_lap_default_pct", 2.5)
            )

        laps_remaining = max(0, state.total_laps - state.lap_num + 1) if state.total_laps > 0 else 0
        lap_num = state.lap_num
        deployed_j = state.ers_deployed_this_lap_j
        harvested_j = state.ers_harvested_mguk_j + state.ers_harvested_mguh_j
        energy_capacity_j = self.ctx.th("ers_store_capacity_j", 4_000_000)
        energy_floor_pct = float(mode.get("ers_soc_floor_pct", 0) or 0)
        energy_over_tolerance_j = self.ctx.th("energy_over_tolerance_j", 400_000)
        energy_attack_ok = mode.get("ers_policy") == "attack_rival"
        (
            live_deployed_j,
            live_harvested_j,
            allowance_store_j,
            live_laps_remaining,
        ) = self._energy_lap_tracker.observe(
            lap_num,
            laps_remaining,
            state.ers_store_energy_j,
            deployed_j,
            harvested_j,
            counters_current=state.ers_counters_current,
            finished=state.ers_finished_lap,
            store_capacity_j=energy_capacity_j,
            soc_floor_pct=energy_floor_pct,
            over_tolerance_j=energy_over_tolerance_j,
            attack_ok=energy_attack_ok,
        )
        self.ctx.energy_prev_lap = self._energy_lap_tracker.prev_lap
        per_lap_kg, fuel_source = self._fuel_per_lap(laps_remaining)
        self.ctx.fuel_budget = fuel_budget(
            laps_remaining=laps_remaining,
            fuel_in_tank_kg=state.fuel_in_tank,
            per_lap_kg=per_lap_kg,
            source=fuel_source,
            lap_done=(
                state.lap_distance / state.track_length_m if state.track_length_m > 0 else 0.0
            ),
        )
        self.ctx.energy_budget = energy_budget(
            store_j=state.ers_store_energy_j,
            store_capacity_j=energy_capacity_j,
            laps_remaining=live_laps_remaining,
            deployed_this_lap_j=live_deployed_j,
            harvested_this_lap_j=live_harvested_j,
            allowance_store_j=allowance_store_j,
            soc_floor_pct=energy_floor_pct,
            over_tolerance_j=energy_over_tolerance_j,
            attack_ok=energy_attack_ok,
        )

        fit = self.ctx.deg_fit
        wear = state.tyres_wear.as_tuple()
        age = state.tyre_age_laps
        if (
            not self._stint_wear0
            or state.tyre_compound != self._stint_compound0
            or age < self._stint_age0
            or any(w < w0 - 1.0 for w, w0 in zip(wear, self._stint_wear0, strict=True))
        ):
            self._stint_wear0 = wear
            self._stint_age0 = age + self._lap_fraction()
            self._stint_compound0 = state.tyre_compound
        lop = corner_wear_life(
            wear,
            self._stint_wear0,
            age + self._lap_fraction() - self._stint_age0,
            wear_cliff_pct=self.ctx.th("wear_cliff_pct", 70),
            default_rate_pct=self._wear_per_lap,
        )
        predicted = 0
        pace_forecast = 0
        if fit is not None:
            lop = min(
                lop,
                laps_of_pace(
                    planning_fit(fit, self.ctx.fresh_prior, self.ctx.th("deg_rmse_bad_ms", 800))
                    if self.ctx.fresh_prior is not None
                    else fit,
                    age,
                    max(wear),
                    cliff_ms=self.ctx.th("tyre_cliff_ms", 1500),
                    wear_cliff_pct=self.ctx.th("wear_cliff_pct", 70),
                    wear_per_lap=0.0,
                ),
            )
            per_lap = self.ctx.fuel_budget.per_lap_kg if self.ctx.fuel_budget is not None else 0.0
            if self._stint_fuel_ref_laps > 0:
                burned = max(0.0, self._stint_fuel_ref_laps - state.fuel_remaining_laps)
            else:
                burned = (
                    max(0.0, self.ctx.stint_fuel_ref - state.fuel_in_tank) / per_lap
                    if per_lap > 0 and self.ctx.stint_fuel_ref > 0
                    else 0.0
                )
            predicted = int(
                fit.base_ms
                + fit.deg_ms_per_lap * (state.tyre_age_laps + 1)
                + fit.deg2_ms_per_lap2 * (state.tyre_age_laps + 1) ** 2
                - fit.fuel_ms_per_lap * (burned + 1)
            )
            pace_forecast = int(
                fit.base_ms
                + fit.deg_ms_per_lap * state.tyre_age_laps
                + fit.deg2_ms_per_lap2 * state.tyre_age_laps**2
                - fit.fuel_ms_per_lap * max(0.0, burned + 1 - self._lap_fraction())
            )
        eb = self.ctx.energy_budget
        fb = self.ctx.fuel_budget
        state.set_model(
            ModelView(
                driver_rating=self.ctx.driver_rating[0],
                driver_confidence=self.ctx.driver_rating[1],
                driver_rating_source=self.ctx.driver_rating[2],
                deg_fit_source=fit.source if fit is not None else "",
                deg_ms_per_lap=fit.deg_ms_per_lap if fit is not None else 0.0,
                deg_confidence=fit.confidence if fit is not None else 0.0,
                base_pace_ms=fit.base_ms if fit is not None else 0.0,
                laps_of_pace=lop,
                wear_per_lap_pct=self._wear_per_lap,
                pit_loss_s=(
                    self.ctx.pit_loss_prior.value / 1000.0
                    if self.ctx.pit_loss_prior is not None
                    else 0.0
                ),
                pit_loss_source=(
                    self.ctx.pit_loss_prior.source if self.ctx.pit_loss_prior is not None else ""
                ),
                sc_pit_loss_s=self.ctx.sc_pit_loss_s,
                green_pit_loss_s=self.ctx.green_pit_loss_s,
                fuel_margin_laps=fb.margin_laps if fb is not None else 0.0,
                fuel_per_lap_kg=fb.per_lap_kg if fb is not None else 0.0,
                run_fuel_per_lap_kg=(
                    sum(delta for _, delta in self._run_fuel_deltas) / len(self._run_fuel_deltas)
                    if self._run_fuel_deltas
                    else 0.0
                ),
                run_fuel_laps=len(self._run_fuel_deltas),
                fuel_source=fb.source if fb is not None else "",
                energy_per_lap_mj=eb.per_lap_j / 1e6 if eb is not None else 0.0,
                energy_lap_delta_mj=eb.lap_delta_j / 1e6 if eb is not None else 0.0,
                energy_prev_lap_delta_mj=(
                    self.ctx.energy_prev_lap.lap_delta_j / 1e6
                    if self.ctx.energy_prev_lap is not None
                    else 0.0
                ),
                energy_prev_lap_mode=(
                    self.ctx.energy_prev_lap.mode if self.ctx.energy_prev_lap is not None else ""
                ),
                energy_laps_to_floor=eb.laps_to_floor if eb is not None else math.inf,
                energy_mode=eb.mode if eb is not None else "",
                predicted_lap_ms=predicted,
                pace_forecast_ms=pace_forecast,
            )
        )

    def battle_rates(self, track_id: int) -> BattleRates:
        """Per-track pass / hold rates from model_params, shrunk to the priors."""
        if self._battle_rates is not None and self._battle_rates[0] == track_id:
            return self._battle_rates[1]
        w = self.ctx.th("battle_prior_weight", 4.0)
        priors = {
            PASS_OT: self.ctx.th("battle_pass_overtake_prior", 0.6),
            PASS_NO_OT: self.ctx.th("battle_pass_no_overtake_prior", 0.6),
            HOLD: self.ctx.th("battle_hold_prior", 0.7),
        }
        vals: dict[str, float] = {}
        for name, prior in priors.items():
            p = self.ctx.db.get_param(track_id, PASS_COMPOUND, name) if self.ctx.db else None
            vals[name] = shrink(p.value if p else None, p.weight if p else 0.0, prior, w)
        rates = BattleRates(vals[PASS_OT], vals[PASS_NO_OT], vals[HOLD])
        self._battle_rates = (track_id, rates)
        return rates

    def _fold_driver(self, uid: int) -> None:
        if (
            self.ctx.db is None
            or self.ctx.driver_db is None
            or self.ctx.state.track_id < 0
            or self.ctx.synthetic_session(uid)
            or self.ctx.driver_db.driver_folded(uid)
        ):
            return
        th = self.ctx.store.current().thresholds
        weight = fold_weight(self.ctx.state.session_type, th)
        if weight == 0:
            return
        event_warnings = self.ctx.state.infringement_warnings
        if self.ctx.state.cars_lap is not None and self.ctx.state.warnings < event_warnings:
            log.debug(
                "LapData total warnings %d below event infringement warnings %d",
                self.ctx.state.warnings,
                event_warnings,
            )
        track_id = self.ctx.state.track_id
        player_laps = [
            lap
            for lap in self.ctx.db.laps_for(uid, 0)
            if lap.valid == 1 and lap.sc_status == 0 and lap.lap_time_ms > 0 and lap.traffic == 0
        ]
        field_lap_times = []
        for car_idx in range(1, 22):
            clean = [
                lap.lap_time_ms
                for lap in self.ctx.db.laps_for(uid, car_idx)
                if lap.valid == 1
                and lap.sc_status == 0
                and lap.lap_time_ms > 0
                and lap.traffic == 0
            ]
            if len(clean) >= self.ctx.th("driver_min_laps", 3):
                field_lap_times.append(float(median(clean)))

        compound = (
            collections.Counter(lap.compound for lap in player_laps).most_common(1)[0][0]
            if player_laps
            else 0
        )
        base_name = self._learned_name(track_id, compound, "base_ms", db=self.ctx.driver_db)
        base_param = self.ctx.driver_db.get_param(track_id, compound, base_name)
        base_ms = float(base_param.value) if base_param is not None else None
        stints = [stint for stint in self.ctx.db.stints_for_session(uid) if stint.n_valid_laps > 0]
        stint_laps = sum(stint.n_valid_laps for stint in stints)
        deg_ms_per_lap = (
            sum(stint.deg_ms_per_lap * stint.n_valid_laps for stint in stints) / stint_laps
            if stint_laps
            else None
        )
        deg_name = self._learned_name(track_id, compound, "deg_ms_per_lap", db=self.ctx.driver_db)
        deg_param = self.ctx.driver_db.get_param(track_id, compound, deg_name)
        deg_prior = float(deg_param.value) if deg_param is not None else None
        profile = compute_profile(
            DriverInputs(
                lap_times_ms=tuple(lap.lap_time_ms for lap in player_laps),
                field_lap_times_ms=tuple(int(value) for value in field_lap_times),
                base_ms=base_ms,
                deg_ms_per_lap=deg_ms_per_lap,
                deg_prior_ms_per_lap=deg_prior,
                laps=max((lap.lap_num for lap in self.ctx.db.laps_for(uid, 0)), default=0),
                lockups=self.ctx.state.incident_lockups,
                spins=self.ctx.state.incident_spins,
                offs=self.ctx.state.incident_offs,
                contacts=self.ctx.state.incident_contacts,
                grid_position=self.ctx.state.grid_position,
                finish_position=self.ctx.state.position,
                overtakes_made=(
                    self.ctx.state.overtakes.made if self.ctx.state.event_telemetry_seen else None
                ),
                overtakes_lost=(
                    self.ctx.state.overtakes.lost if self.ctx.state.event_telemetry_seen else None
                ),
                warnings=(
                    self.ctx.state.infringement_warnings
                    if self.ctx.state.event_telemetry_seen
                    else None
                ),
                penalties=(
                    self.ctx.state.infringement_penalties
                    if self.ctx.state.event_telemetry_seen
                    else None
                ),
            ),
            th,
        )
        if profile is None:
            return
        values = [
            (PARAM_NAMES[name], value)
            for name in PARAM_NAMES
            if (value := getattr(profile, name)) is not None
        ]
        values.append((RATING, profile.rating))
        cap = self.ctx.th("param_weight_cap", 50)
        for target_track in (track_id, GLOBAL_TRACK_ID):
            for name, value in values:
                self.ctx.driver_db.fold_param(
                    target_track,
                    DRIVER_COMPOUND,
                    name,
                    value,
                    weight,
                    param_weight_cap=cap,
                )
        self.ctx.driver_db.mark_driver_folded(uid)
        self._refresh_driver_rating(track_id)
