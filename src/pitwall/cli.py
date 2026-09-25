"""pitwall CLI: record, replay, trim, index, stats, doctor."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pitwall.clock import VirtualClock, WallClock
from pitwall.config.loader import ConfigStore
from pitwall.engine import build_census_engine, build_engine, run_replay
from pitwall.ingest import Ingest
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
    recorder = RecordingRotator(out_dir)
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
    path = recorder.current_path
    if path is not None:
        print(f"last recording: {path}")
    return 0


def _lap_seek_us(path: Path, lap: int) -> int | None:
    for entry in read_index(path):
        if entry["kind"] == "lap" and entry["detail"] == str(lap):
            return int(entry["offset_us"])
    return None


def cmd_replay(args: argparse.Namespace) -> int:
    speed = _parse_speed(args.speed)
    from_us = args.from_us
    if args.from_lap is not None:
        from_us = _lap_seek_us(Path(args.file), args.from_lap)
        if from_us is None:
            print(f"replay: lap {args.from_lap} not found in index of {args.file}")
            return 1
    clock = VirtualClock() if speed is None else WallClock()
    if args.no_rules:
        engine = build_census_engine(clock)
    else:
        engine = build_engine(clock=clock)
    delivered, calls = asyncio.run(
        run_replay(Path(args.file), engine, speed, from_us=from_us, to_us=args.to_us)
    )
    print(f"replayed {delivered} datagrams from {args.file}; {len(calls)} calls")
    if args.stats:
        print(json.dumps(engine.ingest.census(now=engine.clock.now()), indent=2))
    return 0


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
    from_us = args.from_us
    to_us = args.to_us
    with (
        RecordingReader(src) as reader,
        RecordingWriter(
            Path(args.out),
            packet_format=reader.header.packet_format,
            session_uid=reader.header.session_uid,
            config_hash=reader.header.config_hash,
            game_version=reader.header.game_version,
            metadata={**reader.header.metadata, "trimmed_from": str(src)},
        ) as writer,
    ):
        kept = 0
        for offset_us, payload in reader:
            if offset_us < from_us:
                continue
            if offset_us > to_us:
                break
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


def cmd_doctor(args: argparse.Namespace) -> int:
    """M0 doctor: bind-test the UDP port and report observed format/rate."""
    ingest = Ingest()
    clock = WallClock()

    async def run() -> Ingest:
        try:
            transport = await listen(args.host, args.port, ingest, clock)
        except OSError as e:
            print(f"doctor: cannot bind UDP {args.host}:{args.port}: {e}")
            return ingest
        print(f"listening on {args.host}:{args.port} for {args.seconds:.0f}s ...")
        await asyncio.sleep(args.seconds)
        transport.close()
        return ingest

    asyncio.run(run())
    census = ingest.census(now=clock.now())
    total = sum(p["accepted"] for p in census["packets"].values())
    if total == 0 and census["dropped_unsupported"] == 0:
        print(
            "no datagrams seen. Check: UDP Telemetry On, correct IP/port, "
            "firewall inbound rule for UDP 20777."
        )
    else:
        for pid, entry in census["packets"].items():
            print(f"packet id {pid}: {entry['accepted']} ok, {entry['rate_hz']:.1f} Hz")
        if census["dropped_unsupported"]:
            print(
                f"{census['dropped_unsupported']} datagrams dropped: unsupported "
                "format — set UDP Format to 2026 in the game."
            )
        if census["dropped_malformed"]:
            print(f"{census['dropped_malformed']} malformed datagrams")
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
    rec.set_defaults(func=cmd_record)

    rep = sub.add_parser("replay", help="replay a recording through the engine")
    rep.add_argument("file")
    rep.add_argument("--speed", default="max", help="1|N|max")
    rep.add_argument("--stats", action="store_true")
    rep.add_argument("--no-rules", action="store_true", help="packet census only")
    rep.add_argument("--from-lap", type=int, default=None, help="seek via .f1idx")
    rep.add_argument("--from-us", type=int, default=None)
    rep.add_argument("--to-us", type=int, default=None)
    rep.set_defaults(func=cmd_replay)

    rc = sub.add_parser("rules", help="rule tooling")
    rsub = rc.add_subparsers(dest="rules_command", required=True)
    rcheck = rsub.add_parser("check", help="validate rule expressions")
    rcheck.set_defaults(func=cmd_rules_check)

    trim = sub.add_parser("trim", help="extract a time range from a recording")
    trim.add_argument("file")
    trim.add_argument("--from-us", type=int, required=True)
    trim.add_argument("--to-us", type=int, required=True)
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

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)  # type: ignore[no-any-return]


if __name__ == "__main__":
    sys.exit(main())
