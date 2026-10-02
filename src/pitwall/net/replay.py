"""Replay: stream a recording through the same PacketSink as the live socket.

recv_time passed to the sink is always the record's own timestamp in seconds,
so ingest sees identical data at any speed. speed=None means "as fast as
possible" (no sleeps); a numeric speed paces via the Clock (a VirtualClock's
sleep returns instantly, so tests stay deterministic).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from pitwall.clock import Clock
from pitwall.net.recording import RecordingReader
from pitwall.net.udp import PacketSink
from pitwall.voice.arbitrator import RecordedPick

_SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")


async def replay(
    path: Path,
    sink: PacketSink,
    clock: Clock,
    speed: float | None = None,
    *,
    from_us: int | None = None,
    to_us: int | None = None,
) -> int:
    """Feed records to sink. Returns the number of datagrams delivered."""
    delivered = 0
    with RecordingReader(path) as reader:
        for offset_us, payload in reader:
            if from_us is not None and offset_us < from_us:
                continue
            if to_us is not None and offset_us > to_us:
                break
            t = offset_us / 1_000_000
            if speed is not None:
                # Pacing in record time: the clock does the speed scaling.
                delay = t - clock.now()
                if delay > 0:
                    await clock.sleep(delay)
            sink.on_datagram(payload, t)
            delivered += 1
    return delivered


def arbitration_records(paths: Iterable[Path]) -> Iterator[dict[str, Any]]:
    """`arbitrated` decision-log records from decision logs (JSONL) or a SQLite DB."""
    from pitwall.store.db import Database

    for path in paths:
        if not path.is_file():
            continue
        if path.suffix in _SQLITE_SUFFIXES:
            try:
                yield from Database(path).arbitrations()
            except sqlite3.Error:
                continue
            continue
        with path.open(encoding="utf-8") as fp:
            for line in fp:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and record.get("outcome") == "arbitrated":
                    yield record


def load_recorded_picks(paths: Iterable[Path]) -> dict[str, RecordedPick]:
    """Live arbitration picks by decision key, for a replay to repeat (ADR 0010)."""
    picks: dict[str, RecordedPick] = {}
    for record in arbitration_records(paths):
        key = record.get("arb_key")
        if isinstance(key, str) and key:
            picks[key] = RecordedPick.from_record(record)
    return picks
