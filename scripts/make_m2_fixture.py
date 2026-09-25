#!/usr/bin/env python3
"""Regenerate ~/m2-fixture.f1bin: a Q1 garage scenario that demonstrates
release_hold -> release_go, plus 20-car session history for the abort rows.

Usage: uv run python scripts/make_m2_fixture.py [OUT]   (default ~/m2-fixture.f1bin)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pitwall.protocol.header import PacketId  # noqa: E402
from synth import pack_packet, write_packet_stream  # noqa: E402

TRACK_M = 5000.0
DT = 0.2  # 5 Hz


def _frames(t0: float, t1: float, rival_dist: float, rival_lap_ms: int) -> list:
    """Q1 garage frames: player in garage, rival flying at `rival_dist`
    lap-distance with the given best lap."""
    out = []
    n = int((t1 - t0) / DT)
    for i in range(n):
        t = t0 + i * DT
        out.append(
            (
                t,
                pack_packet(
                    PacketId.SESSION,
                    {
                        "session_type": 5,
                        "session_time_left": 720,
                        "track_length": 5000,
                    },
                    session_time=t,
                ),
            )
        )
        out.append(
            (
                t,
                pack_packet(
                    PacketId.LAP_DATA,
                    {
                        "cars": {
                            0: {
                                "driver_status": 0,
                                "pit_status": 0,
                                "current_lap_num": 1,
                            },
                            1: {
                                "driver_status": 1,
                                "pit_status": 0,
                                "result_status": 2,
                                "lap_distance": rival_dist,
                            },
                        }
                    },
                    session_time=t,
                ),
            )
        )
        # Rival history arrives every frame so field_best_laps[1] is fresh.
        out.append(
            (
                t,
                pack_packet(
                    PacketId.SESSION_HISTORY,
                    {
                        "car_idx": 1,
                        "num_laps": 1,
                        "laps": {0: {"lap_time_ms": rival_lap_ms, "lap_valid_bit_flags": 0x01}},
                    },
                    session_time=t,
                ),
            )
        )
    return out


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "m2-fixture.f1bin"

    stream: list[tuple[float, bytes]] = []

    # Phase 1 (0-34 s): rival on a 95 s best lap positioned so it is ~2 s of
    # gap ahead of the player's projected pit-exit arrival -> release_hold
    # fires with ~6-10 s wait. Player out lap derived as 1.3x95 = 123.5 s at
    # 40.5 m/s; a rival at 3400 m arrives at pit exit ~2 s before the player.
    stream += _frames(0.0, 34.0, 3400.0, 95_000)

    # Phase 2 (34-45 s): rival jumps half a lap away -> clean air -> release_go
    # fires once the shared 30 s "release" group cooldown has elapsed.
    stream += _frames(34.0, 45.0, 2500.0, 95_000)

    # Session history for 19 rivals so the abort advisory has a field.
    for i in range(2, 20):
        lap_ms = 89_000 if i <= 15 else 92_000
        stream.append(
            (
                i * 0.05,
                pack_packet(
                    PacketId.SESSION_HISTORY,
                    {
                        "car_idx": i,
                        "num_laps": 1,
                        "laps": {0: {"lap_time_ms": lap_ms, "lap_valid_bit_flags": 0x01}},
                    },
                    session_time=i * 0.05,
                ),
            )
        )

    stream.sort(key=lambda p: p[0])
    path = write_packet_stream(out, stream)
    print(f"wrote {path}")

    # Index sidecar for lap-skips/duration.
    from pitwall.cli import build_parser

    args = build_parser().parse_args(["index", str(path)])
    args.func(args)


if __name__ == "__main__":
    main()
