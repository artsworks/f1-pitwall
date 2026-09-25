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

    @property
    def tick_period(self) -> float:
        return 1.0 / self.store.current().engine.tick_hz

    def tick(self, now: float) -> list[Call]:
        self.store.poll(now)
        snapshot = self.state.snapshot(now)
        if self.state.last_recv_wall is not None:
            self.metrics.note_packet_to_snapshot(self.state.last_recv_wall, now)
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
    decision_log_path: Path | None = None,
    sinks: list[CallSink] | None = None,
    record_to: Path | None = None,
) -> Engine:
    """Assemble a full engine from the layered config."""
    store = ConfigStore(overrides=overrides)
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
    if log_path is None:
        log_path = Path(settings.recording.directory) / "decisions.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    dlog = DecisionLog(log_path, config_hash=store.hash, mindset=settings.mindset.active)
    dispatcher = Dispatcher(
        settings.policy,
        clock,
        decision_log=dlog,
        sinks=sinks or [LogSink()],
        metrics=Metrics(),
        budget_override=mode.get("call_budget_per_lap"),
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
