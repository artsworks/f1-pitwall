"""Generate a small deterministic calibration corpus for M4."""

from __future__ import annotations

import argparse
from pathlib import Path

if __package__:
    from .race_synth import RaceSpec, race_stream
    from .synth import write_packet_stream
else:
    from race_synth import RaceSpec, race_stream
    from synth import write_packet_stream


def generate_learning_corpus(
    output_dir: Path,
    *,
    sessions: int = 8,
    laps: int = 8,
    base_ms: int = 90_000,
    deg_ms: int = 120,
    fuel_kg_per_lap: float = 1.7,
    fuel_ms_per_kg: float = 35.0,
) -> list[Path]:
    """Write recordings with repeatable tyre, fuel, and ERS telemetry."""
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    profile = tuple(
        (95.0, 90.0, 130.0, 85.0, 95.0, 130.0, 90.0, 85.0)[lap % 8] for lap in range(laps)
    )
    for index in range(sessions):
        uid = 0xF1260000 + index
        compound = 17 if index < 4 else 0
        spec = RaceSpec(
            laps=laps,
            base_ms=base_ms,
            deg_ms=deg_ms,
            fuel_kg=laps * fuel_kg_per_lap + 5.0 + index,
            fuel_kg_per_lap=fuel_kg_per_lap,
            fuel_ms_per_kg=fuel_ms_per_kg,
            session_type=1 if index < 4 else 15,
            weekend_link=0x26000001,
            session_uid=uid,
            compound=compound,
            tyre_inner_profile=profile if compound == 17 else None,
            tyre_surface_profile=(
                tuple(temperature + 10.0 for temperature in profile) if compound == 17 else None
            ),
            thermal_window_c=(85.0, 100.0) if compound == 17 else None,
            thermal_penalty_ms=800 if compound == 17 else 0,
            ers_deployed_j_per_lap=350_000.0 + index * 25_000.0,
            player_pit_lap=4 if compound == 0 and index % 2 == 0 else None,
            dt=1.0,
            send_session_end=True,
        )
        path = output_dir / f"m4-learning-{index:02d}.f1bin"
        write_packet_stream(
            path,
            race_stream(spec),
            session_uid=uid,
            metadata={"synthetic": False},
        )
        paths.append(path)
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--sessions", type=int, default=8)
    args = parser.parse_args()
    for path in generate_learning_corpus(args.output_dir, sessions=args.sessions):
        print(path)


if __name__ == "__main__":
    main()
