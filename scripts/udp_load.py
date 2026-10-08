#!/usr/bin/env python3
"""Send recorded packets to the live UDP listener while ticks stall.

Usage: uv run python scripts/udp_load.py --rate 2000 --seconds 10 --stall-ms 1000
"""

from __future__ import annotations

import argparse
import asyncio
import io
import multiprocessing as mp
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from pitwall.clock import WallClock  # noqa: E402
from pitwall.engine import build_engine  # noqa: E402
from pitwall.net.recording import RecordingReader  # noqa: E402
from pitwall.net.udp import effective_rcvbuf, listen  # noqa: E402

RECORDING = Path("/home/ubuntu/bench/race.f1bin")
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


async def _run(args: argparse.Namespace) -> None:
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

    transport = await listen(
        "127.0.0.1",
        args.port,
        engine.ingest,
        clock,
        rcvbuf=args.rcvbuf,
    )
    effective = effective_rcvbuf(transport)
    task = asyncio.create_task(engine.run_live())
    results: mp.Queue = mp.Queue()
    sender = mp.Process(
        target=_send,
        args=(args.rate, args.seconds, args.port, RECORDING, results),
    )
    sender.start()
    sent, duration = await asyncio.to_thread(results.get)
    await asyncio.sleep(1.0)
    received = engine.ingest.raw_datagrams
    task.cancel()
    transport.close()
    sender.join()
    lost = sent - received
    loss_pct = lost / sent * 100 if sent else 0.0
    print(
        f"sent={sent} got={received} lost={lost} lost_pct={loss_pct:.2f} "
        f"effective_rcvbuf={effective} bytes achieved_rate={sent / duration:.1f}/s"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rate", type=int, default=2000)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--stall-ms", type=float, default=0.0)
    parser.add_argument("--stall-kind", choices=("busy", "sleep"), default="busy")
    parser.add_argument("--rcvbuf", type=int)
    parser.add_argument("--port", type=int, default=29777)
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
