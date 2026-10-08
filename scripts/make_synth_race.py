#!/usr/bin/env python3
"""Write a synthetic Silverstone race recording to recordings/ for the learning loop.

52 laps, one stop from C17 to C18 with pit-lane time, lap noise, deg and fuel
burn, so `pitwall digest` and `pitwall calibrate` have deg, pit-loss and fuel
data to fit.

Usage: uv run python scripts/make_synth_race.py [OUT] [--unflagged] [--uid HEX]
  --unflagged  write {"synthetic": false} so priors fold (control run only)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from tests.race_synth import RaceSpec, race_stream  # noqa: E402
from tests.synth import write_packet_stream  # noqa: E402

SILVERSTONE = 7
LAPS = 52


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default=None)
    ap.add_argument("--unflagged", action="store_true")
    ap.add_argument("--uid", default="5E1F0752C0FFEE01")
    args = ap.parse_args()
    uid = int(args.uid, 16)
    default_out = ROOT / "recordings" / f"synth_silverstone_{uid:016x}.f1bin"
    out = Path(args.out) if args.out else default_out
    out.parent.mkdir(parents=True, exist_ok=True)
    spec = RaceSpec(
        laps=LAPS,
        track_id=SILVERSTONE,
        session_type=15,
        session_uid=uid,
        base_ms=88_000,
        deg_ms=85,  # C3 (compound 17); the track file says 70
        wear_pct_per_lap=1.6,
        fuel_kg=95.0,
        fuel_kg_per_lap=1.8,  # track file says 1.75
        fuel_ms_per_kg=30.0,
        player_pit_lap=24,
        pit_lane_loss_ms=21_500,
        compound_after_stop=18,
        deg_ms_after_stop=45,
        lap_noise_ms=120.0,
        seed=1,
        rival_pit_lap=22,
        compound=17,
        finish=True,
        send_session_end=True,
    )
    meta = {"synthetic": not args.unflagged, "source": "scripts/make_synth_race.py"}
    path = write_packet_stream(out, race_stream(spec), session_uid=uid, metadata=meta)
    from pitwall.net.recording import ensure_index

    ensure_index(path)
    print(f"wrote {path} uid={uid:#x} synthetic={meta['synthetic']}")


if __name__ == "__main__":
    main()
