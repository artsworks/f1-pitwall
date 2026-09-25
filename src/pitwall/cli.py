"""pitwall CLI: record, replay, trim, index, stats, doctor."""

from __future__ import annotations

import argparse
import asyncio
import json
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
from pitwall.engine import Engine, build_census_engine, build_engine, run_replay
from pitwall.ingest import Ingest
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
            return build_engine(
                clock=clk,
                sinks=[hub, LogSink(), _ReplaySpokenSink()],
                db=seed_db,
            )

        live_speed = speed or 1.0  # paced replay drives the dashboard
        review = ReviewController(
            Path(args.file),
            _engine_factory,
            hub,
            speed=live_speed,
            db=seed_db,
        )
        engine = _engine_factory(clock)
        review.engine = engine
        review.clock = None  # controller builds its own PausableClock on play

        async def _replay_coro() -> None:
            await review.play()
            if review._task is not None:  # noqa: SLF001
                await review._task

        store = ConfigStore()
        asyncio.run(_serve(engine, hub, store, _replay_coro(), review=review))
        return 0
    db: Database | None = Database(args.seed_db) if args.seed_db else None
    engine = build_census_engine(clock) if args.no_rules else build_engine(clock=clock, db=db)
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


def cmd_stats(args: argparse.Namespace) -> int:
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
        )
        hub.broadcast("state", payload)
        if snap.last_packet_t is not None:
            engine.metrics.note_packet_to_ws(snap.last_packet_t, engine.clock.now())
        await base.clock.sleep(period)


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
        review=review,
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
    )
    server = uvicorn.Server(config)
    host, port = settings.connection.http_host, settings.connection.http_port
    print(f"dashboard: http://{host}:{port}  (LAN: http://{_lan_ip()}:{port})")
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
    hub = Hub()
    store = ConfigStore()
    settings = store.current()
    clock = WallClock()

    profile = args.record or settings.recording.profile
    recorder = None
    if settings.recording.enabled and profile != "off":
        recorder = RecordingRotator(
            Path(settings.recording.directory),
            config_hash=int(store.hash, 16) % (2**32),
            metadata={"config_hash": store.hash, "send_rate_hz": settings.connection.send_rate_hz},
            profile=profile,
            compress=settings.recording.compress_on_close,
        )
    ingest = Ingest(recorder=recorder)
    state = SessionState(
        ema_fast_s=settings.engine.ema_fast_s,
        ema_slow_s=settings.engine.ema_slow_s,
        straight_hold_s=settings.engine.straight_hold_s,
        press_bit=settings.input.udp_action_bit,
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

    async def live() -> None:
        transport = await udp_listen(
            settings.connection.udp_host, settings.connection.udp_port, ingest, clock
        )
        try:
            await engine.run_live()
        finally:
            transport.close()

    async def shutdown() -> None:
        pass

    engine.speaker_name = speaker.name
    engine.recording_desc = (
        f"{profile} -> {settings.recording.directory}/" if recorder is not None else "off"
    )
    if settings.speech.enabled:
        t0 = clock.now()
        for call_id, text in (
            ("startup", "Pit wall online."),
            ("startup-radio-check", "Radio check, radio check."),
        ):
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
    try:
        asyncio.run(_serve(engine, hub, store, live()))
    except KeyboardInterrupt:
        pass
    finally:
        speaker.close()
        if recorder is not None:
            if settings.recording.compress_on_close:
                print("recording: compressing…", flush=True)
            recorder.close()
            if recorder.last_path is not None:
                print(f"recording: saved {recorder.last_path}")
        dlog.close()
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
    from pitwall.audio.piper_tts import SUGGESTED_VOICES, download_voice, installed_voices

    speech = ConfigStore().current().speech
    if args.action == "get":
        for name in args.names or [speech.piper_voice]:
            path = download_voice(speech, name)
            print(f"downloaded {name} -> {path}")
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


def cmd_doctor(args: argparse.Namespace) -> int:
    from pitwall.doctor import run_doctor

    return run_doctor(seconds=args.seconds)


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
    dif.set_defaults(func=cmd_diff)

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
    st.add_argument("file")
    st.set_defaults(func=cmd_stats)

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
    st2.set_defaults(func=cmd_start)

    sp = sub.add_parser("speak", help="audio check: speak a line through the speech backend")
    sp.add_argument("text", nargs="?", default="Pit wall online. Radio check.")
    sp.add_argument("--engine", choices=["auto", "piper", "sapi", "null"], default="auto")
    sp.add_argument("--voice", help="Piper voice name, e.g. en_GB-alan-medium")
    sp.add_argument("--speed", type=float, help="Piper pace multiplier (>1 faster)")
    sp.add_argument("--save", metavar="WAV", help="render with Piper to a WAV file instead")
    sp.set_defaults(func=cmd_speak)

    vo = sub.add_parser("voices", help="list or download Piper voices")
    vo.add_argument("action", nargs="?", choices=["list", "get"], default="list")
    vo.add_argument("names", nargs="*", help="voice names for get (default: configured)")
    vo.set_defaults(func=cmd_voices)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)  # type: ignore[no-any-return]


if __name__ == "__main__":
    sys.exit(main())
