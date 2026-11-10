from __future__ import annotations

import collections
import dataclasses
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pitwall.audio.dispatcher import Call
from pitwall.engine.context import EngineContext
from pitwall.engine.model import SessionModel
from pitwall.ingest import Ingest
from pitwall.learning.hindsight import grade_and_store
from pitwall.model.pitloss import current_pit_loss, measure, ref_pace_after_ms, ref_pace_ms
from pitwall.net.recording import RecordingReader
from pitwall.protocol.enums import session_kind
from pitwall.setup.advisor import SetupAdvisor
from pitwall.setup.states import majority_state
from pitwall.state.lap import LapSummary
from pitwall.store.db import LapRow, lap_row_from_summary


@dataclasses.dataclass(frozen=True, slots=True)
class _PendingPitLoss:
    session_uid: int
    track_id: int
    in_lap: LapRow
    out_lap: LapRow
    ref_pace_ms: int
    lane_ms: int
    neutralised: int


class SessionWriter:
    def __init__(
        self,
        ctx: EngineContext,
        model: SessionModel,
        ingest: Ingest,
        setup_advisor: SetupAdvisor,
        tick: Callable[[float], list[Call]],
        evaluate_setup: Callable[[int], None],
        store_debrief: Callable[[int], None],
    ) -> None:
        self.ctx = ctx
        self.model = model
        self.ingest = ingest
        self.setup_advisor = setup_advisor
        self.tick = tick
        self._evaluate_live_setup = evaluate_setup
        self._store_debrief_setup = store_debrief
        self.recording_path_source: Any = None  # callable -> Path | None
        self._last_heartbeat: float | None = None
        self.alive_path: Path | None = None  # touched each live loop for the supervisor
        self._laps_written = 0
        self._session_upserted: int | None = None
        self._parc_ferme_written: int | None = None
        self._weekend_structure_written: tuple[int, ...] = ()
        self.session_origin_started_at: float | None = None
        self._session_ended_written = False
        self._pit_in_lap: LapSummary | None = None
        self._pending_pit_loss: _PendingPitLoss | None = None
        self._folded_stints: set[tuple[int, int]] = set()

    def reset(self) -> None:
        self._laps_written = len(self.ctx.state.laps)
        self._session_ended_written = False
        self._pit_in_lap = None
        self._folded_stints.clear()
        self.session_origin_started_at = None
        self._parc_ferme_written = None
        self._weekend_structure_written = ()

    def flush_previous_session(self) -> None:
        pending = self._pending_pit_loss
        if pending is not None:
            if self.ctx.db is None:
                self._pending_pit_loss = None
            else:
                try:
                    laps = self.ctx.db.laps_for(pending.session_uid, 0)
                    self._flush_pending_pit_loss(pending.session_uid, laps, force=True)
                except sqlite3.Error:
                    self._pending_pit_loss = None

    @property
    def tick_period(self) -> float:
        return 1.0 / self.ctx.store.current().engine.tick_hz

    def _upsert_session(self, uid: int) -> None:
        if self.ctx.db is None:
            return
        self._session_upserted = uid
        snap = self.ctx.state.snapshot(self.ctx.clock.now())
        self.ctx.db.upsert_session(
            uid,
            track_id=snap.track_id,
            session_type=snap.session_type,
            started_at=(
                self.session_origin_started_at
                if self.session_origin_started_at is not None
                else time.time()
            ),
            config_hash=self.ctx.store.hash,
            weather=snap.weather,
            game_mode=snap.game_mode,
            weekend_link=snap.weekend_link,
            weekend_structure=snap.weekend_structure,
            calls_mode=(
                "off"
                if self.ctx.store.current().policy.quiet
                or not self.ctx.store.current().speech.enabled
                else "on"
            ),
            parc_ferme=snap.parc_ferme if snap.parc_ferme >= 0 else None,
            synthetic=self.ctx.synthetic_session(uid),
        )
        self._parc_ferme_written = snap.parc_ferme if snap.parc_ferme >= 0 else None
        self._weekend_structure_written = tuple(snap.weekend_structure)

    def _write_laps(self) -> None:
        if self.ctx.state.session_uid is None:
            return
        uid = self.ctx.state.session_uid
        if self.ctx.db is None:
            new_laps = self.ctx.state.laps[self._laps_written :]
            self._laps_written = len(self.ctx.state.laps)
            for lap in new_laps:
                self._on_player_lap(uid, lap)
            return
        if self.ctx.recovering:
            # Rebuilt laps are already committed; don't insert or fold twice.
            self.ctx.state.rival_laps.clear()
            self.ctx.state.setup_changes.clear()
            self._laps_written = len(self.ctx.state.laps)
            return
        parc_ferme = self.ctx.state.parc_ferme_rules
        if parc_ferme >= 0 and parc_ferme != self._parc_ferme_written:
            self.ctx.db.set_session_parc_ferme(uid, parc_ferme)
            self._parc_ferme_written = parc_ferme
        structure = tuple(self.ctx.state.weekend_structure)
        if structure and structure != self._weekend_structure_written:
            self.ctx.db.set_weekend_structure(uid, structure)
            self._weekend_structure_written = structure
        if self.ctx.state.setup_rewind_t is not None:
            self.ctx.db.delete_setup_changes_after(uid, self.ctx.state.setup_rewind_t)
            self.ctx.state.setup_rewind_t = None
        setup_changed = bool(self.ctx.state.setup_changes)
        for change in self.ctx.state.setup_changes:
            to_id = self.ctx.db.setup_state_id(change.to_hash, change.fields)
            from_id = self.ctx.db.setup_state_id(change.from_hash) if change.from_hash else None
            self.ctx.db.insert_setup_change(
                uid,
                change.lap_num,
                change.session_time,
                from_id,
                to_id,
            )
        self.ctx.state.setup_changes.clear()
        for car_idx, lap in self.ctx.state.rival_laps:
            self.ctx.db.insert_lap(uid, car_idx, lap)
        self.ctx.state.rival_laps.clear()
        new_laps = self.ctx.state.laps[self._laps_written :]
        for lap in new_laps:
            self.ctx.db.insert_lap(
                uid,
                0,
                lap,
                setup_state_id=self.ctx.db.setup_state_id(lap.setup_hash)
                if lap.setup_hash
                else None,
            )
        self._laps_written = len(self.ctx.state.laps)
        if new_laps and self.ctx.state.total_laps > 0:
            self.ctx.db.set_session_total_laps(uid, self.ctx.state.total_laps)
        for lap in new_laps:
            self._on_player_lap(uid, lap)
        if new_laps or setup_changed:
            if uid != self.setup_advisor.session_uid:
                self.setup_advisor.reset(uid)
            if new_laps or session_kind(self.ctx.state.session_type) == "race":
                self._evaluate_live_setup(uid)
            else:
                # Garage advice was built on the setup the driver just changed.
                # Drop it until a lap on the new setup exists.
                self.setup_advisor.clear()

    def _on_player_lap(self, uid: int, lap: LapSummary) -> None:
        """M3 persistence hooks (docs/18): refit the stint, measure pit loss
        when an out-lap completes, fold fuel-burn deltas."""
        self.model._note_session_fuel(lap)
        db = self.ctx.db
        settings = self.ctx.store.current()
        track_id = self.ctx.state.track_id
        if db is None:
            rows_by_lap = {
                row.lap_num: row
                for row in (
                    lap_row_from_summary(uid, 0, summary) for summary in self.ctx.state.laps
                )
            }
            rows = sorted(rows_by_lap.values(), key=lambda row: row.lap_num)
            in_memory_tail = self.model._stint_rows(rows)
            if in_memory_tail:
                self.model._fit_current_stint(in_memory_tail, track_id, settings, None)
            return
        th = settings.thresholds
        all_laps = db.laps_for(uid, 0)

        tail = self.model._stint_rows(all_laps)

        if len(tail) < len(all_laps):
            # A stint just closed; fold its fitted params exactly once.
            prev = self.model._stint_rows(all_laps[: len(all_laps) - len(tail)])
            key = (uid, prev[0].lap_num) if prev else (uid, tail[0].lap_num)
            if key not in self._folded_stints:
                self._folded_stints.add(key)
                if prev:
                    fit = self.model._fit_closed_stint(prev, track_id, settings, None)
                    self.model._fold_fit(track_id, prev[0].compound, fit)
                    self.model._weekend_prior_cache.pop((uid, prev[0].compound), None)

        if tail:
            compound = tail[0].compound
            persist_fit = self.model._fit_player_stint(tail, track_id, settings)
            db.upsert_stint(
                uid,
                0,
                compound,
                tail[0].lap_num,
                tail[-1].lap_num,
                persist_fit,
                setup_state_id=majority_state(tail),
            )

        # Pit loss: the lane-entry lap is the in-lap, even when the box is after the line.
        if (
            "after_in_lap" in lap.invalid_reasons
            and self._pit_in_lap is not None
            and self._pit_in_lap.lap_num == lap.lap_num - 1
        ):
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
                    self._pending_pit_loss = _PendingPitLoss(
                        session_uid=uid,
                        track_id=track_id,
                        in_lap=in_row,
                        out_lap=out_row,
                        ref_pace_ms=ref,
                        lane_ms=self.ctx.state.pit_lane_time_ms,
                        neutralised=neutralised,
                    )
        elif "pitted" in lap.invalid_reasons:
            pending = self._pending_pit_loss
            if pending is not None and pending.out_lap.lap_num == lap.lap_num - 1:
                self._pending_pit_loss = None
            elif pending is not None:
                self._flush_pending_pit_loss(uid, all_laps, force=True)
            self._pit_in_lap = lap
        else:
            self._flush_pending_pit_loss(uid, all_laps)

        self.model._fold_lap_fuel(uid, lap, track_id)

        self.ctx.pit_loss_prior = current_pit_loss(
            db,
            uid,
            track_id,
            lap.sc_status,
            settings.track,
            th,
        )

    def _flush_pending_pit_loss(
        self,
        uid: int,
        laps: list[LapRow],
        *,
        force: bool = False,
    ) -> None:
        pending = self._pending_pit_loss
        if pending is None:
            return
        if pending.session_uid != uid:
            self._pending_pit_loss = None
            return
        if self.ctx.db is None:
            self._pending_pit_loss = None
            return
        ref_laps = max(1, int(self.ctx.th("pit_ref_after_laps", 3)))
        after_laps = [
            row
            for row in laps
            if row.lap_num > pending.out_lap.lap_num and row.valid == 1 and row.lap_time_ms > 0
        ]
        if not force and len(after_laps) < ref_laps:
            return
        ref_after = ref_pace_after_ms(laps, pending.out_lap.lap_num, ref_laps) or None
        self._pending_pit_loss = None
        pit = measure(
            pending.in_lap,
            pending.out_lap,
            pending.lane_ms,
            pending.ref_pace_ms,
            pending.neutralised,
            ref_after_ms=ref_after,
        )
        if not (
            self.ctx.th("pit_loss_min_ms", 5_000)
            <= pit.loss_ms
            <= self.ctx.th("pit_loss_max_ms", 60_000)
        ):
            return
        if pending.neutralised == 0 and pit.in_lap_ms - pit.ref_pace_ms > self.ctx.th(
            "pit_inlap_slow_max_ms", 10_000
        ):
            return
        self.ctx.db.insert_pit_event(
            pending.session_uid,
            0,
            pending.in_lap.lap_num,
            pit.loss_ms,
            pending.neutralised,
            pit.lane_ms,
            pit.in_lap_ms,
            pit.out_lap_ms,
            pit.ref_pace_ms,
        )
        suffix = {0: "green", 1: "sc", 2: "vsc"}.get(pending.neutralised, "green")
        if pending.track_id >= 0 and not self.ctx.synthetic_session(pending.session_uid):
            self.ctx.db.fold_param(
                pending.track_id,
                0,
                f"pit_loss_{suffix}_ms",
                float(pit.loss_ms),
                param_weight_cap=self.ctx.th("param_weight_cap", 50),
            )

    def fold_open_stint(self) -> None:
        """Persist the current tail stint when a session closes."""
        if self.ctx.db is None or self.ctx.state.session_uid is None:
            self._pending_pit_loss = None
            return
        self._write_laps()
        uid = self.ctx.state.session_uid
        rows = self.ctx.db.laps_for(uid, 0)
        self._flush_pending_pit_loss(uid, rows, force=True)
        stint = self.model._stint_rows(rows)
        if not stint:
            return
        key = (uid, stint[0].lap_num)
        if key in self._folded_stints:
            return
        self._folded_stints.add(key)
        settings = self.ctx.store.current()
        fit = self.model._fit_closed_stint(stint, self.ctx.state.track_id, settings, None)
        self.ctx.db.upsert_stint(
            uid,
            0,
            stint[0].compound,
            stint[0].lap_num,
            stint[-1].lap_num,
            fit,
            setup_state_id=majority_state(stint),
        )
        self.model._weekend_prior_cache.pop((uid, stint[0].compound), None)
        self.model._fold_fit(self.ctx.state.track_id, stint[0].compound, fit)

    def fold_session_end(self) -> None:
        uid = self.ctx.state.session_uid
        if self.ctx.db is None or uid is None:
            return
        self.fold_open_stint()
        self.model._fold_driver(uid)

    def _end_session(self, uid: int, now: float) -> None:
        assert self.ctx.db is not None
        self.fold_open_stint()
        self.model._fold_driver(uid)
        self._store_debrief_setup(uid)
        self._session_ended_written = True
        self.ctx.db.end_session(uid, now)
        grade_and_store(self.ctx.db, uid, self.ctx.store.current().thresholds)
        self.ctx.db.mark_graded(uid)

    def _heartbeat(self, now: float) -> None:
        period = self.ctx.store.current().engine.heartbeat_s
        if self.ctx.db is None or self.ctx.state.session_uid is None or period <= 0:
            return
        if self._last_heartbeat is not None and now - self._last_heartbeat < period:
            return
        self._last_heartbeat = now
        path = self.recording_path_source() if self.recording_path_source is not None else None
        self.ctx.db.write_heartbeat(
            self.ctx.state.session_uid,
            float(self.ctx.state.last_packet_t or 0.0),
            time.time(),
            str(path or ""),
            self.ctx.state.lap_num,
        )

    def recover(self, wall_now: float | None = None) -> str | None:
        """Mid-session restart: if the SQLite heartbeat is fresh and names a
        recording, replay that recording's tail through ingest (not the
        recorder, rules muted) so EMAs, laps and phase are rebuilt; the model
        reads its lap history from the committed SQLite rows. Returns a short
        description, or None when there is nothing to recover."""
        if self.ctx.db is None:
            return None
        hb = self.ctx.db.read_heartbeat()
        eng = self.ctx.store.current().engine
        wall_now = time.time() if wall_now is None else wall_now
        if hb is None or not hb.recording_path or wall_now - hb.wall_t > eng.recovery_max_age_s:
            return None
        path = Path(hb.recording_path)
        if not path.is_file():
            return None
        tail: collections.deque[tuple[int, bytes]] = collections.deque()
        horizon_us = int(eng.recovery_tail_s * 1e6)
        try:
            with RecordingReader(path) as reader:
                for offset_us, payload in reader:
                    tail.append((offset_us, payload))
                    while tail and offset_us - tail[0][0] > horizon_us:
                        tail.popleft()
        except ValueError:
            pass  # a crash leaves a truncated final record; keep what parsed
        if not tail:
            return None
        recorder, self.ingest.recorder = self.ingest.recorder, None
        self.ctx.recovering = True
        period = self.tick_period
        try:
            next_tick = tail[0][0] / 1e6
            for offset_us, payload in tail:
                t = offset_us / 1e6
                self.ingest.on_datagram(payload, t)
                while t >= next_tick:
                    self.tick(next_tick)
                    next_tick += period
        finally:
            self.ctx.recovering = False
            self.ingest.recorder = recorder
            self.ctx.dispatcher.purge(reason="recovery", now=self.ctx.clock.now())
        if self.ctx.state.session_uid is not None and self.ctx.state.session_uid != hb.session_uid:
            return None
        self._session_upserted = self.ctx.state.session_uid
        self.ctx.dispatcher.log.write(
            {
                "t": self.ctx.clock.now(),
                "outcome": "recovered",
                "rule_id": None,
                "text": f"{path.name} tail {len(tail)} datagrams, lap {self.ctx.state.lap_num}",
            }
        )
        return f"recovered lap {self.ctx.state.lap_num} from {path.name} ({len(tail)} datagrams)"

    def rejoin_text(self) -> str:
        """Spoken after a successful recover()."""
        from pitwall.audio.speaker import REJOIN_OPENER

        st = self.ctx.state
        parts = [REJOIN_OPENER]
        if st.lap_num > 0:
            parts.append(f"Lap {st.lap_num}" + (f", P{st.position}." if st.position else "."))
        if st.total_laps > 0 and st.lap_num > 0:
            parts.append(f"{max(0, st.total_laps - st.lap_num + 1)} to go.")
        return " ".join(parts)
