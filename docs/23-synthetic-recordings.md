# Synthetic recordings

This document covers derived recordings and generated field races for rule tests.
Derived recordings alter a real race. Field generation creates a full field from explicit priors.
Synthetic recordings must not change the physics priors.

_Spike run: 7 Oct 2026, on the Brazil race recording from 6 Oct._

## What was built

`pitwall derive` reads a `.f1bin` or `.f1bin.zst` recording and writes a mutated copy.
The mutation code is in `src/pitwall/derive.py`. Each mutation rewrites bytes at known offsets, the same way `net/mask.py` does. Malformed and unrelated packets pass through unchanged.

```
pitwall derive <in> <out> [--wear-scale X] [--inject-sc START[-END]] [--vsc] [--penalty LAP]
```

- `--wear-scale X` multiplies Car Damage `tyres_wear` and `tyres_damage` for every car. The values stay between 0 and 100.
- `--inject-sc START[-END]` adds a safety car from lap START to lap END (default START+2). For those laps, `safety_car_status` in the Session packet is 1, or 2 with `--vsc`. The command adds three `SCAR` events: deployed, returning and returned. It also multiplies Lap Data `last_lap_time_ms` by 1.4 for laps that end inside the window.
- `--penalty LAP` adds a 5 s `PENA` event for the player. It also adds 5 to the player's Lap Data `penalties` value from that lap on.
- `--rival-pit` was not built. See [Breaking mutations](#breaking-mutations).

### Identity

A derived session has a new session UID. The top 24 bits are the tag `0xF1DE57`. The low 40 bits are a hash of the source UID and the mutation list, so the same derive command always gives the same UID.
`derive` writes the new UID to the file header and to every packet header, because the engine reads the UID from packet headers.
The spec asked for the top bit as the tag. That does not work, because game UIDs are random 64-bit values and about half of them have the top bit set.
A real UID matches the 24-bit tag about once in 16 million sessions.

The file header metadata gets these fields:

- `synthetic: true`
- `derived_from`: the source session UID
- `derived_from_path`: the source file
- `mutations`: the list of mutations, for example `["inject_sc=6-8"]`

A new migration adds `synthetic` and `derived_from` columns to `sessions`. Ingest and `pitwall replay --seed-db` set them from the metadata. If the metadata is missing, the UID tag still marks the session as synthetic.

### Learning split

| Path | Synthetic sessions |
|---|---|
| `pitwall digest`, hindsight grading | Included |
| `pitwall tune` | Auto outcomes and press grades skipped. Human grades kept |
| `pitwall calibrate` | Excluded. `--include-synthetic` includes them |
| Live folds during replay: pit loss, fuel burn, stint degradation and base pace, battle pass and hold rates | Skipped |
| Stint rebuild in upkeep (`learning_stints`) and the weekend practice prior (`weekend_stints`) | Excluded |
| Setup learning (`fold_setup_learning`) | Skipped |
| Press grades from replayed presses | Skipped. The decision log still has the presses |
| `pitwall stats --quality` and the quality line in `pitwall start` | Excluded. The debrief CALL QUALITY card still shows |
| Learning pack track ledger and track minutes | Excluded |
| Learning pack grades | Human grades kept. Press grades dropped |
| Question mining (`pitwall propose`) | Excluded |

`calibrate` is not the only way into the priors. The engine folds pit loss, fuel, degradation and battle rates while it replays a session, so those folds also check the synthetic flag.
A derived recording keeps the source's real physics. Without these checks, each variant would fold the same real laps again.

A replayed press answers a call from the real race. In a synthetic session it can land on a call that only the mutation created, so pitwall does not grade from it.

The engine treats a session as synthetic if the recording metadata has `synthetic`, or if the UID has the reserved tag. It reads the metadata before the first packet. A generator that keeps a plain UID still cannot fold priors.

### Where the flag shows

- `pitwall sessions` has `syn` and `derived_from` columns.
- `pitwall recordings` adds `synthetic` after the UID.
- The `/debrief` index has an Origin column with a "synthetic" chip and the source UID. The session page lists them under provenance.
- `pitwall stats --learned` prints `N sessions (M synthetic, excluded from priors)` for each track.

## End-to-end run

The source is the Brazil race recording from 6 Oct: track 16, 17 laps, one stop at the end of lap 11, no safety car. Recordings stay out of git (ADR 0005).
Each variant was ingested into its own empty database, so every run started from the same default priors.

| Variant | Calls fired | Rules that did not fire in the source | Other changes |
|---|---|---|---|
| Source | 64 | | Window 7 to 13. Driver boxed on lap 11 |
| `--inject-sc 6-8` | 63 | `sc_deployed` L6, `sc_ending` L8, `sc_restart` L9, `fuel_spare` L13 | Window opens on lap 10, not lap 9. Lap 9 `tyre_life` says 3 laps, not 5 |
| `--inject-sc 10-11` | 65 | `sc_deployed` L10, `sc_ending` L11, `fuel_spare` L15 | The real stop on lap 11 now falls under the safety car |
| `--wear-scale 1.6` | 64 | None | No call changed |
| `--wear-scale 3` | 67 | `box_now` L8 and L16 | Window 6 to 11. Extra `tyre_life` and `menu:pit` lines |

All four variants replayed and digested without errors.
Calibration (dry run) on a database with all four sessions used 1 session and 14 laps by default and reported `synthetic_skipped: 3`. With `--include-synthetic` it used 4 sessions and 51 laps.
Replaying the source alone folded fuel, degradation, base pace, setup baseline and battle values. Replaying `--inject-sc 6-8` alone folded no physics prior. The battle folds were the last gap and are now skipped as well.

`pitwall tune` found nothing to tune in either the source or the variants. All hindsight outcomes in this race were `censored`, `n/a`, `ignored` or a plan outcome, and `tune` skips those. Unit tests check that `tune` reads synthetic sessions. This race does not show it.

Ingest order changed the calls. When the variants went into the same database after the source, they planned with the source's learned degradation. In that run the window was 10 to 13, and `--inject-sc 6-8` called `box_now` on lap 9.
The exclusion policy blocks the other direction: a synthetic session never changes the priors that a later real session uses.

## Consistency audit

### Working mutations

- **Safety car status, events and lap times.** Session status, the three `SCAR` events and the longer lap times agree with each other. The lap rows for the window have `sc_status` 1 and are not valid laps, so green-lap fits skip them. The `sc_*` rules fired in order.
- **Penalty.** The `PENA` event and the Lap Data `penalties` value agree. Only unit tests cover this. The real-data run did not use it.
- **UID rewrite.** Every packet carries the new UID, so the engine, the database and the debrief see one session.
- **Lap after the window.** `derive` sends a green Session packet before the first Lap Data packet of the next lap. With `--inject-sc 6-8`, lap 8 is a safety-car lap and lap 9 is valid and green.

### Breaking mutations

Safety car injection leaves these fields at their green-flag values:

- Positions, gaps, lap distance, speed, sector times and the current lap time.
- Telemetry, tyre temperatures, fuel burn and ERS. Car Status keeps the same fuel mix and ERS mode, so the driver does not seem to react to the `SCAR` events.
- Session History lap times. Rival pace still reads as green.
- The field does not bunch up and nobody pits under the safety car.
- A real stop that now falls inside the window measures green-flag pit loss but would be labelled as safety-car pit loss. That is why the pit-loss fold is skipped.

Wear scaling has these problems:

- `tyres_age_laps` and the lap times stay the same. Wear grows faster but pace does not drop, so wear and the degradation fit disagree.
- Every value is scaled, including a new set. With `--wear-scale 3` the new tyres read 21% worn at age 0. In the source they read 7%. A better version would scale only the wear gained since the stint started.
- Session History tyre stints and Car Status tyre fields stay the same.
- At 1.6 nothing changed, because pace-cliff life was shorter than wear life. Tyre life is the lower of the two.

Some problems apply to every mutation:

- The recorded driver does not react. Hindsight graded `box_now` as `ignored` for lap 8 and lap 16 with `--wear-scale 3`, and for lap 9 in the shared-database run. Those grades describe the source race, not a decision made in the new scenario.
- `--rival-pit` was not built. One rival stop would need changes to Lap Data `pit_status`, the pit timers, positions and gaps for every car, the Session History stints, and the Car Status and Car Damage tyre fields, all kept in step over two laps. It would be the least consistent mutation.

## Should the exclusion become a down-weight?

No. The rules get consistent signals, but the physics in each variant is the source's physics with some fields changed. Pace, gaps, positions and the relation between wear and age stay as they were in the source.
A down-weighted variant would also count the source's real laps a second time. Keep the hard exclusion for priors.

Keep synthetic sessions in `tune`, with one follow-up. `tune` should discount outcomes that depend on what the driver did, such as `stop_taken`, in synthetic sessions. Today `tune` skips `ignored` outcomes, so this race showed no harm.

## Field generator

`pitwall generate` writes seeded full-field recordings with consistent car status, pit stops, tyre history, and safety-car gaps.
Use `--priors-db` or `--priors-json` to supply race pace and pit-loss values.
Use `--jobs` to generate separate races in parallel.

The generator is for rule development and replay testing. It does not replace recorded races or calibrate trustworthy priors.
Keep `pitwall derive` for quick checks of the same race with an event at a different time.

| Trust | Use |
|---|---|
| Trust for synthetic replay and packet consistency | Check rule timing, bench order, and recording ingestion. |
| Treat as an estimate for strategy comparisons | Compare candidate behavior under the generator's stated assumptions. |
| Do not trust for real pace, tyre wear, or pit-loss values | Calibrate those values from real recordings. |
| Do not treat synthetic bench results as a real gate | Confirm a change against real recordings before approval. |

See [LOCAL_RUN.md](../LOCAL_RUN.md) for Windows commands and result fields.

The race used here had no safety car and one stop. A longer real race with a safety car and two stops would test these results better. You may need to record one.

## Known-answer check

`scripts/known_answer.py` tests the estimators against races whose physics we set. It builds each race with `tests/race_synth.py`, ingests it into its own temp database and runs `calibrate`. Then it prints each learned value next to the true value. Nothing touches the configured `pitwall.sqlite` or `recordings/`.

```text
uv run python scripts/known_answer.py --races 8 --pooled 4 --jobs 5 --json out.json
```

- Each single run is one race with one stint on C17 and one on C18, with pit-lane time, lap noise and tyre temperatures that cross the thermal window.
- The pooled run puts 4 races with the same physics and different pit laps into one database. Different pit laps put the same tyre age at different fuel loads, so calibrate can separate fuel from tyre wear.
- `deg*_net` is deg minus the fuel slope the fit assumed. A single stint can only measure this lap-time slope, so compare live fits on it.

The races are synthetic, so they test the estimators and do not supply priors for real races. `scripts/make_synth_race.py` writes one such race to `recordings/` for a manual `pitwall digest`.

The [scenario bench](24-scenario-bench.md) scores real and synthetic scenarios against a baseline gate.
