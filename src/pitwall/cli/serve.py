from __future__ import annotations

import argparse
import asyncio
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pitwall.audio.decision_log import DecisionLog
from pitwall.audio.dispatcher import Call, Dispatcher
from pitwall.clock import WallClock
from pitwall.config.loader import ConfigStore
from pitwall.engine import Engine, action_bits
from pitwall.ingest import Ingest
from pitwall.net.recording import RecordingRotator
from pitwall.protocol.header import PacketId
from pitwall.rules.engine import RuleEngine
from pitwall.state.session import SessionState
from pitwall.supervisor import (
    ALIVE_FILE,
    WATCHDOG_ENV,
    Supervisor,
    read_recording_pointer,
    runtime_dir,
)

if TYPE_CHECKING:
    from pitwall.config.models import Settings
    from pitwall.server.hub import Hub

BannerEntry = tuple[str, str | None]
_BANNER_GROUPS = {
    "dashboard": 0,
    "LAN": 0,
    "PIN": 0,
    "telemetry": 1,
    "watchdog": 1,
    "recovery": 1,
    "recording": 1,
    "speech": 1,
    "learning": 2,
    "pitwall": 2,
}


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


def _banner(lines: list[BannerEntry]) -> str:
    entries: list[tuple[str, str, int, bool]] = []
    for label, value in lines:
        if value is None:
            continue
        if label == "":
            match = re.fullmatch(r"([A-Za-z][\w ]{0,15}): (.*)", value)
            if match is not None:
                label, value = match.groups()
            else:
                entries.append((label, value, 1, True))
                continue
        entries.append((label, value, _BANNER_GROUPS.get(label, 1), False))

    width = max(
        (len(label) for label, _, _, full_line in entries if not full_line and label != "PIN"),
        default=0,
    )
    entries.sort(key=lambda entry: entry[2])
    rendered: list[str] = []
    previous_group: int | None = None
    for label, value, group, full_line in entries:
        if previous_group is not None and group != previous_group:
            rendered.append("")
        previous_group = group
        if full_line:
            rendered.append(f"  {value}")
        elif label == "PIN":
            pin, note = value.split("  ", 1)
            rendered.extend(_pin_box(pin, note).splitlines())
        else:
            rendered.append(f"  {label:<{width}}  {value}")
    return "\n".join(rendered)


def _pin_box(pin: str, note: str) -> str:
    try:
        "┌─│".encode(sys.stdout.encoding or "ascii")
        horizontal, vertical, top_left, top_right, bottom_left, bottom_right = (
            "─",
            "│",
            "┌",
            "┐",
            "└",
            "┘",
        )
    except (UnicodeEncodeError, LookupError):
        horizontal, vertical = "-", "|"
        top_left, top_right, bottom_left, bottom_right = "+", "+", "+", "+"
    digits = f"\033[1m{pin}\033[0m" if sys.stdout.isatty() else pin
    content = f"  PIN  {digits}  {note}  "
    plain_content = f"  PIN  {pin}  {note}  "
    width = len(plain_content)
    border = horizontal * width
    return "\n".join(
        (
            f"  {top_left}{border}{top_right}",
            f"  {vertical}{content}{vertical}",
            f"  {bottom_left}{border}{bottom_right}",
        )
    )


def _quiet_left_s(engine: Engine, now: float) -> float | None:
    until = engine.dispatcher.quiet_until
    return until - now if until is not None and now < until else None


async def _state_broadcast(
    base: Engine,
    hub: Hub,
    store: ConfigStore,
    active: Callable[[], Engine],
    *,
    pin: str | None = None,
    pin_note: str = "",
) -> None:
    from pitwall.server.app import state_payload

    period = 1.0 / store.current().ui.state_hz
    last_status = base.clock.now()
    seen_packet = False
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
        if snap.last_packet_t is not None and not seen_packet:
            seen_packet = True
            if pin:
                print("telemetry connected\n" + _pin_box(pin, pin_note), flush=True)
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
    *,
    banner: list[BannerEntry] | None = None,
) -> None:
    import uvicorn

    from pitwall.server.app import create_app
    from pitwall.server.pin import PinGate
    from pitwall.server.serve import PitwallServer, serve_with

    settings = store.current()
    gate = (
        PinGate(trust_local=settings.connection.pin_trust_localhost)
        if settings.connection.require_pin
        else None
    )
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
        pin_gate=gate,
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
    server = PitwallServer(config)
    host, port = settings.connection.http_host, settings.connection.http_port
    scheme = "https" if settings.connection.https_cert and settings.connection.https_key else "http"
    local = "localhost" if host in ("0.0.0.0", "::", "") else host
    entries = list(banner or [])
    entries.extend(
        [
            ("dashboard", f"{scheme}://{local}:{port}"),
            ("LAN", f"{scheme}://{_lan_ip()}:{port}"),
        ]
    )
    pin_note = "(LAN devices only)" if gate is not None and gate.trust_local else "(all devices)"
    if gate is not None:
        entries.append(("PIN", f"{gate.pin}  {pin_note}"))
    entries.extend(
        [
            ("recording", getattr(engine, "recording_desc", "off")),
            ("speech", getattr(engine, "speaker_name", "null")),
        ]
    )
    print(_banner(entries), flush=True)
    await serve_with(
        server,
        _state_broadcast(
            engine,
            hub,
            store,
            active,
            pin=gate.pin if gate else None,
            pin_note=pin_note,
        ),
        coro,
    )


def _recording_metadata(store: ConfigStore, settings: Settings) -> dict[str, object]:
    """Header fields for a live recording. `speech_enabled` and `quiet` are the
    inputs behind `calls_mode`; their presence also marks a recorder whose
    speaker honours `speech.enabled` (ingest trusts `calls_mode: off` only then)."""
    return {
        "config_hash": store.hash,
        "send_rate_hz": settings.connection.send_rate_hz,
        "speech_enabled": settings.speech.enabled,
        "quiet": settings.policy.quiet,
        "calls_mode": "off" if settings.policy.quiet or not settings.speech.enabled else "on",
    }


def cmd_start(args: argparse.Namespace) -> int:
    from pitwall.audio.piper_tts import PiperSpeaker
    from pitwall.audio.speaker import make_speaker
    from pitwall.doctor import set_below_normal_priority
    from pitwall.net.recording import RecordingRotator
    from pitwall.net.udp import listen as udp_listen
    from pitwall.server.hub import Hub

    set_below_normal_priority()
    store = ConfigStore()
    settings = store.current()
    pack_dir = Path(settings.learning.pack_dir).expanduser()
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
            metadata=_recording_metadata(store, settings),
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
    banner: list[BannerEntry] = []
    db = None
    if settings.persistence.enabled:
        from pitwall.store.db import open_configured

        db = open_configured(settings)
    if db is not None:
        from pitwall.maintenance import maintain, mid_session

        try:
            report = maintain(db, settings, refit=not mid_session(db, settings))
            banner.append(("learning", report.summary()))
        except sqlite3.Error as e:
            banner.append(("learning", f"upkeep skipped ({e})"))
        from pitwall.digest import startup_scorecard

        try:
            banner.append(("", startup_scorecard(db, pack_dir)))
        except sqlite3.Error as e:
            banner.append(("pitwall", f"scorecard skipped ({e})"))
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
    if isinstance(speaker, PiperSpeaker):
        speaker.on_audio = hub.audio
        hub.streams_audio = True
    engine = Engine(
        store,
        clock,
        ingest,
        state,
        rule_engine,
        dispatcher,
        learning_pack_dir=pack_dir,
        learning_pack_keep_days=settings.learning.pack_keep_days,
    )
    if recorder is not None:
        engine.recording_path_source = lambda: recorder.current_path or recorder.last_path
    elif child:
        engine.recording_path_source = lambda: read_recording_pointer(rt_dir)
        engine.alive_path = rt_dir / ALIVE_FILE
    recovered = engine.recover()
    if recovered is not None:
        banner.append(("recovery", f"{recovered}; radio: {engine.rejoin_text()}"))
    udp_host, udp_port = settings.connection.udp_host, settings.connection.udp_port
    if child:
        udp_host, udp_port = "127.0.0.1", settings.engine.engine_port
        banner.append(("watchdog", os.environ.get(WATCHDOG_ENV)))
    else:
        banner.append(("telemetry", f"UDP {udp_host}:{udp_port}"))

    async def live() -> None:
        transport = await udp_listen(udp_host, udp_port, ingest, clock)
        try:
            await engine.run_live()
        finally:
            transport.close()

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
        asyncio.run(_serve(engine, hub, store, live(), banner=banner))
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
        if db is not None:
            from pitwall.learnpack import write_pack

            try:
                saved = write_pack(
                    db,
                    pack_dir,
                    keep_days=settings.learning.pack_keep_days,
                    refresh_quality=[state.session_uid] if state.session_uid is not None else None,
                )
                print(f"learning pack: saved {saved}", flush=True)
            except (OSError, sqlite3.Error, ValueError) as e:
                print(f"learning pack: skipped ({e})", flush=True)
            except KeyboardInterrupt:
                print("learning pack: interrupted", flush=True)
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
            metadata=_recording_metadata(store, settings),
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
    sup.run()
    if recorder is not None and recorder.last_path is not None:
        print(f"recording: saved {recorder.last_path}")
    return 0


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
