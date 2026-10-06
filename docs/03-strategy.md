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

The model is what lets the engine answer "box this lap or next", which thresholds cannot.

```
lap_time_ms = base_ms
            + deg_ms_per_lap · tyre_age_laps
            + fuel_ms_per_lap · fuel_laps_burned
```

- The engine fits valid, non-neutralised laps in the current stint. Short fits use or blend
  toward a prior. The planner shrinks the fitted slope toward the prior based on fit error;
  clean fits are used as-is.
- Race priors are scoped by total race laps, so different race distances do not mix.
  Practice values are unscoped.
- Rival pace comes from Session History lap times, filtered the same way.

Output to the driver is always in driver currency: "about three laps of life left at this
pace", "the undercut is worth 1.2 seconds", never a raw coefficient.

## Pit loss

Measured, not assumed. On any stop, `m_pitLaneTimeInLaneInMS` plus the pace delta of the
in/out laps versus rolling green-flag pace gives the real loss; store it per track and
per neutralisation state. The hardcoded table is only a cold-start prior, and must be
keyed by the **sparse, non-contiguous** track IDs (see `reference/f1-26-udp-notes.md`).

Neutralisation multipliers (green ≈ 1.0, VSC ≈ 0.55, full SC ≈ 0.45 of green loss) are
also priors to be replaced by measurement.

## Race calls

**Pit window.** For each candidate pit lap in the next N laps, project your race time to a
horizon with and without stopping, using measured pit loss, the deg model, and rival pace
from Session History; recommend the lap with the best projected net position. Re-evaluate
each lap and only *speak* when the recommendation changes materially or the window is
about to close. The margin required before recommending a stop, and the tolerated
probability of losing a place, come from `mode.pit_gain_min_s`,
`mode.pit_confidence_min` and `mode.position_loss_risk_max` — the same projection, a different appetite.

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

**Free stop.** Gap to the car behind exceeds measured pit loss → position-neutral stop
available.

**Cheap stop.** Safety car or VSC deployed (from the `SCAR` event, not polling) and
current tyre wear justifies it → box now, with the saving quantified.

**Rival scope.** Track at most three: the car ahead, the car behind (only while the gap is
under 5 s), and the projected pit-exit rival — the car whose projected track position
lands near your pit-exit point, computed as
`D_target = (D_player − L_track · T_pit_loss / P_rolling) mod L_track`. Purge a missing
rival only after two consecutive missing updates or `m_resultStatus >= 3`.

**Tyre and thermal.** Overheat and graining warnings from the slow (30 s) core EMA with
hysteresis, blister percentage from Car Damage, and wear phrased as remaining laps of
pace. Tyre life is the earlier of the worst corner's wear limit and the pace cliff from
the degradation fit. Compound choice for the next stint is constrained by what Tyre Sets
says you actually have left.

**Fuel.** `m_fuelRemainingLaps` versus laps remaining → lift-and-coast or fuel-mix calls,
stated as "you need half a lap of fuel saving over the next five".

**Energy.** ERS store versus `m_ersHarvestLimitPerLap` and `m_ersDeployedThisLap` →
deployment advice; plus the 2026 Overtake Mode: `m_overtakeAvailable` and
`m_overtakeActivationDistance` make "overtake available in 200 metres" a precise,
actionable call. DRS availability comes from `m_drsAllowed` / `m_drsActivationDistance`
and the `DRSE`/`DRSD` events.

**Discipline.** `m_cornerCuttingWarnings` approaching the penalty threshold, blue flags,
unserved penalties, damage-driven pace loss.

**Weather.** Forecast samples filtered to the current session type, ordered by
`m_timeOffset`, with `m_rainPercentage` — call the crossover lap for inters/wets and warn
on the transition, not merely on the change.

## Strategy plans (Plan A/B/C)

**How real pit walls talk strategy.** Teams pre-brief a small set of named strategies
before the race and then speak in references to them, not in full explanations — the radio
is public and bandwidth is tiny. Common patterns:

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

**Model.** `pitwall.strategy.plans` enumerates every compound sequence with 0–2 stops
(`plan_max_stops`) that is legal: sets left per compound from the Tyre Sets packet
(unknown = unlimited), the mandatory two-dry-compound rule (`plan_two_compound_rule`) and
`plan_min_stint_laps`. Each sequence gets its best stop laps from the same deg model and
pit-loss the optimiser uses (fresh-set pace per compound = learned prior, else the current
slope scaled by `plan_deg_factor_*`, offset by `plan_pace_*_ms`). The window is every first-
stop lap within `plan_window_s` of the best.

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

Every set/switch/off/on transition is written to the decision log (`outcome: plan`) and the
`plan_events` table; every call row carries `active_plan` and `on_plan` for review grading.

## Qualifying calls

**Out-lap priming.** Target windows per compound and track from config, phrased as
actions: "front left is cold, weave less and drag the brakes through sector three".

**Track evolution and run timing.** Session clock versus expected queue at pit exit;
advise the release slot.

**Clean-air release.** Project every car's `m_lapDistance` forward to your pit-exit point
over the expected out-lap time and report the true window, rather than reading a gap off
the classification.

**Abort advisory.** Compare the projected lap time (current sector deltas extrapolated)
against the time needed to advance, derived from the field's Session History, and weigh
it against remaining ERS and remaining fresh sets from Tyre Sets. A fixed +0.2 s rule
cannot know whether you are already comfortably through.

## Persistence

SQLite, one file per install:

- `sessions` — uid, track, type, date, weather, game version
- `laps` — lap summary rows per car, with validity reason
- `stints` — compound, in/out lap, fitted deg parameters
- `pit_events` — measured loss, neutralisation state
- `calls` — every rule firing and every suppression with its reason
- `track_params` — learned pit loss, fuel coefficient, base pace

This is what makes the second race at a track better than the first, and it is what the
"< 1 MB memory" constraint in plan v1 would have prevented.
