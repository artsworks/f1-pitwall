#!/usr/bin/env python3
"""Send recorded or synthetic packets to the live UDP listener while ticks stall.

Usage: uv run python scripts/udp_load.py [RECORDING] --rate 2000 --seconds 10 --stall-ms 1000
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import multiprocessing as mp
import queue
import socket
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tests.race_synth import RaceSpec, race_stream  # noqa: E402
from tests.synth import write_packet_stream  # noqa: E402

from pitwall.clock import WallClock  # noqa: E402
from pitwall.engine import build_engine  # noqa: E402
from pitwall.net.recording import RecordingReader  # noqa: E402
from pitwall.net.udp import effective_rcvbuf, listen  # noqa: E402

BURST = 6


def _send(rate: int, seconds: float, port: int, path: Path, result: mp.Queue) -> None:
    with RecordingReader(path) as reader:
        packets = [payload for _, payload in reader]
    if not packets:
        raise ValueError(f"recording has no datagrams: {path}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    time.sleep(1.0)
    start = time.perf_counter()
    period = BURST / rate
    next_send = start
    sent = 0
    while time.perf_counter() - start < seconds:
        for _ in range(BURST):
            sock.sendto(packets[sent % len(packets)], ("127.0.0.1", port))
            sent += 1
        next_send += period
        delay = next_send - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
    sock.close()
    result.put((sent, time.perf_counter() - start))


async def _run(args: argparse.Namespace, recording: Path) -> None:
    clock = WallClock()
    engine = build_engine(
        clock=clock,
        overrides={"engine": {"heartbeat_s": 0}},
        isolated=True,
        sinks=[],
        db=None,
        decision_log_fp=io.StringIO(),
    )
    if args.stall_ms:
        original_tick = engine.tick
        last_stall = time.monotonic()

        def stalled_tick(t: float):
            nonlocal last_stall
            now = time.monotonic()
            if now - last_stall >= 2.0:
                last_stall = now
                if args.stall_kind == "busy":
                    end = time.perf_counter() + args.stall_ms / 1000
                    while time.perf_counter() < end:
                        pass
                else:
                    time.sleep(args.stall_ms / 1000)
            return original_tick(t)

        engine.tick = stalled_tick

    results: mp.Queue = mp.Queue()
    sender = mp.Process(
        target=_send,
        args=(args.rate, args.seconds, args.port, recording, results),
    )
    transport = await listen(
        "127.0.0.1",
        args.port,
        engine.ingest,
        clock,
        rcvbuf=args.rcvbuf,
    )
    task = None
    sender_started = False
    try:
        effective = effective_rcvbuf(transport)
        task = asyncio.create_task(engine.run_live())
        sender.start()
        sender_started = True
        while True:
            try:
                sent, duration = await asyncio.to_thread(results.get, True, 0.5)
                break
            except queue.Empty:
                if not sender.is_alive():
                    raise SystemExit(f"sender failed with exit code {sender.exitcode}") from None
        await asyncio.sleep(1.0)
        received = engine.ingest.raw_datagrams
        lost = sent - received
        loss_pct = lost / sent * 100 if sent else 0.0
        print(
            f"sent={sent} got={received} lost={lost} lost_pct={loss_pct:.2f} "
            f"effective_rcvbuf={effective} bytes achieved_rate={sent / duration:.1f}/s"
        )
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        transport.close()
        if sender_started:
            if sender.is_alive():
                sender.terminate()
            sender.join()
        results.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("recording", nargs="?", type=Path)
    parser.add_argument("--rate", type=int, default=2000)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--stall-ms", type=float, default=0.0)
    parser.add_argument("--stall-kind", choices=("busy", "sleep"), default="busy")
    parser.add_argument("--rcvbuf", type=int)
    parser.add_argument("--port", type=int, default=29777)
    args = parser.parse_args()
    if args.recording is not None and not args.recording.is_file():
        parser.error(f"recording path does not exist or is not a file: {args.recording}")
    if args.rate <= 0:
        parser.error("--rate must be greater than zero")
    if args.seconds <= 0:
        parser.error("--seconds must be greater than zero")
    if args.recording is not None:
        asyncio.run(_run(args, args.recording))
        return
    with TemporaryDirectory() as directory:
        recording = Path(directory) / "synthetic.f1bin"
        spec = RaceSpec(laps=3, track_id=7, session_type=15)
        write_packet_stream(recording, race_stream(spec), session_uid=spec.session_uid)
        asyncio.run(_run(args, recording))


if __name__ == "__main__":
    main()
