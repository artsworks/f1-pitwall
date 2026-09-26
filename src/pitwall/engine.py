"""Engine: snapshot -> evaluate -> submit, driven live or by replay.

Replay ticks whenever the record clock crosses a tick boundary, so a 10x or
max-speed replay makes identical decisions to 1x (determinism per docs/07).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Call, CallSink, Dispatcher, LogSink
from pitwall.clock import Clock, ReplayClock, VirtualClock, WallClock
from pitwall.config.loader import ConfigStore
from pitwall.ingest import Ingest
from pitwall.input.press import Press, PressDetector
from pitwall.metrics import Metrics
from pitwall.rules.engine import RuleEngine
from pitwall.state.session import SessionState


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
        )

    def _write_laps(self) -> None:
        if self.db is None or self.state.session_uid is None:
            return
        for lap in self.state.laps[self._laps_written :]:
            self.db.insert_lap(self.state.session_uid, 0, lap)
        self._laps_written = len(self.state.laps)

    def tick(self, now: float) -> list[Call]:
        self.store.poll(now)
        snapshot = self.state.snapshot(now)
        self._write_laps()
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
