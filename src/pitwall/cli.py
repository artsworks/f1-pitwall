"""pitwall CLI: record, replay, trim, index, stats, doctor."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pitwall.server.hub import Hub

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Call, Dispatcher
from pitwall.clock import Clock, ReplayClock, VirtualClock, WallClock
from pitwall.config.loader import ConfigStore
from pitwall.engine import Engine, action_bits, build_census_engine, build_engine, run_replay
from pitwall.ingest import Ingest
from pitwall.input.menu import validate as validate_menu
from pitwall.input.menu import validate_shortcuts
from pitwall.net.profile import PROFILES, RecordFilter
from pitwall.net.recording import (
    RecordingReader,
    RecordingRotator,
    RecordingWriter,
    build_index,
    compress_recording,
    index_path_for,
    read_index,
    write_index,
)
from pitwall.net.udp import listen
from pitwall.protocol.header import PacketId
from pitwall.rules.engine import RuleEngine
from pitwall.state.session import SessionState
from pitwall.supervisor import ALIVE_FILE, Supervisor, read_recording_pointer, runtime_dir


def _parse_speed(value: str) -> float | None:
    if value == "max":
        return None
    v = float(value)
    if v <= 0:
        raise argparse.ArgumentTypeError("speed must be positive or 'max'")
    return v


def cmd_record(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    recorder = RecordingRotator(out_dir, profile=args.profile)
    ingest = Ingest(recorder=recorder)
    clock = WallClock()

    async def run() -> None:
        transport = await listen(args.host, args.port, ingest, clock)
        print(f"recording on {args.host}:{args.port} -> {out_dir} (ctrl-c to stop)")
        try:
            await asyncio.Event().wait()
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            transport.close()
            recorder.close()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        recorder.close()
    if recorder.last_path is not None:
        print(f"last recording: {recorder.last_path}")
    return 0


def _lap_seek_us(path: Path, lap: int) -> int | None:
    for entry in read_index(path):
        if entry["kind"] == "lap" and entry["detail"] == str(lap):
            return int(entry["offset_us"])
    return None


def cmd_replay(args: argparse.Namespace) -> int:
    from pitwall.net.mask import mask_restricted
    from pitwall.store.db import Database

    speed = _parse_speed(args.speed)
    from_us = args.from_us
    if args.from_lap is not None:
        from_us = _lap_seek_us(Path(args.file), args.from_lap)
        if from_us is None:
            print(f"replay: lap {args.from_lap} not found in index of {args.file}")
            return 1
    clock = VirtualClock() if speed is None else ReplayClock(speed, start=(from_us or 0) / 1e6)
    if args.serve:
        from pitwall.audio.dispatcher import LogSink
        from pitwall.server.hub import Hub
        from pitwall.server.review import ReviewController

        hub = Hub()
        seed_db = Database(args.seed_db)

        class _ReplaySpokenSink:
            """No speaker in replay: mark calls spoken in the hub immediately."""

            speaks_audio = False

            def speak(self, call: Call) -> None:
                hub.spoken(call.id, call.t)

            def cancel(self, call_id: str) -> None:
                pass

        def _engine_factory(clk: Clock) -> Engine:
            eng = build_engine(
                clock=clk,
                sinks=[hub, LogSink(), _ReplaySpokenSink()],
                db=seed_db,
            )
            if args.mask_restricted:
                eng.ingest.transform = mask_restricted
            return eng

        live_speed = speed or 1.0  # paced replay drives the dashboard
        review = ReviewController(
            Path(args.file),
            _engine_factory,
            hub,
            speed=live_speed,
            db=seed_db,
            thresholds=ConfigStore().current().thresholds,
        )
        engine = _engine_factory(clock)
        review.engine = engine
        review.clock = None  # controller builds its own PausableClock on play

        async def _replay_coro() -> None:
            await review.play()
            # A seek cancels the running task and starts a new one: follow it.
            while (task := review._task) is not None:  # noqa: SLF001
                try:
                    await task
                except asyncio.CancelledError:
                    if review._task is task:  # noqa: SLF001
                        raise
                    continue
                if review._task is task:  # noqa: SLF001
                    break

        store = ConfigStore()
        asyncio.run(_serve(engine, hub, store, _replay_coro(), review=review))
        return 0
    db: Database | None = Database(args.seed_db) if args.seed_db else None
    engine = build_census_engine(clock) if args.no_rules else build_engine(clock=clock, db=db)
    if args.mask_restricted:
        engine.ingest.transform = mask_restricted
    replay_coro = run_replay(Path(args.file), engine, speed, from_us=from_us, to_us=args.to_us)
    delivered, calls = asyncio.run(replay_coro)
    print(f"replayed {delivered} datagrams from {args.file}; {len(calls)} calls")
    if args.stats:
        print(json.dumps(engine.ingest.census(now=engine.clock.now()), indent=2))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    import glob as _glob

    from pitwall.diff import format_diff, run_diff

    recordings = [Path(f) for f in args.recordings]
    if args.corpus:
        recordings += [Path(p) for p in sorted(_glob.glob(args.corpus))]
    if not recordings:
        print("diff: no recordings matched")
        return 0
    a_dir = Path(args.a) if args.a else None
    result = run_diff(
        recordings,
        a_dir,
        Path(args.b),
        a_mindset=args.a_mindset,
        b_mindset=args.b_mindset,
    )
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(format_diff(result))
    if args.record:
        from pitwall.store.db import open_configured
        from pitwall.tune import record_diff

        db = open_configured(ConfigStore().current())
        if db is not None:
            n = record_diff(
                db,
                result,
                a_dir=str(args.a or ""),
                b_dir=str(args.b),
                a_mindset=str(args.a_mindset or ""),
                b_mindset=str(args.b_mindset or ""),
            )
            print(f"diff: recorded {n} A/B rows")
    return 0


def cmd_tune(args: argparse.Namespace) -> int:
    """Fold graded calls + A/B results from SQLite into persisted rule tuning."""
    from pitwall.store.db import Database, open_configured
    from pitwall.tune import format_tune, tune_from_db

    settings = ConfigStore().current()
    db = Database(args.db) if args.db else open_configured(settings)
    if db is None:
        print("tune: persistence disabled")
        return 1
    if args.paths:
        from pitwall.ingest import ingest_recordings

        results = ingest_recordings(db, args.paths, settings, calls_mode=args.calls_mode)
        for result in results:
            print(
                f"{result.session_uid} {result.status} {result.path}"
                + (f": {result.error}" if result.error else "")
            )
    print(format_tune(tune_from_db(db, settings.thresholds)))
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    """Grade a session in hindsight and write its compact JSON digest."""
    from pitwall.digest import build_digest, format_digest
    from pitwall.store.db import Database, open_configured

    settings = ConfigStore().current()
    db = Database(args.db) if args.db else open_configured(settings)
    if db is None:
        print("digest: persistence disabled")
        return 1
    if args.paths:
        from pitwall.ingest import ingest_recordings

        out_dir = None if args.out in (None, "-") else Path(args.out)
        results = ingest_recordings(
            db,
            args.paths,
            settings,
            calls_mode=args.calls_mode,
            out_dir=out_dir,
        )
        for result in results:
            print(f"session {result.session_uid}: {result.status} ({result.path})")
            for finding in result.findings:
                print(f"  - {finding}")
            if result.error:
                print(f"  error: {result.error}")
        return 1 if any(result.status == "error" for result in results) else 0
    uid = db.latest_session_uid() if args.session in (None, "latest") else int(args.session)
    if uid is None:
        print("digest: no sessions in the database")
        return 1
    digest = build_digest(db, uid, settings.thresholds)
    if args.json:
        print(json.dumps(digest, indent=2, default=str))
    else:
        print(format_digest(digest))
    if args.out != "-":
        default = Path(settings.persistence.path).expanduser().parent / "digests"
        out_dir = Path(args.out) if args.out else default
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{uid}.json"
        path.write_text(json.dumps(digest, indent=2, default=str))
        print(f"digest: {path}")
    return 0


def cmd_debrief(args: argparse.Namespace) -> int:
    from pitwall.debrief import render_debrief
    from pitwall.store.db import Database, open_configured

    settings = ConfigStore().current()
    db = Database(args.db) if args.db else open_configured(settings)
    if db is None:
        print("debrief: persistence disabled")
        return 1
    uid = db.latest_session_uid() if args.session == "latest" else int(args.session)
    if uid is None or db.session_row(uid) is None:
        print("debrief: session not found")
        return 1
    path = Path(args.out) if args.out else Path(f"debrief-{uid}.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_debrief(db, uid, settings))
    print(f"debrief: {path}")
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    from pitwall.evaluate import evaluate_corpus
    from pitwall.store.db import Database, open_configured

    db = Database(args.db) if args.db else open_configured(ConfigStore().current())
    if db is None:
        print("evaluate: persistence disabled")
        return 1
    result = evaluate_corpus(db, args.track)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for track in result["tracks"]:
            mode_status = "comparable" if track["comparable"] else "one mode only"
            print(f"Track {track['track_id']}: {mode_status}")
            for mode, row in (("on", track["on"]), ("off", track["off"])):
                print(
                    f"  calls {mode}: {row['sessions']} sessions, {row['completed_laps']} laps, "
                    f"clean pace p25/p50/p75={row['lap_time_s']['p25']}/"
                    f"{row['lap_time_s']['p50']}/{row['lap_time_s']['p75']} s; "
                    f"invalid laps={row['invalid_laps']} ({row['mistake_rate']}); "
                    f"negative call grades={row['negative_call_grades']}"
                )
        print(result["note"])
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    import yaml

    from pitwall.propose import propose_thresholds
    from pitwall.store.db import Database, open_configured

    settings = ConfigStore().current()
    db = Database(args.db) if args.db else open_configured(settings)
    if db is None:
        print("propose: persistence disabled")
        return 1
    result = propose_thresholds(db, settings, args.candidate_rules)
    output = yaml.safe_dump(result, sort_keys=False)
    if args.out:
        Path(args.out).write_text(output)
        print(f"propose: review {args.out} before editing YAML")
    else:
        print(output)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Bundle a recording, its index, decision log, config and DB rows into a
    zip for a bug report."""
    import platform
    import zipfile

    import yaml as _yaml

    from pitwall.store.db import open_configured

    store = ConfigStore()
    settings = store.current()
    rec_dir = Path(settings.recording.directory)
    if args.recording:
        rec_path = Path(args.recording)
    else:
        candidates = sorted(rec_dir.glob("*.f1bin"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            print(f"report: no .f1bin recordings in {rec_dir}")
            return 1
        rec_path = candidates[0]
    out = Path(args.out) if args.out else Path(f"pitwall-report-{int(time.time())}.zip")

    db = open_configured(settings)
    uid = db.latest_session_uid() if db is not None else None
    system = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "pitwall": _pitwall_version(),
        "config_hash": store.hash,
        "session_uid": uid,
    }
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(rec_path, arcname=rec_path.name)
        idx = index_path_for(rec_path)
        if idx.exists():
            z.write(idx, arcname=idx.name)
        dlog_path = rec_dir / f"{settings.mindset.active}.decisions.jsonl"
        if dlog_path.exists():
            z.write(dlog_path, arcname=dlog_path.name)
        z.writestr(
            "config.yaml",
            _yaml.safe_dump(settings.model_dump(mode="json"), sort_keys=True),
        )
        if db is not None and uid is not None:
            z.writestr("calls.json", json.dumps(db.calls_for_session(uid), default=str))
            z.writestr("grades.json", json.dumps(db.grades_for_session(uid), default=str))
            z.writestr(
                "bookmarks.json",
                json.dumps(db.bookmarks_for_session(uid), default=str),
            )
        z.writestr("system.json", json.dumps(system, indent=2))
    print(f"report bundle: {out}")
    return 0


def _pitwall_version() -> str:
    try:
        from importlib.metadata import version

        return version("pitwall")
    except Exception:
        return "dev"


def cmd_rules_check(args: argparse.Namespace) -> int:
    """Validate all rule expressions compile and evaluate on a dummy snapshot."""
    store = ConfigStore()
    settings = store.current()
    engine = RuleEngine(
        list(settings.rules),
        thresholds=settings.thresholds,
        mode=store.current().resolved_mindset(),
        staleness_s=settings.engine.staleness_s,
    )
    menu_errors = validate_menu(settings.menu) + validate_shortcuts(settings.input, settings.menu)
    for err in menu_errors:
        print(f"rules check FAILED: {err}")
    if menu_errors:
        return 1
    snap = SessionState().snapshot(0.0)
    try:
        result = engine.evaluate(snap)
    except Exception as e:
        print(f"rules check FAILED: {e}")
        return 1
    print(
        f"rules check: {len(engine.rules)} rules compiled, "
        f"{len(result.candidates)} candidates on empty snapshot"
    )
    return 0


def cmd_trim(args: argparse.Namespace) -> int:
    src = Path(args.file)
    from_us = args.from_us if args.from_us is not None else 0
    to_us = args.to_us
    keep = RecordFilter(args.profile) if args.profile else None
    with (
        RecordingReader(src) as reader,
        RecordingWriter(
            Path(args.out),
            packet_format=reader.header.packet_format,
            session_uid=reader.header.session_uid,
            config_hash=reader.header.config_hash,
            game_version=reader.header.game_version,
            metadata={
                **reader.header.metadata,
                "trimmed_from": str(src),
                **({"profile": args.profile} if args.profile else {}),
            },
        ) as writer,
    ):
        kept = 0
        for offset_us, payload in reader:
            if offset_us < from_us:
                continue
            if to_us is not None and offset_us > to_us:
                break
            if keep is not None and not keep.keep(offset_us / 1_000_000, payload):
                continue
            # Re-base so the trimmed file starts at 0.
            writer.write_datagram((offset_us - from_us) / 1_000_000, payload)
            kept += 1
    print(f"wrote {kept} records -> {args.out}")
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    path = Path(args.file)
    entries = build_index(path)
    out = index_path_for(path)
    write_index(out, entries)
    print(f"wrote {len(entries)} index entries -> {out}")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    from pitwall.calibrate import calibrate, format_calibration, write_overlays
    from pitwall.store.db import Database, open_configured

    settings = ConfigStore().current()
    db = Database(args.db) if args.db else open_configured(settings)
    if db is None:
        print("calibrate: persistence disabled")
        return 1
    errors = False
    if args.paths:
        from pitwall.ingest import ingest_recordings

        results = ingest_recordings(db, args.paths, settings)
        for result in results:
            if result.status == "error":
                errors = True
                print(f"ingest error {result.path}: {result.error}")
    report = calibrate(db, settings, track_id=args.track, dry_run=args.dry_run)
    if args.write_overlay and not args.dry_run:
        report["overlays"] = [
            str(path) for path in write_overlays(report, settings, args.overlay_dir)
        ]
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_calibration(report))
        for path in report.get("overlays", []):
            print(f"overlay: {path}")
    return 1 if errors else 0


def cmd_stats(args: argparse.Namespace) -> int:
    if args.learned:
        from pitwall.store.db import Database, open_configured

        settings = ConfigStore().current()
        db = Database(args.db) if args.db else open_configured(settings)
        if db is None:
            print("stats --learned: persistence disabled")
            return 1
        from pitwall.learned import format_learned, learned_state

        state = learned_state(db, settings, track_id=args.track)
        print(json.dumps(state, indent=2, default=str) if args.json else format_learned(state))
        return 0
    if not args.file:
        print("stats: provide a recording or use --learned")
        return 2
    ingest = Ingest()
    last_t = 0.0
    with RecordingReader(Path(args.file)) as reader:
        header = reader.header
        for offset_us, payload in reader:
            last_t = offset_us / 1_000_000
            ingest.on_datagram(payload, last_t)
    out = {
        "file": args.file,
        "session_uid": header.session_uid,
        "packet_format": header.packet_format,
        "config_hash": header.config_hash,
        "metadata": header.metadata,
        **ingest.census(now=last_t),
    }
    print(json.dumps(out, indent=2))
    return 0


def _lan_ip() -> str:
    import socket as _socket

    try:
        s_ = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s_.connect(("8.8.8.8", 80))
        ip = str(s_.getsockname()[0])
        s_.close()
        return ip
    except OSError:
        return "127.0.0.1"


def _quiet_left_s(engine: Engine, now: float) -> float | None:
    until = engine.dispatcher.quiet_until
    return until - now if until is not None and now < until else None


async def _state_broadcast(
    base: Engine, hub: Hub, store: ConfigStore, active: Callable[[], Engine]
) -> None:
    from pitwall.server.app import state_payload

    period = 1.0 / store.current().ui.state_hz
    last_status = base.clock.now()
    while True:
        engine = active()
        now = engine.clock.now()
        if now - last_status >= 10.0:
            last_status = now
            c = engine.ingest.census(now=now)
            accepted = sum(p["accepted"] for p in c["packets"].values())
            rate = c["packets"].get(str(int(PacketId.CAR_TELEMETRY)), {}).get("rate_hz", 0.0)
            print(
                f"udp: {c['raw_datagrams']} datagrams, {accepted} accepted, "
                f"unsupported={c['dropped_unsupported']} malformed={c['dropped_malformed']} "
                f"size_mismatch={sum(p['dropped_size_mismatch'] for p in c['packets'].values())} "
                f"telemetry {rate:.0f} Hz",
                flush=True,
            )
        snap = engine.state.snapshot(engine.clock.now())
        payload = state_payload(
            snap,
            settings=store.current(),
            metrics=engine.metrics,
            quiet=store.current().policy.quiet,
            quiet_left_s=_quiet_left_s(engine, now),
            silent=engine.dispatcher.silent,
            mindset=engine.mindset,
            page=engine.page,
            menu=engine.menu_payload(now),
        )
        hub.broadcast("state", payload)
        if snap.last_packet_t is not None:
            engine.metrics.note_packet_to_ws(snap.last_packet_t, engine.clock.now())
        await base.clock.sleep(period)


def _handle_https_disconnect(loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
    message = context.get("message")
    if (
        isinstance(context.get("exception"), ConnectionResetError)
        and isinstance(message, str)
        and message.startswith(
            "Exception in callback _ProactorBasePipeTransport._call_connection_lost("
        )
    ):
        return
    loop.default_exception_handler(context)


async def _serve(
    engine: Engine,
    hub: Hub,
    store: ConfigStore,
    coro: Coroutine[Any, Any, Any],
    review: Any = None,
) -> None:
    import uvicorn

    from pitwall.server.app import create_app

    settings = store.current()
    if sys.platform == "win32" and settings.connection.https_cert and settings.connection.https_key:
        asyncio.get_running_loop().set_exception_handler(_handle_https_disconnect)

    def active() -> Engine:
        """Review mode rebuilds the engine on play/seek; follow the current one."""
        if review is not None and review.engine is not None:
            current: Engine = review.engine
            return current
        return engine

    def _latest() -> Any:
        e = active()
        return e.dispatcher.latest_snapshot or e.state.snapshot(e.clock.now())

    app = create_app(
        hub,
        store,
        engine.metrics,
        speaker_name=getattr(engine, "speaker_name", "null"),
        latest_snapshot=_latest,
        on_client_press=lambda down: active().client_press(down),
        on_client_message=lambda msg: active().client_message(msg),
        review=review,
        db=engine.db,
    )

    def _health() -> dict[str, Any]:
        from pitwall.server.app import packet_age_ms

        e = active()
        snap = e.state.snapshot(e.clock.now())
        age = packet_age_ms(snap)
        return {"packet_age_ms": age, "live": age is not None and age < 1000.0}

    hub.health_source = _health
    hub.attach_loop(asyncio.get_running_loop())
    config = uvicorn.Config(
        app,
        host=settings.connection.http_host,
        port=settings.connection.http_port,
        log_level="warning",
        timeout_graceful_shutdown=settings.connection.shutdown_timeout_s,
        ssl_certfile=str(Path(settings.connection.https_cert).expanduser())
        if settings.connection.https_cert and settings.connection.https_key
        else None,
        ssl_keyfile=str(Path(settings.connection.https_key).expanduser())
        if settings.connection.https_cert and settings.connection.https_key
        else None,
    )
    server = uvicorn.Server(config)
    host, port = settings.connection.http_host, settings.connection.http_port
    scheme = "https" if settings.connection.https_cert and settings.connection.https_key else "http"
    print(f"dashboard: {scheme}://{host}:{port}  (LAN: {scheme}://{_lan_ip()}:{port})")
    print(f"speech: {getattr(engine, 'speaker_name', 'null')}")
    print(f"recording: {getattr(engine, 'recording_desc', 'off')}")
    await asyncio.gather(server.serve(), _state_broadcast(engine, hub, store, active), coro)


def cmd_start(args: argparse.Namespace) -> int:
    from pitwall.audio.speaker import make_speaker
    from pitwall.doctor import set_below_normal_priority
    from pitwall.net.recording import RecordingRotator
    from pitwall.net.udp import listen as udp_listen
    from pitwall.server.hub import Hub

    set_below_normal_priority()
    store = ConfigStore()
    settings = store.current()
    child = bool(args.child)
    if settings.engine.watchdog and not child and not args.no_watchdog:
        return _start_supervised(args, store)
    hub = Hub()
    clock = WallClock()
    rt_dir = runtime_dir(Path(settings.recording.directory))

    profile = args.record or settings.recording.profile
    recorder = None
    if settings.recording.enabled and profile != "off" and not child:
        recorder = RecordingRotator(
            Path(settings.recording.directory),
            config_hash=int(store.hash, 16) % (2**32),
            metadata={
                "config_hash": store.hash,
                "send_rate_hz": settings.connection.send_rate_hz,
                "calls_mode": (
                    "off" if settings.policy.quiet or not settings.speech.enabled else "on"
                ),
            },
            profile=profile,
            compress=settings.recording.compress_on_close,
        )
    ingest = Ingest(recorder=recorder)
    state = SessionState(
        ema_fast_s=settings.engine.ema_fast_s,
        ema_slow_s=settings.engine.ema_slow_s,
        straight_hold_s=settings.engine.straight_hold_s,
        press_bit=settings.input.udp_action_bit,
        toggle_bit=settings.input.silent_toggle_bit,
        action_bits=action_bits(settings.input),
        thresholds=settings.thresholds,
    )
    state.register(ingest)
    speaker = make_speaker(settings.speech)
    mode = store.current().resolved_mindset()
    rule_engine = RuleEngine(
        list(settings.rules),
        thresholds=settings.thresholds,
        mode=mode,
        staleness_s=settings.engine.staleness_s,
    )
    rec_dir = Path(settings.recording.directory)
    rec_dir.mkdir(parents=True, exist_ok=True)
    db = None
    if settings.persistence.enabled:
        from pitwall.store.db import open_configured

        db = open_configured(settings)
    if db is not None:
        from pitwall.maintenance import maintain

        try:
            print(f"learning: {maintain(db, settings.thresholds).summary()}", flush=True)
        except sqlite3.Error as e:
            print(f"learning: upkeep skipped ({e})", flush=True)
    dlog = DecisionLog(
        rec_dir / f"{settings.mindset.active}.decisions.jsonl",
        config_hash=store.hash,
        mindset=settings.mindset.active,
        db=db,
        session_uid_source=lambda: state.session_uid,
    )
    dispatcher = Dispatcher(
        settings.policy,
        clock,
        decision_log=dlog,
        sinks=[hub, speaker],
        input=settings.input,
    )
    dispatcher.on_press_event = lambda payload: hub.broadcast("press", payload)
    speaker.on_spoken = lambda cid, t: hub.spoken(cid, t)
    engine = Engine(store, clock, ingest, state, rule_engine, dispatcher)
    if recorder is not None:
        engine.recording_path_source = lambda: recorder.current_path or recorder.last_path
    elif child:
        engine.recording_path_source = lambda: read_recording_pointer(rt_dir)
        engine.alive_path = rt_dir / ALIVE_FILE
    recovered = engine.recover()
    if recovered is not None:
        print(f"watchdog: {recovered}; radio: {engine.rejoin_text()}", flush=True)
    udp_host, udp_port = settings.connection.udp_host, settings.connection.udp_port
    if child:
        udp_host, udp_port = "127.0.0.1", settings.engine.engine_port

    async def live() -> None:
        transport = await udp_listen(udp_host, udp_port, ingest, clock)
        try:
            await engine.run_live()
        finally:
            transport.close()

    async def shutdown() -> None:
        pass

    engine.speaker_name = speaker.name
    engine.recording_desc = (
        f"{profile} -> {settings.recording.directory}/"
        if recorder is not None
        else "supervisor"
        if child and settings.recording.enabled
        else "off"
    )
    if settings.speech.enabled:
        t0 = clock.now()
        lines = (
            (("rejoin", engine.rejoin_text()),)
            if recovered is not None
            else (
                ("startup", "Pit wall online."),
                ("startup-radio-check", "Radio check, radio check."),
            )
        )
        for call_id, text in lines:
            speaker.speak(
                Call(
                    id=call_id,
                    rule_id="startup",
                    priority=3,
                    text=text,
                    tags=[],
                    deadline_ms=5000,
                    lap=0,
                    t=t0,
                    trigger_t=t0,
                )
            )
    clean = False
    try:
        asyncio.run(_serve(engine, hub, store, live()))
    except KeyboardInterrupt:
        clean = True
    finally:
        if clean and db is not None:
            db.clear_heartbeat()  # a later start is a fresh session, not a crash
        speaker.close()
        if recorder is not None:
            if settings.recording.compress_on_close:
                print("recording: compressing…", flush=True)
            recorder.close()
            if recorder.last_path is not None:
                print(f"recording: saved {recorder.last_path}")
        dlog.close()
    return 0


def _start_supervised(args: argparse.Namespace, store: ConfigStore) -> int:
    """Recorder + watchdog in this process; the engine runs as a restartable child."""
    settings = store.current()
    profile = args.record or settings.recording.profile
    rec_dir = Path(settings.recording.directory)
    recorder = None
    if settings.recording.enabled and profile != "off":
        recorder = RecordingRotator(
            rec_dir,
            config_hash=int(store.hash, 16) % (2**32),
            metadata={
                "config_hash": store.hash,
                "send_rate_hz": settings.connection.send_rate_hz,
                "calls_mode": (
                    "off" if settings.policy.quiet or not settings.speech.enabled else "on"
                ),
            },
            profile=profile,
            compress=settings.recording.compress_on_close,
        )
    cmd = [sys.executable, "-m", "pitwall", "start", "--child"]
    if args.record:
        cmd += ["--record", args.record]
    host, port = settings.connection.udp_host, settings.connection.udp_port
    sup = Supervisor(
        settings.engine,
        recorder,
        cmd,
        runtime_dir(rec_dir),
        udp_host=host,
        udp_port=port,
        log=lambda m: print(m, flush=True),
    )
    print(
        f"watchdog: recorder on {host}:{port}, engine on 127.0.0.1:{settings.engine.engine_port}",
        flush=True,
    )
    sup.run()
    if recorder is not None and recorder.last_path is not None:
        print(f"recording: saved {recorder.last_path}")
    return 0


def cmd_speak(args: argparse.Namespace) -> int:
    """Diagnose the audio path: speak TEXT and report when it was spoken."""
    from pitwall.audio.piper_tts import PiperSpeaker, make_piper_synth
    from pitwall.audio.speaker import make_speaker

    store = ConfigStore()
    update: dict[str, object] = {"engine": args.engine}
    if args.voice:
        update["piper_voice"] = args.voice
    if args.speed:
        update["piper_speed"] = args.speed
    speech = store.current().speech.model_copy(update=update)
    if args.save:
        t0 = time.monotonic()
        try:
            wav, seconds = make_piper_synth(speech)(args.text)
        except FileNotFoundError as exc:
            print(exc)
            return 1
        Path(args.save).write_bytes(wav)
        print(
            f"saved {args.save}: {seconds:.1f} s of audio, "
            f"rendered in {(time.monotonic() - t0) * 1000:.0f} ms (incl. voice load)"
        )
        return 0
    speaker = make_speaker(speech)
    print(
        f"speaker: {speaker.name} (engine={args.engine} rate={speech.rate} volume={speech.volume})"
    )
    done = threading.Event()
    spoken_at: list[float] = []

    def _on_spoken(cid: str, t: float) -> None:
        spoken_at.append(time.monotonic())
        done.set()

    speaker.on_spoken = _on_spoken
    t0 = time.monotonic()
    speaker.speak(
        Call(
            id="speak",
            rule_id="speak",
            priority=3,
            text=args.text,
            tags=[],
            deadline_ms=10000,
            lap=0,
            t=t0,
            trigger_t=t0,
        )
    )
    if done.wait(timeout=10.0):
        print(f"spoken after {(spoken_at[0] - t0) * 1000:.0f} ms")
        if isinstance(speaker, PiperSpeaker):
            time.sleep(max(0.0, speaker.busy_until - time.monotonic()))
    else:
        print("TIMEOUT: nothing spoken in 10 s")
    speaker.close()
    return 0


def cmd_voices(args: argparse.Namespace) -> int:
    from pitwall.audio.piper_tts import (
        SUGGESTED_VOICES,
        common_phrases,
        download_voice,
        installed_voices,
        make_piper_speaker,
    )

    speech = ConfigStore().current().speech
    if args.action == "get":
        for name in args.names or [speech.piper_voice]:
            path = download_voice(speech, name)
            print(f"downloaded {name} -> {path}")
        return 0
    if args.action == "warm":
        speaker = make_piper_speaker(speech)
        try:
            count = speaker.warm(common_phrases())
            print(f"cached {count} phrases for {speech.piper_voice}")
        finally:
            speaker.close()
        return 0
    have = installed_voices(speech)
    print(f"voices in {speech.voices_dir}/ (configured: {speech.piper_voice}):")
    for name in have:
        print(f"  {'*' if name == speech.piper_voice else ' '} {name}")
    if not have:
        print("  (none) - run: pitwall voices get")
    print("suggested: " + ", ".join(SUGGESTED_VOICES))
    print("all voices: https://rhasspy.github.io/piper-samples/")
    return 0


def cmd_voice(args: argparse.Namespace) -> int:
    """Voice channel tooling (docs/21): grammar, devices, spike."""
    from pitwall.voice.grammar import VoiceGrammar

    settings = ConfigStore().current()
    voice = settings.voice
    if args.action == "grammar":
        print(VoiceGrammar.from_mapping(voice.intents).to_srgs(args.lang), end="")
        return 0
    if sys.platform != "win32":
        print("voice devices/spike need Windows SAPI (pywin32)")
        return 1
    if args.action == "devices":
        from pitwall.voice.sapi import list_inputs

        recs, ins = list_inputs()
        print("recognisers (voice.recognizer matches a substring):")
        for r in recs:
            print(f"  {r}")
        print("audio inputs (voice.device):")
        for i, name in enumerate(ins):
            print(f"  {i}: {name}")
        return 0
    from pitwall.voice.spike import run_spike

    update: dict[str, object] = {}
    if args.device is not None:
        update["device"] = args.device
    if args.recognizer is not None:
        update["recognizer"] = args.recognizer
    if args.confidence is not None:
        update["confidence_min"] = args.confidence
    if args.affinity is not None:
        update["affinity_mask"] = int(args.affinity, 0)
    voice = voice.model_copy(update=update)
    log = Path(args.log or f"recordings/voice-spike-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    return run_spike(
        voice,
        settings.input,
        host=settings.connection.udp_host,
        port=None if args.no_udp else (args.port or settings.connection.udp_port),
        log_path=log,
        grammar_mode=args.grammar,
        say=args.say,
    )


def cmd_doctor(args: argparse.Namespace) -> int:
    from pitwall.doctor import run_doctor

    result = run_doctor(seconds=args.seconds)
    try:
        from pitwall.learned import learned_state
        from pitwall.store.db import open_configured

        settings = ConfigStore().current()
        db = open_configured(settings)
        state = learned_state(db, settings) if db is not None else {}
        tracks = state.get("tracks", [])
        tuned = state.get("tuned_cooldowns", [])
        print("learned state:")
        if tracks:
            for track in tracks:
                print(
                    f"  track {track['track_id']} {track['name']}: "
                    f"{track['session_count']} sessions, {track['ingested_count']} ingested"
                )
        else:
            print("  no persisted track learning")
        print(f"  tuned rules: {len(tuned)}")
        if db is not None:
            print(f"  database: {db.path}")
            print(f"  quarantined values: {len(db.quarantined_params())}")
    except Exception:
        print("learned state: unavailable")
    return result


def cmd_maintain(args: argparse.Namespace) -> int:
    """Quarantine bad learned values, rebuild stint priors, grade sessions."""
    from pitwall.maintenance import maintain
    from pitwall.store.db import Database, open_configured

    settings = ConfigStore().current()
    db = Database(args.db) if args.db else open_configured(settings)
    if db is None:
        print("maintain: persistence disabled")
        return 1
    report = maintain(db, settings.thresholds)
    print(f"database: {db.path}")
    for line in report.quarantined:
        print(f"  quarantined {line}")
    print(f"maintain: {report.summary()}")
    return 0


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB"


def cmd_cleanup(args: argparse.Namespace) -> int:
    """List old files that are safe to delete; delete them after confirmation."""
    from pitwall.cleanup import apply, plan_cleanup
    from pitwall.store.db import Database, open_configured

    settings = ConfigStore().current()
    db = Database(args.db) if args.db else open_configured(settings)
    if db is None:
        print("cleanup: persistence disabled, recordings are never deleted")
    db_path = (
        Path(db.path).expanduser()
        if db is not None
        else Path(settings.persistence.path).expanduser()
    )
    plan = plan_cleanup(
        Path(args.recordings or settings.recording.directory),
        db_path.parent,
        Path(settings.speech.voices_dir),
        db.ingested_uids() if db is not None else set(),
        args.days,
    )
    if plan.kept_unlearned:
        print(f"keeping {plan.kept_unlearned} old recording(s) not learned yet")
    if not plan.delete:
        print("cleanup: nothing to delete")
        return 0
    by_reason: dict[str, list[int]] = {}
    for c in plan.delete:
        print(f"  {_mb(c.size):>9}  {c.path}")
        by_reason.setdefault(c.reason, []).append(c.size)
    for reason, sizes in by_reason.items():
        print(f"{len(sizes)} x {reason}: {_mb(sum(sizes))}")
    prompt = f"Delete {len(plan.delete)} file(s), {_mb(plan.total_bytes)}? [y/N] "
    if not args.yes:
        if not sys.stdin.isatty():
            print("cleanup: not confirmed (use --yes to run without a prompt)")
            return 1
        if input(prompt).strip().lower() not in ("y", "yes"):
            print("cleanup: cancelled, nothing deleted")
            return 1
    n, freed = apply(plan)
    print(f"cleanup: deleted {n} file(s), freed {_mb(freed)}")
    return 0


def cmd_compress(args: argparse.Namespace) -> int:
    dst = compress_recording(Path(args.file))
    print(f"compressed -> {dst}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pitwall")
    sub = p.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="record UDP telemetry to .f1bin")
    rec.add_argument("--host", default="0.0.0.0")
    rec.add_argument("--port", type=int, default=20777)
    rec.add_argument("--out", default="recordings/")
    rec.add_argument("--profile", choices=PROFILES, default="full")
    rec.set_defaults(func=cmd_record)

    rep = sub.add_parser("replay", help="replay a recording through the engine")
    rep.add_argument("file")
    rep.add_argument("--speed", default="max", help="1|N|max")
    rep.add_argument("--stats", action="store_true")
    rep.add_argument("--no-rules", action="store_true", help="packet census only")
    rep.add_argument("--from-lap", type=int, default=None, help="seek via .f1idx")
    rep.add_argument("--from-us", type=int, default=None)
    rep.add_argument("--to-us", type=int, default=None)
    rep.add_argument("--serve", action="store_true", help="run dashboard while replaying")
    rep.add_argument(
        "--mask-restricted",
        action="store_true",
        help="zero rival fuel/ERS/tyre-wear fields, as an online lobby with restricted telemetry",
    )
    rep.add_argument(
        "--seed-db",
        default=":memory:",
        help="SQLite db for replay persistence (default :memory:; never the real DB)",
    )
    rep.set_defaults(func=cmd_replay)

    dif = sub.add_parser("diff", help="compare two rules dirs over recording(s)")
    dif.add_argument("recordings", nargs="+")
    dif.add_argument("--a", default=None, help="rules dir A (default: packaged defaults)")
    dif.add_argument("--b", required=True, help="rules dir B")
    dif.add_argument("--a-mindset", default=None)
    dif.add_argument("--b-mindset", default=None)
    dif.add_argument("--corpus", default=None, help="glob of extra recordings to aggregate")
    dif.add_argument("--json", action="store_true")
    dif.add_argument(
        "--record", action="store_true", help="store per-rule A/B counts in SQLite for tune"
    )
    dif.set_defaults(func=cmd_diff)

    tun = sub.add_parser("tune", help="fold review grades + A/B results into rule tuning")
    tun.add_argument("paths", nargs="*", help="recordings to ingest before tuning")
    tun.add_argument("--calls-mode", choices=["on", "off"], default=None)
    tun.add_argument("--db", default=None, help="SQLite path (default: configured database)")
    tun.set_defaults(func=cmd_tune)

    mt = sub.add_parser("maintain", help="repair learned state (runs automatically on start)")
    mt.add_argument("--db", default=None)
    mt.set_defaults(func=cmd_maintain)

    cu = sub.add_parser("cleanup", help="delete old learned recordings and caches (asks first)")
    cu.add_argument("--days", type=float, default=30.0, help="only files older than this")
    cu.add_argument("--recordings", default=None, help="recordings folder (default: settings)")
    cu.add_argument("--db", default=None)
    cu.add_argument("--yes", action="store_true", help="delete without asking")
    cu.set_defaults(func=cmd_cleanup)

    dg = sub.add_parser("digest", help="hindsight-grade a session and write its digest")
    dg.add_argument("paths", nargs="*", help="recordings to ingest")
    dg.add_argument("--calls-mode", choices=["on", "off"], default=None)
    dg.add_argument("--db", default=None, help="SQLite path (default: configured database)")
    dg.add_argument("--session", default=None, help="session uid (default: latest)")
    dg.add_argument("--out", default=None, help="digest dir (default: ~/.pitwall/digests, - none)")
    dg.add_argument("--json", action="store_true")
    dg.set_defaults(func=cmd_digest)

    debrief = sub.add_parser("debrief", help="export a standalone session debrief")
    debrief.add_argument("--session", default="latest", help="session uid (default: latest)")
    debrief.add_argument("--db", default=None, help="SQLite path (default: configured database)")
    debrief.add_argument("--out", default=None, help="HTML output path")
    debrief.set_defaults(func=cmd_debrief)

    ev = sub.add_parser("evaluate", help="compare recorded calls-on and calls-off outcomes")
    ev.add_argument("--db", default=None)
    ev.add_argument("--track", type=int, default=None)
    ev.add_argument("--json", action="store_true")
    ev.set_defaults(func=cmd_evaluate)

    proposal = sub.add_parser("propose", help="review-only corpus threshold proposals")
    proposal.add_argument("--db", default=None)
    proposal.add_argument("--candidate-rules", type=Path, default=None)
    proposal.add_argument("--out", default=None)
    proposal.set_defaults(func=cmd_propose)

    cal = sub.add_parser("calibrate", help="fit track priors from recorded sessions")
    cal.add_argument("paths", nargs="*", help="recordings to ingest before calibration")
    cal.add_argument("--db", default=None, help="SQLite path (default: configured database)")
    cal.add_argument("--track", type=int, default=None)
    cal.add_argument("--dry-run", action="store_true")
    cal.add_argument("--write-overlay", action="store_true")
    cal.add_argument("--overlay-dir", type=Path, default=None)
    cal.add_argument("--json", action="store_true")
    cal.set_defaults(func=cmd_calibrate)

    rpt = sub.add_parser("report", help="bundle a recording + decisions for a bug report")
    rpt.add_argument("--recording", default=None, help=".f1bin path (default: newest in dir)")
    rpt.add_argument("-o", "--out", default=None, help="output zip path")
    rpt.set_defaults(func=cmd_report)

    rc = sub.add_parser("rules", help="rule tooling")
    rsub = rc.add_subparsers(dest="rules_command", required=True)
    rcheck = rsub.add_parser("check", help="validate rule expressions")
    rcheck.set_defaults(func=cmd_rules_check)

    trim = sub.add_parser("trim", help="extract a time range from a recording")
    trim.add_argument("file")
    trim.add_argument("--from-us", type=int, default=None)
    trim.add_argument("--to-us", type=int, default=None)
    trim.add_argument(
        "--profile", choices=PROFILES, default=None, help="also downsample (e.g. full -> lite)"
    )
    trim.add_argument("--out", required=True)
    trim.set_defaults(func=cmd_trim)

    idx = sub.add_parser("index", help="rebuild the .f1idx sidecar")
    idx.add_argument("file")
    idx.set_defaults(func=cmd_index)

    st = sub.add_parser("stats", help="packet census of a recording")
    st.add_argument("file", nargs="?")
    st.add_argument("--learned", action="store_true")
    st.add_argument("--db", default=None, help="SQLite path (default: configured database)")
    st.add_argument("--track", type=int, default=None)
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_stats)

    vc = sub.add_parser("voice", help="voice channel: SRGS grammar, SAPI devices, Phase 0 spike")
    vc.add_argument("action", choices=["spike", "devices", "grammar"])
    vc.add_argument("--port", type=int, default=None, help="UDP port for Action 1 taps")
    vc.add_argument("--no-udp", action="store_true", help="Enter key only; don't bind UDP")
    vc.add_argument("--device", type=int, default=None, help="audio input index")
    vc.add_argument("--recognizer", default=None, help="recogniser description substring")
    vc.add_argument("--confidence", type=float, default=None, help="confidence_min override")
    vc.add_argument("--affinity", default=None, help="CPU affinity mask, e.g. 0xF000")
    vc.add_argument("--grammar", choices=["srgs", "api"], default="srgs")
    vc.add_argument("--lang", default="en-US", help="xml:lang for `grammar`")
    vc.add_argument("--say", action="store_true", help="speak 'Copy, <intent>' via SAPI")
    vc.add_argument("--log", default=None, help="JSONL log path")
    vc.set_defaults(func=cmd_voice)

    doc = sub.add_parser("doctor", help="bind-test the port and report observed telemetry")
    doc.add_argument("--host", default="0.0.0.0")
    doc.add_argument("--port", type=int, default=20777)
    doc.add_argument("--seconds", type=float, default=5.0)
    doc.set_defaults(func=cmd_doctor)

    cz = sub.add_parser("compress", help="zstd-compress a recording")
    cz.add_argument("file")
    cz.set_defaults(func=cmd_compress)

    st2 = sub.add_parser("start", help="live: UDP ingest + rules + dashboard + speech")
    st2.add_argument(
        "--record",
        choices=[*PROFILES, "off"],
        default=None,
        help="recording profile (default: settings recording.profile = lite); "
        "full = every packet at native rate, for debugging/tuning",
    )
    st2.add_argument(
        "--no-watchdog",
        action="store_true",
        help="run recorder and engine in one process (no crash restart)",
    )
    st2.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    st2.set_defaults(func=cmd_start)

    sp = sub.add_parser("speak", help="audio check: speak a line through the speech backend")
    sp.add_argument("text", nargs="?", default="Pit wall online. Radio check.")
    sp.add_argument("--engine", choices=["auto", "piper", "sapi", "null"], default="auto")
    sp.add_argument("--voice", help="Piper voice name, e.g. en_GB-alan-medium")
    sp.add_argument("--speed", type=float, help="Piper pace multiplier (>1 faster)")
    sp.add_argument("--save", metavar="WAV", help="render with Piper to a WAV file instead")
    sp.set_defaults(func=cmd_speak)

    vo = sub.add_parser("voices", help="list or download Piper voices")
    vo.add_argument("action", nargs="?", choices=["list", "get", "warm"], default="list")
    vo.add_argument("names", nargs="*", help="voice names for get (default: configured)")
    vo.set_defaults(func=cmd_voices)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)  # type: ignore[no-any-return]


if __name__ == "__main__":
    sys.exit(main())
