# Learning loop

How pitwall gets better from each session without repeating the same mistakes.
Live decisions stay deterministic (ADR 0008). Upkeep and calibration write learned values.
Proposals remain review-only.

Recording imports and startup upkeep use database transactions. A failed import
leaves no partial learning. Calibration stores race pace and tyre wear separately
for each race length. A fit that cannot separate fuel from tyre age does not replace
those priors. `pitwall stats --learned` lists each race length.

```
session ──► SQLite laps, stints, calls and plan events
        ──► grade at session end
        ──► maintain at next start: rebuild clean stint priors, quarantine bad values,
            refit track priors, tune rule cooldowns
        ──► inspect learned state
        ──► optional debrief, evaluation and review-only proposals
```

Learned values are data. A lesson ledger,
automatic promotion and LLM debrief prose are not implemented; LLM prose is deferred
(item 21).

## 1. Hindsight grader (`pitwall.hindsight`)

Labels come from SQLite alone. They're recomputed for the whole session on every run, so
running it again is idempotent. They're stored in the `outcomes` table (migration 6):
`call_id, rule_id, lap, metric, predicted, actual, error, label, detail`.

| Calls | Metric | Label |
|---|---|---|
| `box_now`, `plan_target_lap`, `plan_sc_box` | `stop_taken` | `ignored` when no stop within `hind_stop_window_laps`. A call re-fired within the window with no stop in between is `n/a` (`refired`); only the last call of the run is graded, so one stop counts once |
| same, stop taken | `stop_cost_s` | Fits both stints linearly (fuel-corrected lap time at `fuel_ms_per_lap_default` vs tyre age; the second stint priced from its real starting age), holds total laps fixed, and searches legal in-laps, stretching neither stint more than `hind_extrapolate_laps` past what was driven. Pit loss cancels, so `plan_sc_box`, `pit_plan` cheap/free/undercut stops and stops with SC/VSC on the in- or out-lap are `n/a`. `good` if the actual stop is within `hind_stop_tol_s` of the best |
| any call with `inputs.predicted_lap_ms` | `lap_ms` | vs that lap's actual time (valid green laps only), tolerance `hind_lap_tol_ms` |
| `tyre_life` (`inputs.laps_of_pace`) | `laps_of_pace` | vs laps after the call until fuel-corrected pace stays `tyre_cliff_ms` slower for `hind_cliff_sustain_laps` consecutive green laps, than the stint's fitted age-0 pace (the model's reference, fitted from green laps up to the call). No cliff before the stop or flag: `censored` |
| `fuel_*` (`inputs.fuel_margin_laps`) | `fuel_margin` | vs the final lap's fuel-remaining laps, tolerance `hind_fuel_tol_laps`. `censored` unless the session reached `sessions.total_laps` (migration 7). Kept in the digest as forecast calibration but not fed to `pitwall tune` (the driver's response to the call moves it) |
| plan `set` / `switch` events | `plan_followed` | Did the compounds actually run from that lap match the plan's sequence exactly (a stint's compound is its first lap's)? A plan replaced before any stop is `n/a`; a session that ended early while still on the plan so far is `censored` |

Stops are laps flagged `pitted` next to a tyre change (drive-throughs don't count), else a tyre-age reset. Player laps are always `car_idx` 0; a rival in slot 0 is stored under the player's slot. Labels: `good`, `wrong`, `ignored`,
`censored`, `n/a` (not enough green laps to judge).

`pitwall tune` now also uses `good`/`wrong` outcomes, each weighted `tune_auto_weight` (0.5),
but only for calls nobody graded by hand. A human grade always overrides the automatic one.

## 2. Session digest (`pitwall digest`)

```
pitwall digest [PATHS...] [--calls-mode on|off] [--db PATH] [--session UID|latest] [--out DIR|-] [--json]
```

Writes digest JSON to `~/.pitwall/digests/<uid>.json` and prints findings. With paths,
it ingests recordings first. A digest is optional; live sessions are graded automatically.

- `session`: uid, track, type, mode, weather, config hash, laps
- `pace`: valid green laps, best, median, stdev
- `stints`, `stops`, `pit_events` (loss, neutralised)
- `strategy`: executed sequence (e.g. `M-H`), plan events, stop costs
- `model`: mean signed errors for lap time, laps of pace, fuel margin; mean green pit loss
- `outcomes`: counts per label
- `calls`: per rule: fired / suppressed / ack / neg / human good or bad / auto good or wrong
- `bookmarks`, `findings`: deterministic templates, most costly first, at most
  `digest_max_findings`

A digest contains no raw telemetry. Recordings stay out of git (ADR 0005).

## Automatic upkeep

`pitwall start` runs `maintain()` before rules start. Upkeep does these steps in one
database transaction:

- It rebuilds stint-derived values once per learning version.
- It quarantines invalid active values with a reason.
- It grades sessions that still need grading.
- It refits pace, tyre wear, fuel, thermal and energy priors with the `pitwall calibrate` fit.
- It refolds rule cooldowns with the `pitwall tune` fold. Auto-graded `good` and `wrong`
  outcomes count at `tune_auto_weight`, and a human grade replaces the automatic one.

A session is also graded when it ends. The engine folds pit loss and pass or hold rates
live during the session. The refit drops any value that the quarantine check rejects, so
a quarantined value does not come back. Weights stay at or below `param_weight_cap`.
Upkeep never writes threshold YAML.

Upkeep is idempotent. A second run with no new data changes nothing. Database errors are
logged and skipped. Run upkeep manually with `pitwall maintain`. To skip the refit and
cooldown steps, set `learning.auto_calibrate: false`.

Race stint values are scoped to total race distance. A 52-lap race uses names such as
`deg_ms_per_lap@52L`; another distance does not mix into that prior. If a scoped value
does not have enough weight, the engine falls back to the unscoped value. Non-race stints
fold without a distance suffix.

## Calibration and review

`pitwall calibrate` fits values from stored sessions. `pitwall stats --learned` shows
their values and sources. `pitwall evaluate` reports calls-on and calls-off outcomes by
track; it is descriptive, not a causal comparison. `pitwall propose` writes candidates
for review and does not change active settings. `pitwall calibrate` and `pitwall tune`
run the same steps as upkeep on demand. `pitwall tune` also folds A/B results from
`pitwall diff`.

## After a race

No command is required. Pitwall grades the session when it ends. On the next start,
upkeep refits pace, tyre wear, fuel, thermal and energy priors and adjusts rule cooldowns.
Two steps stay manual by design: grading calls at `/debrief/<uid>` and applying the
threshold YAML changes that `pitwall propose` writes. Rule thresholds never change
without your review. Use `pitwall stats --learned` to inspect learned values. Use `pitwall digest`
only when you want digest JSON or need to ingest external recordings.
