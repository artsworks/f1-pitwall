#!/usr/bin/env python3
"""Measure replay CPU and allocation costs. `pitwall bench` measures scenario accuracy.

Usage: uv run python scripts/perf_replay.py [RECORDING] [--laps N]
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import hashlib
import io
import json
import statistics
import sys
import time
import timeit
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tests.race_synth import RaceSpec, race_stream  # noqa: E402
from tests.synth import make_packet, write_packet_stream  # noqa: E402

from pitwall.clock import VirtualClock  # noqa: E402
from pitwall.engine import build_engine, run_replay  # noqa: E402
from pitwall.net.recording import RecordingReader  # noqa: E402
from pitwall.protocol import packets  # noqa: E402
from pitwall.protocol.header import HEADER_SIZE, PACKET_SIZES, PacketId  # noqa: E402

TARGET_IDS = (
    PacketId.CAR_TELEMETRY,
    PacketId.CAR_STATUS,
    PacketId.CAR_DAMAGE,
    PacketId.CAR_TELEMETRY_2,
)


def _summary(values: list[float]) -> str:
    ordered = sorted(values)
    if not ordered:
        return "n/a"
    p50 = ordered[len(ordered) // 2]
    p99 = ordered[min(int(len(ordered) * 0.99), len(ordered) - 1)]
    return f"mean {statistics.mean(ordered) * 1e6:.1f} p50 {p50 * 1e6:.1f} p99 {p99 * 1e6:.1f}"


def _allocation_per_call(fn: Callable[[], Any], count: int = 100) -> tuple[float, float]:
    gc.collect()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    results = [fn() for _ in range(count)]
    after = tracemalloc.take_snapshot()
    stats = after.compare_to(before, "lineno")
    blocks = sum(stat.count_diff for stat in stats)
    size = sum(stat.size_diff for stat in stats)
    del results
    tracemalloc.stop()
    return blocks / count, size / count


def _time_per_call(fn: Callable[[], Any], number: int = 2000) -> float:
    return min(timeit.repeat(fn, number=number, repeat=5)) / number * 1e6


def _samples(path: Path) -> dict[int, bytes]:
    samples: dict[int, bytes] = {}
    with RecordingReader(path) as reader:
        for _, payload in reader:
            if len(payload) < HEADER_SIZE:
                continue
            packet_id = payload[6]
            if len(payload) == PACKET_SIZES.get(packet_id):
                samples.setdefault(packet_id, payload)
    for packet_id in TARGET_IDS:
        samples.setdefault(packet_id, make_packet(packet_id))
    return samples


def _calls_hash(calls: list[Any]) -> str:
    payload = [(call.rule_id, call.text, call.t) for call in calls]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def _measure_replay(path: Path) -> None:
    engine = build_engine(
        clock=VirtualClock(),
        overrides={"engine": {"heartbeat_s": 0}},
        isolated=True,
        sinks=[],
        db=None,
        decision_log_fp=io.StringIO(),
    )
    ingest_times: list[float] = []
    tick_times: list[float] = []
    original_ingest = engine.ingest.on_datagram
    original_tick = engine.tick

    def timed_ingest(payload: bytes, recv_time: float) -> None:
        start = time.perf_counter()
        original_ingest(payload, recv_time)
        ingest_times.append(time.perf_counter() - start)

    def timed_tick(t: float) -> Any:
        start = time.perf_counter()
        calls = original_tick(t)
        tick_times.append(time.perf_counter() - start)
        return calls

    engine.ingest.on_datagram = timed_ingest
    engine.tick = timed_tick
    start = time.perf_counter()
    count, calls = await run_replay(path, engine)
    wall = time.perf_counter() - start
    print(f"datagrams={count} wall_s={wall:.3f} datagrams/s={count / wall:.1f}")
    print(f"ingest_us {_summary(ingest_times)}")
    print(f"tick_us {_summary(tick_times)} max_ms={max(tick_times, default=0.0) * 1000:.3f}")
    print(f"calls={len(calls)} calls_sha256={_calls_hash(calls)}")


def _measure_packets(path: Path) -> None:
    samples = _samples(path)
    eager_parse = getattr(packets, "_parse_eager", packets.parse)
    print(
        "packet parse_us/allocations_per_packet "
        "(allocations are blocks, bytes; lazy+player reads header.player_car_index)"
    )
    print(
        f"{'packet':20} {'lazy us':>9} {'lazy blocks/bytes':>21} "
        f"{'eager us':>9} {'eager blocks/bytes':>21} "
        f"{'lazy+player us':>15} {'lazy+player blocks/bytes':>26}"
    )
    for packet_id in TARGET_IDS:
        payload = samples[packet_id]

        def lazy(packet_id: int = packet_id, payload: bytes = payload) -> Any:
            return packets.parse(packet_id, payload)

        def eager(packet_id: int = packet_id, payload: bytes = payload) -> Any:
            return eager_parse(packet_id, payload)

        def lazy_player(packet_id: int = packet_id, payload: bytes = payload) -> Any:
            packet = packets.parse(packet_id, payload)
            return packet.cars[packet.header.player_car_index]

        lazy_alloc = _allocation_per_call(lazy)
        eager_alloc = _allocation_per_call(eager)
        player_alloc = _allocation_per_call(lazy_player)
        label = PacketId(packet_id).name
        print(
            f"{label:20} {_time_per_call(lazy):9.2f} "
            f"{lazy_alloc[0]:8.1f}/{lazy_alloc[1]:.0f} "
            f"{_time_per_call(eager):9.2f} {eager_alloc[0]:8.1f}/{eager_alloc[1]:.0f} "
            f"{_time_per_call(lazy_player):15.2f} "
            f"{player_alloc[0]:8.1f}/{player_alloc[1]:.0f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("recording", nargs="?", type=Path)
    parser.add_argument("--laps", type=int, default=52)
    args = parser.parse_args()
    if args.recording is not None:
        _measure_packets(args.recording)
        asyncio.run(_measure_replay(args.recording))
        return
    with TemporaryDirectory() as directory:
        path = Path(directory) / "synthetic.f1bin"
        spec = RaceSpec(laps=args.laps, track_id=7, session_type=15)
        write_packet_stream(path, race_stream(spec), session_uid=spec.session_uid)
        _measure_packets(path)
        asyncio.run(_measure_replay(path))


if __name__ == "__main__":
    main()
