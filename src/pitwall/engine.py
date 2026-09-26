"""Engine: snapshot -> evaluate -> submit, driven live or by replay.

Replay ticks whenever the record clock crosses a tick boundary, so a 10x or
max-speed replay makes identical decisions to 1x (determinism per docs/07).
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from statistics import median
from typing import Any

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Call, CallSink, Dispatcher, LogSink
from pitwall.clock import Clock, ReplayClock, VirtualClock, WallClock
from pitwall.config.loader import ConfigStore
from pitwall.ingest import Ingest
from pitwall.input.press import Press, PressDetector
from pitwall.metrics import Metrics
from pitwall.model.budget import EnergyBudget, FuelBudget, energy_budget, fuel_budget
from pitwall.model.deg import DegFit, Prior, fit_stint, laps_of_pace, resolve_prior
from pitwall.model.pitloss import current_pit_loss, measure, ref_pace_ms
from pitwall.rules.engine import RuleEngine
from pitwall.state.lap import LapSummary
from pitwall.state.model_view import ModelView
from pitwall.state.session import SessionState
from pitwall.store.db import LapRow


class Engine:
    def __init__(
        self,
        store: ConfigStore,
        clock: Clock,
        ingest: Ingest,
        state: SessionState,
        rule_engine: RuleEngine | None,
        dispatcher: Dispatcher,
    ) -> None:
        self.store = store
        self.clock = clock
        self.ingest = ingest
        self.state = state
        self.rule_engine = rule_engine
        self.dispatcher = dispatcher
        self.metrics = dispatcher.metrics
        self.speaker_name = "null"
        self.recording_desc = "off"
        self._paused = False
        inp = store.current().input
        self.detector = PressDetector(
            double_ms=inp.double_press_ms,
            long_ms=inp.long_press_ms,
            bounce_ms=inp.bounce_ms,
        )
        self._press_queue: list[Press] = []
        state.press_listeners.append(self._on_press_edge)
        state.toggle_listeners.append(lambda t: self._press_queue.append(Press("silent", t)))
        self.db: Any = dispatcher.log.db
        self._laps_written = 0
        self._session_upserted: int | None = None
        self._track_loaded: int | None = None
        self._session_ended_written = False
        # M3 model outputs, exposed on the engine until H2 moves them into
        # the snapshot.
        self.deg_fit: DegFit | None = None
        self.pit_loss_prior: Prior | None = None
        self.fuel_budget: FuelBudget | None = None
        self.energy_budget: EnergyBudget | None = None
        self._pit_in_lap: LapSummary | None = None
        self._folded_stints: set[tuple[int, int]] = set()
        self._fuel_last_kg: float | None = None
        self._stint_fuel_ref = 0.0
        self._stint_laps: list[LapRow] = []
        self._model_key: tuple[int | None, int, int] | None = None
        self._fuel_prior = Prior(0.0, 0.0, "default")
        self._wear_per_lap = 0.0

        def _on_rewind(t: float) -> None:
            dispatcher.purge(reason="flashback", now=t)

        state.rewind_listeners.append(_on_rewind)
        state.session_listeners.append(self._on_new_session)

    @property
    def tick_period(self) -> float:
        return 1.0 / self.store.current().engine.tick_hz

    def _on_press_edge(self, t: float, down: bool) -> None:
        press = self.detector.edge(t, down)
        if press is not None:
            self._press_queue.append(press)

    def client_press(self, down: bool) -> None:
        """WebSocket/spacebar press path (docs/12): feed the same detector."""
        self._on_press_edge(self.clock.now(), down)

    def _on_new_session(self, uid: int) -> None:
        self.dispatcher.reset_session()
        self._laps_written = len(self.state.laps)
        self._session_ended_written = False
        self._pit_in_lap = None
        self._folded_stints.clear()
        self._fuel_last_kg = None
        self.deg_fit = None
        self._upsert_session(uid)

    def _upsert_session(self, uid: int) -> None:
        if self.db is None:
            return
        self._session_upserted = uid
        snap = self.state.snapshot(self.clock.now())
        self.db.upsert_session(
            uid,
            track_id=getattr(snap, "track_id", 0),
            session_type=getattr(snap, "session_type", 0),
            started_at=self.clock.now(),
            game_version=getattr(snap, "game_version", ""),
            config_hash=self.store.hash,
            weather=getattr(snap, "weather", 0),
            game_mode=getattr(snap, "game_mode", 0),
        )

    def _write_laps(self) -> None:
        if self.db is None or self.state.session_uid is None:
            return
        uid = self.state.session_uid
        for car_idx, lap in self.state.rival_laps:
            self.db.insert_lap(uid, car_idx, lap)
        self.state.rival_laps.clear()
        new_laps = self.state.laps[self._laps_written :]
        for lap in new_laps:
            self.db.insert_lap(uid, 0, lap)
        self._laps_written = len(self.state.laps)
        for lap in new_laps:
            self._on_player_lap(uid, lap)

    def _on_player_lap(self, uid: int, lap: LapSummary) -> None:
        """M3 persistence hooks (docs/18): refit the stint, measure pit loss
        when an out-lap completes, fold fuel-burn deltas."""
        db = self.db
        if db is None:
            return
        settings = self.store.current()
        th = settings.thresholds
        track_id = self.state.track_id
        all_laps = db.laps_for(uid, 0)

        # Current stint = contiguous tail with one compound and strictly
        # increasing tyre_age. Whatever precedes the tail is a closed stint.
        tail: list[LapRow] = []
        for row in reversed(all_laps):
            if tail and (
                row.compound != tail[-1].compound or row.tyre_age_laps >= tail[-1].tyre_age_laps
            ):
                break
            tail.append(row)
        tail.reverse()

        if len(tail) < len(all_laps):
            # A stint just closed; fold its fitted params exactly once.
            boundary = tail[0].lap_num
            key = (uid, boundary)
            if key not in self._folded_stints:
                self._folded_stints.add(key)
                prev = self._stint_rows(all_laps[: len(all_laps) - len(tail)])
                if prev:
                    prior = self._deg_prior(track_id, prev[0].compound, settings)
                    fit = fit_stint(
                        prev,
                        prior,
                        min_laps=int(self._th("deg_min_laps", 3)),
                        fuel_coeff_fixed=None,
                        deg_max_ms_per_lap=self._th("deg_max_ms_per_lap", 600),
                        deg_rmse_bad_ms=self._th("deg_rmse_bad_ms", 800),
                    )
                    if fit.source == "fit":
                        for name, value in (
                            ("deg_ms_per_lap", fit.deg_ms_per_lap),
                            ("base_ms", fit.base_ms),
                            ("fuel_ms_per_lap", fit.fuel_ms_per_lap),
                        ):
                            db.fold_param(
                                track_id,
                                prev[0].compound,
                                name,
                                value,
                                weight=float(fit.n),
                                param_weight_cap=self._th("param_weight_cap", 50),
                            )

        if tail:
            self._stint_laps = tail
            self._stint_fuel_ref = max((r.fuel_remaining_laps for r in tail), default=0.0)
            compound = tail[0].compound
            prior = self._deg_prior(track_id, compound, settings)
            fuel_param = db.get_param(track_id, compound, "fuel_ms_per_lap")
            fuel_fixed = (
                fuel_param.value
                if fuel_param is not None and fuel_param.weight >= self._th("prior_min_weight", 2)
                else None
            )
            fit = fit_stint(
                tail,
                prior,
                min_laps=int(self._th("deg_min_laps", 3)),
                fuel_coeff_fixed=fuel_fixed,
                deg_max_ms_per_lap=self._th("deg_max_ms_per_lap", 600),
                deg_rmse_bad_ms=self._th("deg_rmse_bad_ms", 800),
            )
            self.deg_fit = fit
            db.upsert_stint(uid, 0, compound, tail[0].lap_num, tail[-1].lap_num, fit)

        # Pit loss: measure when an out-lap completes against a pending in-lap.
        if "pitted" in lap.invalid_reasons:
            self._pit_in_lap = lap
        elif "after_in_lap" in lap.invalid_reasons and self._pit_in_lap is not None:
            in_lap = self._pit_in_lap
            self._pit_in_lap = None
            skip = {"flashback", "red_flag"}
            reasons = set(in_lap.invalid_reasons) | set(lap.invalid_reasons)
            if not reasons & skip:
                ref = ref_pace_ms(all_laps, in_lap.lap_num)
                if ref > 0:
                    in_row = next(r for r in all_laps if r.lap_num == in_lap.lap_num)
                    out_row = next(r for r in all_laps if r.lap_num == lap.lap_num)
                    neutralised = max(in_row.sc_status, out_row.sc_status)
                    pit = measure(
                        in_row,
                        out_row,
                        self.state.pit_lane_time_ms,
                        ref,
                        neutralised,
                    )
                    db.insert_pit_event(
                        uid,
                        0,
                        in_lap.lap_num,
                        pit.loss_ms,
                        neutralised,
                        pit.lane_ms,
                        pit.in_lap_ms,
                        pit.out_lap_ms,
                        pit.ref_pace_ms,
                    )
                    suffix = {0: "green", 1: "sc", 2: "vsc"}.get(neutralised, "green")
                    db.fold_param(
                        track_id,
                        0,
                        f"pit_loss_{suffix}_ms",
                        float(pit.loss_ms),
                        param_weight_cap=self._th("param_weight_cap", 50),
                    )

        # Fuel burn per lap: fold each consecutive-valid-lap delta.
        if lap.valid and lap.fuel_kg > 0:
            if self._fuel_last_kg is not None:
                delta = self._fuel_last_kg - lap.fuel_kg
                if 0.0 < delta < 10.0:
                    db.fold_param(
                        track_id,
                        0,
                        "fuel_kg_per_lap",
                        delta,
                        param_weight_cap=self._th("param_weight_cap", 50),
                    )
            self._fuel_last_kg = lap.fuel_kg
        elif lap.fuel_kg > 0:
            self._fuel_last_kg = lap.fuel_kg

        self.pit_loss_prior = current_pit_loss(
            db,
            uid,
            track_id,
            lap.sc_status,
            settings.track,
            th,
        )

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
        min_w = self._th("prior_min_weight", 2)
        deg_p = resolve_prior(
            self.db,
            track_id,
            compound,
            "deg_ms_per_lap",
            overlay_value=(overlay.deg_ms_per_lap.get(compound) if overlay is not None else None),
            default=self._th("deg_default_ms_per_lap", 80),
            min_weight=min_w,
        )
        base_p = resolve_prior(
            self.db,
            track_id,
            compound,
            "base_ms",
            overlay_value=(
                float(overlay.base_pace_ms)
                if overlay is not None and overlay.base_pace_ms > 0
                else None
            ),
            default=self._th("release_fallback_lap_s", 95.0) * 1000.0,
            min_weight=min_w,
        )
        fuel_p = resolve_prior(
            self.db,
            track_id,
            compound,
            "fuel_ms_per_lap",
            overlay_value=None,
            default=self._th("fuel_ms_per_lap_default", 30),
            min_weight=min_w,
        )
        return DegFit(
            base_ms=base_p.value,
            deg_ms_per_lap=deg_p.value,
            fuel_ms_per_lap=fuel_p.value,
            n=0,
            rmse_ms=0.0,
            confidence={"learned": 0.5, "overlay": 0.35}.get(deg_p.source, 0.2),
            source=deg_p.source,
        )

    def _th(self, name: str, default: float) -> float:
        v = self.store.current().thresholds.get(name, default)
        return float(v) if isinstance(v, int | float) else default

    def _update_model(self) -> None:
        """Refresh priors (once per lap) and budgets (every tick), then hand
        the whole model view to SessionState for the rules-facing snapshot."""
        state = self.state
        settings = self.store.current()
        mode = settings.resolved_mindset()
        state.set_mode_offsets(
            thermal_warn_offset_c=float(mode.get("thermal_warn_offset_c", 0) or 0)
        )
        track_id = state.track_id
        overlay = settings.track
        key = (state.session_uid, self._laps_written, state.lap_num)
        if key != self._model_key:
            self._model_key = key
            self.pit_loss_prior = current_pit_loss(
                self.db,
                state.session_uid,
                track_id,
                state.safety_car_status,
                overlay,
                settings.thresholds,
            )
            self._fuel_prior = resolve_prior(
                self.db,
                track_id,
                0,
                "fuel_kg_per_lap",
                overlay_value=(
                    overlay.fuel_kg_per_lap
                    if overlay is not None and overlay.fuel_kg_per_lap > 0
                    else None
                ),
                default=self._th("fuel_kg_per_lap_default", 1.7),
                min_weight=self._th("prior_min_weight", 2),
            )
            # Wear rate this stint: median of positive consecutive deltas.
            wear_deltas = [
                b.wear_pct - a.wear_pct
                for a, b in zip(self._stint_laps, self._stint_laps[1:], strict=False)
                if a.wear_pct > 0 and b.wear_pct > a.wear_pct
            ]
            self._wear_per_lap = (
                median(wear_deltas) if wear_deltas else self._th("wear_per_lap_default_pct", 2.5)
            )

        laps_remaining = max(0, state.total_laps - state.lap_num + 1) if state.total_laps > 0 else 0
        self.fuel_budget = fuel_budget(
            laps_remaining=laps_remaining,
            fuel_in_tank_kg=state.fuel_in_tank,
            per_lap_kg=self._fuel_prior.value,
            source=self._fuel_prior.source,
        )
        self.energy_budget = energy_budget(
            store_j=state.ers_store_energy_j,
            store_capacity_j=self._th("ers_store_capacity_j", 4_000_000),
            laps_remaining=laps_remaining,
            deployed_this_lap_j=state.ers_deployed_this_lap_j,
            harvested_this_lap_j=state.ers_harvested_mguk_j + state.ers_harvested_mguh_j,
            harvest_limit_per_lap_j=state.ers_harvest_limit_per_lap_j,
            soc_floor_pct=float(mode.get("ers_soc_floor_pct", 0) or 0),
            over_tolerance_j=self._th("energy_over_tolerance_j", 200_000),
            attack_ok=mode.get("ers_policy") == "attack_rival",
        )

        fit = self.deg_fit
        wear_mean = sum(state.tyres_wear.as_tuple()) / 4.0
        lop = math.inf
        predicted = 0
        if fit is not None:
            lop = laps_of_pace(
                fit,
                state.tyre_age_laps,
                wear_mean,
                cliff_ms=self._th("tyre_cliff_ms", 1500),
                wear_cliff_pct=self._th("wear_cliff_pct", 70),
                wear_per_lap=self._wear_per_lap,
            )
            burned = self._stint_fuel_ref - state.fuel_remaining_laps
            predicted = int(
                fit.base_ms
                + fit.deg_ms_per_lap * (state.tyre_age_laps + 1)
                + fit.fuel_ms_per_lap * (burned + 1)
            )
        eb = self.energy_budget
        fb = self.fuel_budget
        state.set_model(
            ModelView(
                deg_fit_source=fit.source if fit is not None else "",
                deg_ms_per_lap=fit.deg_ms_per_lap if fit is not None else 0.0,
                deg_confidence=fit.confidence if fit is not None else 0.0,
                base_pace_ms=fit.base_ms if fit is not None else 0.0,
                laps_of_pace=lop,
                wear_per_lap_pct=self._wear_per_lap,
                pit_loss_s=(
                    self.pit_loss_prior.value / 1000.0 if self.pit_loss_prior is not None else 0.0
                ),
                pit_loss_source=(
                    self.pit_loss_prior.source if self.pit_loss_prior is not None else ""
                ),
                fuel_margin_laps=fb.margin_laps if fb is not None else 0.0,
                fuel_per_lap_kg=fb.per_lap_kg if fb is not None else 0.0,
                fuel_source=fb.source if fb is not None else "",
                energy_per_lap_mj=eb.per_lap_j / 1e6 if eb is not None else 0.0,
                energy_lap_delta_mj=eb.lap_delta_j / 1e6 if eb is not None else 0.0,
                energy_laps_to_floor=eb.laps_to_floor if eb is not None else math.inf,
                energy_mode=eb.mode if eb is not None else "",
                predicted_lap_ms=predicted,
            )
        )

    def tick(self, now: float) -> list[Call]:
        self.store.poll(now)
        if self.state.track_id != self._track_loaded:
            self._track_loaded = self.state.track_id
            self.store.set_track(self.state.track_id if self.state.track_id >= 0 else None)
        self._write_laps()
        self._update_model()
        snapshot = self.state.snapshot(now)
        if (
            snapshot.session_ended
            and not self._session_ended_written
            and self.db is not None
            and self.state.session_uid is not None
        ):
            self._session_ended_written = True
            self.db.end_session(self.state.session_uid, now)
        press = self.detector.tick(now)
        if press is not None:
            self._press_queue.append(press)
        for p in self._press_queue:
            self.dispatcher.on_press(p, snapshot)
        self._press_queue.clear()
        if self.state.last_recv_wall is not None:
            self.metrics.note_packet_to_snapshot(self.state.last_recv_wall, now)
        if snapshot.paused != self._paused:
            self.dispatcher.log.write(
                {"t": now, "outcome": "paused" if snapshot.paused else "resumed"}
            )
            if snapshot.paused:
                self.dispatcher.purge(reason="paused", now=now)
            self._paused = snapshot.paused
        if snapshot.paused:
            return []
        if (
            self.db is not None
            and self.state.session_uid is not None
            and self._session_upserted != self.state.session_uid
        ):
            self._upsert_session(self.state.session_uid)
        if self.rule_engine is not None:
            result = self.rule_engine.evaluate(snapshot)
            self.dispatcher.submit(result.candidates, snapshot)
        return self.dispatcher.drain(now)

    async def run_live(self) -> None:
        """Tick at tick_hz forever; dispatch drains on its own loop."""
        period = self.tick_period
        while True:
            self.tick(self.clock.now())
            await self.clock.sleep(period)


class _ReplaySink:
    """Wraps ingest; ticks the engine when the record clock crosses a tick
    boundary. recv_time is the record's own timestamp in seconds."""

    def __init__(self, ingest: Ingest, engine: Engine) -> None:
        self.ingest = ingest
        self.engine = engine
        self.next_tick: float | None = None
        self.calls: list[Call] = []

    def on_datagram(self, payload: bytes, recv_time: float) -> None:
        self.ingest.on_datagram(payload, recv_time)
        period = self.engine.tick_period
        if self.next_tick is None:
            self.next_tick = recv_time
        while recv_time >= self.next_tick:
            self.calls.extend(self.engine.tick(self.next_tick))
            self.next_tick += period

    def finish(self) -> None:
        self.calls.extend(self.engine.dispatcher.drain(self.engine.clock.now()))


async def run_replay(
    path: Path,
    engine: Engine,
    speed: float | None = None,
    *,
    from_us: int | None = None,
    to_us: int | None = None,
) -> tuple[int, list[Call]]:
    """Stream a recording through ingest + engine. Returns (datagrams, calls)."""
    from pitwall.net.replay import replay

    sink = _ReplaySink(engine.ingest, engine)
    delivered = await replay(path, sink, engine.clock, speed, from_us=from_us, to_us=to_us)
    sink.finish()
    return delivered, sink.calls


def build_engine(
    *,
    clock: Clock | None = None,
    overrides: dict[str, Any] | None = None,
    rules_dir: Path | None = None,
    decision_log_path: Path | None = None,
    decision_log_fp: Any = None,
    sinks: list[CallSink] | None = None,
    record_to: Path | None = None,
    db: Any = None,
) -> Engine:
    """Assemble a full engine from the layered config. db=None disables
    SQLite mirroring (replays opt in via the CLI)."""
    store = ConfigStore(overrides=overrides, rules_dir=rules_dir)
    settings = store.current()
    clock = clock or WallClock()
    recorder = None
    if record_to is not None:
        from pitwall.net.recording import RecordingRotator

        recorder = RecordingRotator(record_to, config_hash=int(store.hash, 16) % (2**32))
    ingest = Ingest(recorder=recorder)
    state = SessionState(
        ema_fast_s=settings.engine.ema_fast_s,
        ema_slow_s=settings.engine.ema_slow_s,
        straight_hold_s=settings.engine.straight_hold_s,
        press_bit=settings.input.udp_action_bit,
        toggle_bit=settings.input.silent_toggle_bit,
        thresholds=settings.thresholds,
    )
    state.register(ingest)
    mode = store.current().resolved_mindset()
    rule_engine = RuleEngine(
        list(settings.rules),
        thresholds=settings.thresholds,
        mode=mode,
        staleness_s=settings.engine.staleness_s,
    )
    log_path = decision_log_path
    if log_path is None and decision_log_fp is None:
        log_path = Path(settings.recording.directory) / "decisions.jsonl"
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    dlog = DecisionLog(
        log_path,
        fp=decision_log_fp,
        config_hash=store.hash,
        mindset=settings.mindset.active,
        db=db,
        session_uid_source=lambda: state.session_uid,
    )
    dispatcher = Dispatcher(
        settings.policy,
        clock,
        decision_log=dlog,
        sinks=sinks or [LogSink()],
        metrics=Metrics(),
        budget_override=mode.get("call_budget_per_lap"),
        input=settings.input,
    )
    return Engine(store, clock, ingest, state, rule_engine, dispatcher)


def build_census_engine(clock: Clock | None = None) -> Engine:
    """Ingest-only engine for --no-rules census replays."""
    store = ConfigStore()
    clock = clock or VirtualClock()
    ingest = Ingest()
    state = SessionState()
    dlog = DecisionLog()
    dispatcher = Dispatcher(store.current().policy, clock, decision_log=dlog, sinks=[])
    return Engine(store, clock, ingest, state, None, dispatcher)


def record_and_run(path: Path, speed: float | None) -> tuple[int, list[Call]]:
    engine = build_engine(clock=VirtualClock() if speed is None else ReplayClock(speed))
    return asyncio.run(run_replay(path, engine, speed))
