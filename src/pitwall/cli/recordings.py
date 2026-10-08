from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from pitwall.audio.dispatcher import Call
from pitwall.cli.common import (
    _resolve_recording,
    db_from_args,
    emit_json_or,
    open_db,
)
from pitwall.cli.serve import _serve
from pitwall.clock import Clock, ReplayClock, VirtualClock
from pitwall.config.loader import ConfigStore
from pitwall.engine import Engine, build_census_engine, build_engine, run_replay
from pitwall.ingest import Ingest
from pitwall.net.profile import RecordFilter
from pitwall.net.recording import (
    RecordingReader,
    RecordingWriter,
    ensure_index,
    index_path_for,
    list_recordings,
    read_index,
)


def _parse_speed(value: str) -> float | None:
    if value == "max":
        return None
    v = float(value)
    if v <= 0:
        raise argparse.ArgumentTypeError("speed must be positive or 'max'")
    return v


def _lap_seek_us(path: Path, lap: int) -> int | None:
    if ensure_index(path):
        print(f"index: rebuilt {index_path_for(path)}")
    for entry in read_index(path):
        if entry["kind"] == "lap" and entry["detail"] == str(lap):
            return int(entry["offset_us"])
    return None


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB"


def _pitwall_version() -> str:
    try:
        from importlib.metadata import version

        return version("pitwall")
    except Exception:
        return "dev"


def cmd_replay(args: argparse.Namespace) -> int:
    from pitwall.derive import is_synthetic_uid
    from pitwall.net.mask import mask_restricted
    from pitwall.store.db import Database

    speed = _parse_speed(args.speed)
    file = _resolve_recording(args.file, ConfigStore().current())
    with RecordingReader(file) as reader:
        header = reader.header
    synthetic = bool(header.metadata.get("synthetic")) or is_synthetic_uid(header.session_uid)
    from_us = args.from_us
    if args.from_lap is not None:
        from_us = _lap_seek_us(file, args.from_lap)
        if from_us is None:
            print(f"replay: lap {args.from_lap} not found in index of {file}")
            return 1

    def persist_recording_origin(engine: Engine, db: Database | None) -> None:
        if db is None:
            return
        uid = engine.state.session_uid
        if uid is None:
            uid = header.session_uid
        session = db.session_row(uid) or {}
        db.set_session_origin(
            uid,
            started_at=header.wall_clock_start_us / 1_000_000.0,
            recording_path=str(file),
            calls_mode=str(session.get("calls_mode") or ""),
            synthetic=synthetic,
            derived_from=str(header.metadata.get("derived_from") or ""),
        )

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
                synthetic=synthetic,
            )
            if args.mask_restricted:
                eng.ingest.transform = mask_restricted
            return eng

        live_speed = speed or 1.0  # paced replay drives the dashboard
        review = ReviewController(
            file,
            _engine_factory,
            hub,
            speed=live_speed,
            db=seed_db,
            thresholds=ConfigStore().current().thresholds,
        )
        engine = _engine_factory(clock)
        persist_recording_origin(engine, seed_db)
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
        persist_recording_origin(engine, seed_db)
        return 0
    db: Database | None = Database(args.seed_db) if args.seed_db else None
    engine = (
        build_census_engine(clock)
        if args.no_rules
        else build_engine(clock=clock, db=db, synthetic=synthetic)
    )
    if args.mask_restricted:
        engine.ingest.transform = mask_restricted
    replay_coro = run_replay(file, engine, speed, from_us=from_us, to_us=args.to_us)
    delivered, calls = asyncio.run(replay_coro)
    persist_recording_origin(engine, db)
    print(f"replayed {delivered} datagrams from {file}; {len(calls)} calls")
    if args.stats:
        print(json.dumps(engine.ingest.census(now=engine.clock.now()), indent=2))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    if args.quality:
        return _stats_quality(args)
    if args.learned:
        return _stats_learned(args)
    return _stats_census(args)


def _stats_quality(args: argparse.Namespace) -> int:
    from pitwall.digest import quality_trend
    from pitwall.learnpack import pack_track_minutes

    if args.sessions < 1:
        print("stats --quality: sessions must be positive")
        return 2
    settings = ConfigStore().current()
    db = open_db(args, settings, "stats --quality")
    if db is None:
        return 1
    pack_dir = Path(settings.learning.pack_dir).expanduser()
    minutes = pack_track_minutes(db, pack_dir)
    report = quality_trend(db, args.sessions, pack_dir)
    rows = report["sessions"]
    trend = report["trend"]

    def format_quality(_report: Any) -> None:
        print(
            f"track minutes: {minutes['minutes']:,.1f} over "
            f"{minutes['sessions']:,} sessions ({minutes['laps']:,} laps)"
        )
        if not rows:
            print("No sessions with fired calls.")
        else:
            for row in rows:
                print(
                    f"{row['date']} · {row['track']} · {row['session_type']} · "
                    f"{row['fired']} fired · good {row['good_pct']:g}% · "
                    f"neg {row['neg_rate_pct']:g}% · "
                    f"{row['unanswered_questions']} unanswered · "
                    f"{row['ungraded']} ungraded"
                )
            if trend is not None:
                print(
                    f"trend: good% {trend['older_mean_good_pct']:.1f} -> "
                    f"{trend['newer_mean_good_pct']:.1f} "
                    f"({trend['delta_pct']:+.1f}) over {trend['sessions']} sessions"
                )

    emit_json_or(args, {"track_minutes": minutes, **report}, format_quality)
    return 0


def _stats_learned(args: argparse.Namespace) -> int:
    from pitwall.learned import format_learned, learned_state

    settings = ConfigStore().current()
    db = open_db(args, settings, "stats --learned")
    if db is None:
        return 1
    state = learned_state(db, settings, track_id=args.track)
    emit_json_or(args, state, format_learned)
    return 0


def _stats_census(args: argparse.Namespace) -> int:
    file = _resolve_recording(args.file, ConfigStore().current())
    ingest = Ingest()
    last_t = 0.0
    with RecordingReader(file) as reader:
        header = reader.header
        for offset_us, payload in reader:
            last_t = offset_us / 1_000_000
            ingest.on_datagram(payload, last_t)
    out = {
        "file": str(file),
        "session_uid": header.session_uid,
        "packet_format": header.packet_format,
        "config_hash": header.config_hash,
        "metadata": header.metadata,
        **ingest.census(now=last_t),
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_trim(args: argparse.Namespace) -> int:
    src = _resolve_recording(args.file, ConfigStore().current())
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


def cmd_derive(args: argparse.Namespace) -> int:
    from pitwall.derive import derive_recording, ops_from_options

    settings = ConfigStore().current()
    source = _resolve_recording(args.file, settings)
    try:
        ops = ops_from_options(args.wear_scale, args.inject_sc, args.vsc, args.penalty)
        summary = derive_recording(source, Path(args.out), ops)
    except ValueError as exc:
        print(f"derive: {exc}")
        return 1
    print(f"derived session UID: 0x{summary.header.session_uid:016x}")
    print(f"mutations: {', '.join(op.label for op in ops)}")
    print(f"wrote {summary.record_count} records -> {args.out}")
    return 0


def cmd_recordings(args: argparse.Namespace) -> int:
    """List recordings newest first; the index works as a recording argument."""
    from pitwall.derive import is_synthetic_uid

    directory = Path(ConfigStore().current().recording.directory)
    found = list_recordings(directory)
    if not found:
        print(f"recordings: none in {directory}")
        return 0
    print(f"recordings in {directory}, newest first:")
    print(f"{'#':>3}  {'file':<44} {'start':<16} {'size':>9}  session_uid")
    for i, path in enumerate(found):
        start, uid, synthetic = "?", "?", False
        try:
            with RecordingReader(path) as reader:
                header = reader.header
            uid = str(header.session_uid)
            synthetic = bool(header.metadata.get("synthetic")) or is_synthetic_uid(
                header.session_uid
            )
            if header.wall_clock_start_us:
                t = time.localtime(header.wall_clock_start_us / 1e6)
                start = time.strftime("%Y-%m-%d %H:%M", t)
        except Exception:
            pass
        marker = " synthetic" if synthetic else ""
        print(f"{i:>3}  {path.name:<44} {start:<16} {_mb(path.stat().st_size):>9}  {uid}{marker}")
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
    rec_path = _resolve_recording(args.recording or args.recording_opt, settings)
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


def cmd_cleanup(args: argparse.Namespace) -> int:
    """List old files that are safe to delete; delete them after confirmation."""
    from pitwall.cleanup import apply, plan_cleanup

    settings = ConfigStore().current()
    db = db_from_args(args, settings)
    if db is None:
        print("cleanup: persistence disabled, recordings are never deleted")
    db_path = (
        Path(db.path).expanduser()
        if db is not None
        else Path(settings.persistence.path).expanduser()
    )
    try:
        plan = plan_cleanup(
            Path(args.recordings or settings.recording.directory),
            db_path.parent,
            Path(settings.speech.voices_dir),
            db.ingested_uids() if db is not None else set(),
            args.days,
            recording_imports=db.ingested_recordings() if db is not None else {},
        )
    except ValueError as exc:
        print(f"cleanup: {exc}")
        return 2
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
