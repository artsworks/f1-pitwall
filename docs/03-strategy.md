# Strategy engine

## Rules as data

Every call is a rule definition, not a branch in a Python file. The engine evaluates all
enabled rules for the current session type against the 10 Hz snapshot.

```yaml
- id: front_left_overheat
  sessions: [race]
  priority: 2
  when: "tyre_core_ema_slow.FL > th.core_overheat"
  clear_when: "tyre_core_ema_slow.FL < th.core_overheat - 5"   # hysteresis
  cooldown_s: 45
  max_per_stint: 3
  requires: [car_telemetry, car_damage]
  say: "Front left at {tyre_core_ema_slow.FL:.0f} degrees. Ease the trail braking."
  still_true: "tyre_core_ema_slow.FL > th.core_overheat - 3"   # revalidated at speak time
```

Fields:

| Field | Purpose |
|---|---|
| `when` / `clear_when` | trigger and release predicates — separate values give hysteresis, which is what stops oscillating calls |
| `priority` | 1 critical, 2 tactical, 3 informational |
| `cooldown_s`, `max_per_stint`, `min_lap` | rate limiting per rule |
| `conflict_group` | Keep only queued calls in the group whose current predicate still holds. |
| `supersedes` | Drop queued calls for the listed rule ids when this rule arrives. |
| `contradicts` | Never speak both. Of the two the higher priority speaks, then the higher urgency class, then the newest call. Declared on either rule. |
| `requires` | packets that must be fresh; the rule is skipped (not fired on stale data) otherwise |
| `say` | phrasing template, or a list of variants rotated without repeats (first call uses the first) |
| `escalate`, `repeat_window_s` | `[{after: N, say: [...]}]`: a different pool once the call has triggered N times inside the window; `{repeat}` is the count (ADR 0008) |
| `still_true` | revalidated immediately before speaking; deliberately looser than `when` |
| `screen_only` | show on the dashboard without speaking |

Predicates are evaluated in a restricted expression environment exposing only the
snapshot, thresholds (`th`), the active mindset's parameters (`mode`, see
`08-configuration.md`), and a small function library. Every rule gets a replay test:
a recording plus the expected set of firings.

## Session state machine

Derived from `m_sessionType`, with the corrected mapping:

- 1–4 practice, 5–9 qualifying, 10–14 sprint shootout (use the quali rule set),
  15–17 race, 18 time trial.

Plus a phase within the session, driven by `m_driverStatus` and `m_pitStatus`:
`garage → out_lap → flying → in_lap`, and in race: `formation → racing → sc/vsc → in_lap
→ out_lap → finished`.

## Lap validity

A lap enters the pace model only if all hold: not lap 1; `m_pitStatus == 0` throughout;
not the lap after an in-lap; `m_safetyCarStatus == 0` throughout; no weather transition;
`m_currentLapInvalid == 0`; no flashback during the lap. Otherwise it is still recorded,
tagged with why, so the debrief can show it.

## Pace and degradation model

The model lets the engine answer "box this lap or next". Thresholds cannot.

```
lap_time_ms = base_ms
            + deg_ms_per_lap · tyre_age_laps
            + deg2_ms_per_lap2 · tyre_age_laps²
            - fuel_ms_per_lap · fuel_laps_burned
```

- The engine excludes majority-traffic laps and laps above `thermal_hi_c`. Unknown tyre temperatures remain eligible.
- A race lap counts as traffic when over half its ticks fall within `traffic_gap_s` of the car ahead.
- The quadratic fit needs eight clean laps and four distinct tyre ages. It keeps fuel fixed and accepts significant positive curvature.
- The engine solves `deg * age + deg2 * age² = tyre_cliff_ms`. Quadratic fits use the positive root for `cliff_age`.
- Short fits use or blend toward a prior. The planner shrinks both degradation terms by fit error.
- The planner multiplies deterministic plan confidence by Monte Carlo agreement. It keeps `plan.lap` unchanged and does not scale `cheap_stop`.
- Race priors are scoped by total race laps, so different race distances do not mix.
  Practice values are unscoped.
- Rival pace comes from Session History lap times, filtered the same way.

Report results in driver currency: "about three laps of life left at this pace" or
"the undercut is worth 1.2 seconds", never a raw coefficient.

## F1 26 calibration

The current defaults use these F1 26 race values:

- `fuel_kg_per_lap_default` is 1.2 kg per lap.
- `energy_over_tolerance_j` is 400 kJ.
- `battle_pass_overtake_prior` is 0.6.
- `battle_pass_no_overtake_prior` is 0.6.

Silverstone's track overlay uses `fuel_kg_per_lap: 1.26` and
`energy_over_tolerance_j: 600000`.

These settings stay unchanged:

- `cliff_ms_per_lap`, `tyre_cliff_ms`, and `wear_corner_*`. No stint reached the cliff or showed uneven wear.
- Degradation remains unchanged. Calibrate reports `not identifiable` with one stint per compound.
- Tyre inner windows remain unchanged. The Silverstone fit uses three laps on one compound.
  Interlagos's windows already come from a larger database.
- `battle_defend_gap_s` stays at 1.0 s, matching the 1.0 s overtake detection gap.
- `slow_car_*` stays unchanged because the 2026 rules do not change qualifying behavior.
- Pit loss stays unchanged because the sample has one stop per track.

Refit these values after recording more F1 26 races with `pitwall calibrate` and
`pitwall propose`.

## Pit loss

Measure, do not assume, pit loss. For each stop, combine `m_pitLaneTimeInLaneInMS` with
the in/out-lap pace delta versus rolling green-flag pace. Store the result per track and
neutralisation state. The hardcoded table is only a cold-start prior. Key it by the
**sparse, non-contiguous** track IDs (see `reference/f1-26-udp-notes.md`).

Neutralisation multipliers (green ≈ 1.0, VSC ≈ 0.55, full SC ≈ 0.45 of green loss) are
priors to replace with measurements.

## Race calls

**Pit window.** For each candidate pit lap in the next N laps, project your race time to a
horizon with and without stopping, using measured pit loss, the deg model, and rival pace
from Session History; recommend the lap with the best projected net position. Re-evaluate
each lap and only *speak* when the recommendation changes materially or the window is
about to close. The minimum gain required to recommend a stop and the tolerated
probability of losing a place come from `mode.pit_gain_min_s`,
`mode.pit_confidence_min`, and `mode.position_loss_risk_max`. These settings change the
appetite, not the projection.

Every projection carries a confidence. Below the configured floor the engine says "not
enough data yet" rather than a number; a model that is confidently wrong is worse than
no model.

**Undercut / overcut vs. a specific rival.** Same projection, restricted to the target
ahead and the chaser behind.

The optimiser sets `undercut_s` to zero unless
`laps_remaining > th.undercut_laps + th.pit_min_laps_left`.
The optimiser sets `overcut_s` to zero unless `laps_remaining > th.overcut_laps` and a stop remains possible.
These limits suppress values and plans when too few laps remain to exploit the tyre advantage.
When `laps_remaining <= th.pit_min_laps_left`, the optimiser returns `no_stop` with both values at zero.
The `rival_ahead_pitted` rule also stays silent at this limit.

**Cover the undercut.** When the car behind pits, the optimiser compares its projected fresh-tyre gain with our gap. It calls us to box if the rival could jump us, or stay out when the gap is safe.

**Free stop.** A position-neutral stop is available when the gap to the car behind exceeds
measured pit loss.

**Cheap stop.** If the `SCAR` event reports a safety car or VSC and current tyre wear
justifies a stop, recommend boxing now and quantify the saving.

**Rival scope.** Track at most three rivals: the car ahead, the car behind (only while the
gap is under 5 s), and the projected pit-exit rival. The projected rival's track position
lands near your pit-exit point, computed as
`D_target = (D_player − L_track · T_pit_loss / P_rolling) mod L_track`. Purge a missing
rival only after two consecutive missing updates or `m_resultStatus >= 3`.

**Tyre and thermal.** Use the slow (30 s) core EMA with hysteresis for overheat and graining
warnings. Use Car Damage for blister percentage, and phrase wear as remaining laps of
pace. Tyre life is the earlier of the worst corner's wear limit and the pace cliff from
the degradation fit. Choose the next-stint compound from the sets reported by Tyre Sets.

**Fuel.** Compare `m_fuelRemainingLaps` with laps remaining to advise lift-and-coast or
fuel-mix changes. Phrase the call as "you need half a lap of fuel saving over the next five".

**Energy.** Compare ERS store with `m_ersHarvestLimitPerLap` and `m_ersDeployedThisLap`
to advise deployment. The 2026 Overtake Mode uses `m_overtakeAvailable` and
`m_overtakeActivationDistance` to make "overtake available in 200 metres" a precise,
actionable call. Under the 2026 regulations, `m_drsAllowed` and `m_drsActivationDistance`
are always 0 and the `DRSE`/`DRSD` events never fire. DRS logic runs only on pre-2026 data.

**Discipline.** Track `m_cornerCuttingWarnings` against the penalty threshold, blue flags,
unserved penalties, and damage-driven pace loss.

**Weather.** Filter forecast samples to the current session type and order them by
`m_timeOffset`. Use `m_rainPercentage` to call the crossover lap for inters/wets and warn
on the transition, not only on the change.

## Strategy plans (Plan A/B/C)

**How real pit walls talk strategy.** Teams pre-brief a few named strategies and refer to
them by name on the radio. The radio is public and bandwidth is limited. Common patterns:

- *Lettered plans.* Ferrari famously name them "Plan A / B / C" ("we are on Plan C",
  "let's go Plan F" is folklore); Mercedes and Red Bull say "Plan B" / "we're switching to
  Plan B" or "option B". The driver knows what each letter means from the briefing, so the
  call carries status, not detail.
- *Status confirmations.* "We're on Plan A", "still Plan A", "target lap 26", "pit window
  is open", "window laps 26 to 28".
- *Execution words.* "Box, box" (pit this lap), "box, confirm" / "stay out, stay out",
  "extend" / "we're going long" (lengthen the stint), "box opposite" (react to a rival).
- *Pace instructions tied to strategy.* "Hammer time" (Mercedes, push flat out for the
  undercut/overcut), "manage the tyres", "we need 26 laps from this set".
- *Engine/strat modes.* "Strat 3", "Strategy 5" and similar are numbered power-unit
  modes rather than race strategies; pitwall does not reuse the numbering.
- *Reactive plans.* Under SC/VSC the pre-briefed plan is "Plan C, box box" or "safety
  car, box this lap", because the neutralised pit loss changes the answer instantly.

**Model.** `pitwall.strategy.plans` enumerates every legal compound sequence with 0–2 stops
(`plan_max_stops`). It uses sets left per compound from the Tyre Sets packet (unknown =
unlimited), the mandatory two-dry-compound rule (`plan_two_compound_rule`), and
`plan_min_stint_laps`. It finds each sequence's best stop laps with the optimiser's
degradation model and pit loss. Fresh-set pace per compound uses a learned prior or the
current slope scaled by `plan_deg_factor_*`, offset by `plan_pace_*_ms`. The window
includes each first-stop lap within `plan_window_s` of the best.

- **Plan A** = fastest sequence at race start; **Plan B** = fastest with a different stop
  count (else a different sequence); **Plan C** = reactive: box this lap at the SC/VSC pit
  loss, re-derived every recompute until taken.
- A and B are frozen as compound sequences; their timing is re-optimised every lap. If the
  active plan falls more than `plan_off_s` behind the best legal strategy we're *off plan*
  (hysteresis `plan_off_hysteresis_s`); beyond `plan_switch_s` the engine switches to the
  plan that matches the best sequence (reason `pace`). A plan becomes *invalid* when its
  remaining sequence is no longer legal (wrong tyre fitted, sets gone, SC stop not taken).
- A cheap-stop SC/VSC recommendation switches the active plan to C (reason `sc`).
- After a pit stop, the tracker counts the stop on entry to the out-lap and rebases the
  plan from the new compound, tyre age, laps left and stops completed.

**Calls** (`rules/race.yaml`, all through the dispatcher budget/cooldowns):

| rule | P | example |
| --- | --- | --- |
| `plan_announce` | 2 | "We're on Plan A, one stop, medium then hard. Window laps 26 to 28. Plan B is two stop, medium then hard then soft" |
| `plan_announce_no_stop` | 2 | "We're on Plan A, no stop, hard to the end. We're going long" |
| `plan_status` | 3 | every `plan_status_every_laps` in sector 1 before the window: "Still on Plan A, window laps 26 to 28" |
| `plan_window_open` | 2 | "Pit window is open. Plan A, target lap 27" |
| `plan_target_lap` | 1 | "Target lap. Plan A, box box, hards" (neg: "Understood, extend") |
| `plan_off` | 2 | "Heads up, we're off Plan A. Losing 4 seconds on it, reviewing" |
| `plan_switch` | 2 | "Switching to Plan B. two stop, medium then hard then soft, target lap 18" |
| `plan_invalid` | 2 | "We're off Plan C. New plan is A, one stop, medium then hard" |
| `box_now` | 1 | now prefixed with the plan: "Box, box. Plan C. Cheap stop under the safety car" |
| `plan_sc_box` | 1 | fallback when `box_now` is gated out: "Plan C, box box. Hards, cheap stop" |

Write every set/switch/off/on transition to the decision log (`outcome: plan`) and
`plan_events` table. Each call row carries `active_plan` and `on_plan` for review grading.

## Qualifying calls

**Out-lap priming.** Use config target windows for each compound and track. Phrase them
as actions: "front left is cold, weave less and drag the brakes through sector three".

**Track evolution and run timing.** Compare the session clock with the expected queue at
pit exit, then advise the release slot.

**Clean-air release.** Project every car's `m_lapDistance` to your pit-exit point over the
expected out-lap time. Report the true window instead of a gap from the classification.

**Abort advisory.** Compare the projected lap time (extrapolated from current sector
deltas) with the time needed to advance, derived from the field's Session History. Weigh
the result against remaining ERS and fresh sets from Tyre Sets. A fixed +0.2 s rule
cannot tell whether you are already comfortably through.

## Persistence

SQLite, one file per install:

- `sessions`: uid, track, type, date, weather, game version
- `laps`: lap summary rows per car, with validity reason
- `stints`: compound, in/out lap, fitted deg parameters
- `pit_events`: measured loss, neutralisation state
- `calls`: every rule firing and every suppression with its reason
- `track_params`: learned pit loss, fuel coefficient, base pace

This makes the second race at a track better than the first. The "< 1 MB memory"
constraint in plan v1 would have prevented it.
