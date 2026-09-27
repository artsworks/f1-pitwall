# Learning loop

How pitwall gets better from each session without repeating the same mistakes.
Live decisions stay deterministic (ADR 0008). Learning only changes config, model
parameters and tests, and only through the gates below.

```
race ──► recording + SQLite facts (laps, stints, pit_events, calls, plan_events)
      ──► 1. hindsight grader   automatic outcome label on every fired call / plan event
      ──► 2. session digest     ~5–10 KB JSON per session: the compressed memory
      ──► 3. lessons ledger     claims with evidence; hypothesis → confirmed → applied
      ──► 4. promotion gate     corpus replay A/B must improve, must-fire calls kept
      ──► 5. regression fixture trimmed slice + replay test per confirmed mistake
      ──► learned overlay / model_params / rules PR ──► next race
```

Phases: **L1** hindsight grader + digest (built, below). **L2** lessons ledger, corpus-scored
`diff` gate, `tune` for bounded thresholds and priors, trim-to-fixture. **L3** battle state and
push / defend / manage / encourage coaching with a learned pass model. **L4** debrief §07
actions from the ledger; optional offline LLM analyst that only *proposes* lessons.

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
pitwall digest [--db PATH] [--session UID|latest] [--out DIR|-] [--json]
```

Runs the grader, prints the top findings and writes `~/.pitwall/digests/<uid>.json`
(version 1):

- `session`: uid, track, type, mode, weather, config hash, laps
- `pace`: valid green laps, best, median, stdev
- `stints`, `stops`, `pit_events` (loss, neutralised)
- `strategy`: executed sequence (e.g. `M-H`), plan events, stop costs
- `model`: mean signed errors for lap time, laps of pace, fuel margin; mean green pit loss
- `outcomes`: counts per label
- `calls`: per rule: fired / suppressed / ack / neg / human good or bad / auto good or wrong
- `bookmarks`, `findings`: deterministic templates, most costly first, at most
  `digest_max_findings`

A digest contains no raw telemetry. Recordings stay out of git (ADR 0005); digests are small
enough to keep forever and to diff between sessions.

## 3–5. Lessons, promotion, fixtures (L2)

A lesson is a claim with a scope (track / compound / rule), a metric, the digests that
support it, the effect size and spread, and a status. "Conclusive" means at least
`lesson_min_sessions` sessions where the effect has a consistent sign and the spread
excludes zero. Contradicting evidence moves a lesson back to `hypothesis`, or to `retired`.

A confirmed lesson becomes a candidate change. It's promoted only if
`pitwall diff --corpus`, scored on hindsight labels, improves and no must-fire call is lost
(SC, box, fuel short, penalty). Bounded parameter moves go to the learned overlay with the
lesson id as provenance; rule or logic changes become a PR. Each confirmed mistake gets a
`pitwall trim` slice and a replay test asserting the corrected call.

## After a race

1. Record with the `full` profile (review mode needs it).
2. Bookmark every wrong, late or missing call (long press with `input.long_press: bookmark`, docs/12).
3. `pitwall digest`, then grade the pit and plan calls in review mode.
4. `pitwall tune` to fold grades and outcomes into the rule cooldowns.
