#!/usr/bin/env python3
"""Known-answer check for pitwall's learning estimators.

Generates synthetic races with known deg, fuel, pit-loss and thermal values,
ingests each into its own scratch DB, runs calibrate, and prints learned
against true values. Recordings and DBs live in a temp dir and are deleted.
They never touch the configured pitwall.sqlite or recordings/.

Usage: uv run python scripts/known_answer.py [--races 8] [--seed 1] [--jobs 4] [--json OUT]
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

TRACK = 7
C1, C2 = 17, 18


@dataclass(frozen=True)
class Truth:
    seed: int
    laps: int
    pit_lap: int
    base_ms: int
    deg1_ms: int
    deg2_ms: int
    fuel_kg_per_lap: float
    fuel_ms_per_kg: float
    pit_lane_loss_ms: int
    noise_ms: float
    thermal_lo_c: float
    thermal_hi_c: float
    thermal_penalty_ms: int

    @property
    def fuel_ms_per_lap(self) -> float:
        return self.fuel_ms_per_kg * self.fuel_kg_per_lap


def make_truth(seed: int) -> Truth:
    rng = random.Random(seed)
    laps = rng.choice([24, 36, 52])
    return Truth(
        seed=seed,
        laps=laps,
        pit_lap=round(laps * rng.uniform(0.4, 0.55)),
        base_ms=rng.randrange(85_000, 95_000),
        deg1_ms=rng.randrange(40, 121),
        deg2_ms=rng.randrange(20, 71),
        fuel_kg_per_lap=round(rng.uniform(1.5, 2.0), 2),
        fuel_ms_per_kg=round(rng.uniform(20.0, 40.0), 1),
        pit_lane_loss_ms=rng.randrange(18_000, 24_001),
        noise_ms=rng.choice([0.0, 80.0, 150.0, 250.0]),
        thermal_lo_c=85.0,
        thermal_hi_c=100.0,
        thermal_penalty_ms=300,
    )


def _param(db: Any, compound: int, name: str) -> float | None:
    p = db.get_param(TRACK, compound, name)
    return None if p is None else round(float(p.value), 2)


def _learned(db: Any, laps: int) -> dict[str, float | None]:
    at = f"@{laps}L"
    return {
        "deg1": _param(db, C1, "deg_ms_per_lap" + at),
        "deg2": _param(db, C2, "deg_ms_per_lap" + at),
        "deg1_ref": _param(db, C1, "deg_fuel_ref_ms_per_lap" + at),
        "deg2_ref": _param(db, C2, "deg_fuel_ref_ms_per_lap" + at),
        "base1": _param(db, C1, "base_ms" + at),
        "fuel_ms_per_lap": _param(db, C1, "fuel_ms_per_lap" + at),
        "fuel_kg_per_lap": _param(db, 0, "fuel_kg_per_lap"),
        "pit_loss_green": _param(db, 0, "pit_loss_green_ms"),
        "thermal_lo": _param(db, C1, "thermal_lo_c"),
        "thermal_hi": _param(db, C1, "thermal_hi_c"),
    }


def _spec(truth: Truth, seed: int, race_index: int = 0) -> tuple[Any, int]:
    from tests.race_synth import RaceSpec

    rng = random.Random(seed * 7919)
    temps = tuple(float(rng.randrange(80, 111)) for _ in range(truth.laps))
    uid = rng.getrandbits(62) | 1
    spec = RaceSpec(
        laps=truth.laps,
        track_id=TRACK,
        session_type=15,
        session_uid=uid,
        base_ms=truth.base_ms,
        deg_ms=truth.deg1_ms,
        compound=C1,
        compound_after_stop=C2,
        deg_ms_after_stop=truth.deg2_ms,
        wear_pct_per_lap=1.6,
        fuel_kg=truth.fuel_kg_per_lap * truth.laps + 2.0,
        fuel_kg_per_lap=truth.fuel_kg_per_lap,
        fuel_ms_per_kg=truth.fuel_ms_per_kg,
        player_pit_lap=truth.pit_lap,
        pit_request_laps_early=3,
        pit_box_after_line=race_index % 2 == 1,
        pit_lane_loss_ms=truth.pit_lane_loss_ms,
        lap_noise_ms=truth.noise_ms,
        seed=seed,
        tyre_inner_profile=temps,
        thermal_window_c=(truth.thermal_lo_c, truth.thermal_hi_c),
        thermal_penalty_ms=truth.thermal_penalty_ms,
        finish=True,
        send_session_end=True,
    )
    return spec, uid


def run_one(seed: int, race_index: int = 0) -> dict[str, Any]:
    from tests.race_synth import race_stream
    from tests.synth import write_packet_stream

    from pitwall.calibrate import calibrate
    from pitwall.config.loader import ConfigStore
    from pitwall.ingest import ingest_recordings
    from pitwall.store.db import Database

    truth = make_truth(seed)
    spec, uid = _spec(truth, seed, race_index)
    with tempfile.TemporaryDirectory(prefix="pitwall-ka-") as tmp:
        tmp_path = Path(tmp)
        rec = write_packet_stream(
            tmp_path / f"ka_{seed}.f1bin",
            race_stream(spec),
            session_uid=uid,
            metadata={"synthetic": False, "known_answer": True},
        )
        settings = ConfigStore(isolated=True).current()
        db = Database(tmp_path / "ka.sqlite")
        ingest_recordings(db, [str(rec)], settings, out_dir=tmp_path / "digests", isolated=True)
        live = _with_net(_learned(db, truth.laps))
        pit_rows = [int(e.loss_ms) for e in db.pit_events_for_session(uid)]
        calibrate(db, settings)
        cal = _with_net(_learned(db, truth.laps))
        db.close()
    return {"truth": asdict(truth), "live": live, "calibrated": cal, "pit_events_ms": pit_rows}


def run_pooled(seed: int, races: int) -> dict[str, Any]:
    """One track, one truth, `races` races with different pit laps in one DB.
    Different pit laps put the same tyre age at different fuel loads, which
    is what lets calibrate separate fuel from tyre wear."""
    from dataclasses import replace

    from tests.race_synth import race_stream
    from tests.synth import write_packet_stream

    from pitwall.calibrate import calibrate
    from pitwall.config.loader import ConfigStore
    from pitwall.ingest import ingest_recordings
    from pitwall.store.db import Database

    base = replace(make_truth(seed), laps=52, noise_ms=120.0)
    pit_laps = [16 + round(i * 18 / max(races - 1, 1)) for i in range(races)]
    pit_rows: list[int] = []
    with tempfile.TemporaryDirectory(prefix="pitwall-ka-pool-") as tmp:
        tmp_path = Path(tmp)
        settings = ConfigStore(isolated=True).current()
        db = Database(tmp_path / "ka.sqlite")
        for i, pit_lap in enumerate(pit_laps):
            truth = replace(base, pit_lap=pit_lap)
            spec, uid = _spec(truth, seed * 100 + i, i)
            rec = write_packet_stream(
                tmp_path / f"ka_pool_{i}.f1bin",
                race_stream(spec),
                session_uid=uid,
                metadata={"synthetic": False, "known_answer": True},
            )
            ingest_recordings(db, [str(rec)], settings, out_dir=tmp_path / "digests", isolated=True)
            pit_rows += [int(e.loss_ms) for e in db.pit_events_for_session(uid)]
        live = _with_net(_learned(db, base.laps))
        calibrate(db, settings)
        cal = _with_net(_learned(db, base.laps))
        db.close()
    return {
        "truth": asdict(base),
        "pit_laps": pit_laps,
        "live": live,
        "calibrated": cal,
        "pit_events_ms": pit_rows,
    }


TRUE_KEYS = {
    "deg1": lambda t: t["deg1_ms"],
    "deg2": lambda t: t["deg2_ms"],
    "deg1_net": lambda t: t["deg1_ms"] - t["fuel_ms_per_kg"] * t["fuel_kg_per_lap"],
    "deg2_net": lambda t: t["deg2_ms"] - t["fuel_ms_per_kg"] * t["fuel_kg_per_lap"],
    "base1": lambda t: t["base_ms"],
    "fuel_ms_per_lap": lambda t: t["fuel_ms_per_kg"] * t["fuel_kg_per_lap"],
    "fuel_kg_per_lap": lambda t: t["fuel_kg_per_lap"],
    "pit_loss_green": lambda t: t["pit_lane_loss_ms"],
    "thermal_lo": lambda t: t["thermal_lo_c"],
    "thermal_hi": lambda t: t["thermal_hi_c"],
}


def _with_net(values: dict[str, float | None]) -> dict[str, float | None]:
    """Lap-time slope in a stint (deg minus the fuel slope it assumed). A
    stint fit can only see this, so it is the fair live comparison."""
    out = dict(values)
    for i in ("1", "2"):
        deg, ref = values[f"deg{i}"], values[f"deg{i}_ref"]
        out[f"deg{i}_net"] = None if deg is None or ref is None else round(deg - ref, 2)
    return out


def summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for stage in ("live", "calibrated"):
        rows: dict[str, Any] = {}
        for key, true_of in TRUE_KEYS.items():
            errs = [
                r[stage][key] - true_of(r["truth"]) for r in results if r[stage][key] is not None
            ]
            rows[key] = {
                "n": len(errs),
                "missing": len(results) - len(errs),
                "mean_err": round(statistics.fmean(errs), 2) if errs else None,
                "mean_abs_err": round(statistics.fmean(abs(e) for e in errs), 2) if errs else None,
            }
        out[stage] = rows
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--races", type=int, default=8)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--pooled", type=int, default=4, help="races in the pooled run, 0 = skip")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()
    seeds = [args.seed + i for i in range(args.races)]
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        pooled_future = pool.submit(run_pooled, args.seed, args.pooled) if args.pooled else None
        results = list(pool.map(run_one, seeds, range(args.races)))
        pooled = pooled_future.result() if pooled_future else None
    print(f"{'seed':>4} {'laps':>4} {'param':<16} {'true':>9} {'live':>9} {'calibrated':>10}")
    for r in results:
        t = r["truth"]
        for key, true_of in TRUE_KEYS.items():
            print(
                f"{t['seed']:>4} {t['laps']:>4} {key:<16} {true_of(t):>9.2f} "
                f"{str(r['live'][key]):>9} {str(r['calibrated'][key]):>10}"
            )
        print(f"{t['seed']:>4} {t['laps']:>4} {'pit_events_ms':<16} {'':>9} {r['pit_events_ms']}")
    summary = summarise(results)
    print("\nerror = learned - true")
    for stage, rows in summary.items():
        print(f"[{stage}]")
        for key, row in rows.items():
            print(
                f"  {key:<16} n={row['n']} missing={row['missing']} "
                f"mean={row['mean_err']} mean_abs={row['mean_abs_err']}"
            )
    if pooled is not None:
        t = pooled["truth"]
        print(f"\n[pooled] {args.pooled} races, 52 laps, pit laps {pooled['pit_laps']}")
        print(f"  {'param':<16} {'true':>9} {'live':>9} {'calibrated':>10}")
        for key, true_of in TRUE_KEYS.items():
            print(
                f"  {key:<16} {true_of(t):>9.2f} "
                f"{str(pooled['live'][key]):>9} {str(pooled['calibrated'][key]):>10}"
            )
        print(f"  pit_events_ms    {pooled['pit_events_ms']}")
    if args.json:
        payload = {"results": results, "summary": summary, "pooled": pooled}
        args.json.write_text(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
