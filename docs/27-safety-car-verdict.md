# Safety car verdict: which compound reaches the flag (plan v4)

Status: phase 1 (shadow log) and phase 2 (hindsight grading) are built. Phases 3 to 5 are not built. Plan checked against main at 0.60.6 (`734a02d`) on 2026-11-22.

## Goal

Under SC or VSC, the engine decides between a stop now on a named compound and no stop. The verdict states which dry compound reaches the flag with fighting pace, or that none does.

## What main does today

- `cheap_stop` in `strategy/pitwindow.py` (lines 347 to 364) recommends a stop under SC or VSC on three conditions. Wear is at or above `sc_stop_min_wear_pct` (35 balanced, 25 aggressive), or a stop is owed inside the window, or a stop is owed and the SC saving is larger.
- `cheap_stop` also needs more than `sc_stop_min_laps_left` (3) laps remaining, and a saving that is positive after the double-stack loss.
- `reactive()` in `strategy/plans.py` builds Plan C. It ranks sequences by race time only, and it accepts one stop or more.
- `CompoundModel` holds `base_ms`, `deg_ms_per_lap`, `deg_posterior_sd_ms_per_lap` and `sets`. It has no wear field.
- The wear rate comes from the current set only, with a default of 2.5 % per lap (`engine/model.py:659`). The `learning/` package has no wear code.
- `learning/hindsight.py` marks every neutralised stop and every `cheap_stop` as not gradable (lines 274 to 281). Nothing scores SC decisions today.
- `pitwall voice warm` renders a static list from `speech.warm_phrases` (`cli/voice.py:252`).

No code on main asks whether the new set lasts to the flag.

## Facts on main that shape the plan

- **Sprints and short races have no pit plan.** `engine/planning.py:255` returns no plan in a sprint, or in a race of `plan_no_stop_max_laps` (5) laps or fewer. Damage and lost pace are the exceptions. A 13-lap race is not short, so the verdict runs there.
- **A VSC that is ending cancels the cheap stop.** `planning.py:259-264` sets the SC status to 0 and restores the green pit loss when `snap.sc_ending` is true under VSC.
- **The pit loss already includes the wing change.** `planning.py:260` adds `wing_loss_s` for damage.
- **Pit-entry timing exists for some tracks.** A rule with `decision_point: pit_entry` is timed by the dispatcher against `pit_entry_m` (`audio/dispatcher.py:436`). 5 of the 15 packaged tracks have `pit_entry_m`.
- **The game reports a life for each tyre set.** `TyreSet` carries `wear`, `life_span` and `usable_life` from the Tyre Sets packet (`state/session.py:276`). The fresh-set count includes only sets with `wear == 0` (line 2702).
- **The planner already receives the rival and teammate inputs.** `snap.rival_data_restricted`, `snap.teammate_stacking` and `snap.field_cars` reach `optimise()`.
- **No estimate of the remaining SC laps exists.** A search by name in `strategy/`, `engine/` and `model/` found none. The deg fit leaves SC laps out (`model/deg.py:405`).
- **The snapshot has forecast fields.** `weather_in_10` and `weather_crossover_pct` are in `state/session.py` (lines 653 and 663).

## Design decisions

1. **One function owns the verdict.** `sc_verdict()` in `strategy/` returns the pick, the ladder row for each compound and the reason. `cheap_stop`, Plan C, the radio rule and the dashboard all read this one result.
2. **Shadow first, takeover second.** For one release the engine computes, logs and shows the ladder, and `cheap_stop` still decides the radio. The takeover is a later code change, not a setting.
3. **Hindsight grades each ladder row that the driver tested.** The survival percentage comes from a standard deviation that nothing calibrates today. The grade is the evidence for the takeover.
4. **Use the shorter life, not a product.** Pace life and wear life belong to the same tyre and driver, so they are not independent. Survival is the probability for the shorter of the two lives.
5. **`usable_life` is the wear life until a learned prior exists.** A compound whose `usable_life` is below `laps_needed` is not a survivor. This closes the case where a soft with a low pace slope passes although wear ends its life.
6. **The compound ladder shows the margin in laps.** A row reads `M  life 14  need 10  +4`. The exact percentage shows only in review mode and on the debrief page.
7. **The survival threshold is a new mindset key.** `sc_survive_min` is 0.75 for balanced and 0.60 for aggressive. `pit_confidence_min` (0.70 and 0.55) keeps its present meaning and its present use.
8. **No confidence word until the high band can occur.** Without a learned wear prior the band stops at medium, so the word is always "medium". The radio line and the card omit the word until phase 5 lifts the cap.
9. **The learned wear prior waits for evidence.** It needs a new learning path. The shadow log shows how often `usable_life` is wrong. Build the prior only if that case is common.

Decisions 1 to 5 and 7 of plan v2 stay as agreed. Decision 6 of plan v2 (the wear prior) moves to phase 5.

## When the verdict runs

The verdict runs each lap when all of these are true:

- The race phase is SC or VSC, and the VSC is not ending (`snap.sc_ending`).
- The session is not a sprint and not a short race, the same gate as `planning.py:255`.
- More than `sc_stop_min_laps_left` laps remain.
- The car is on a dry compound. A car on intermediates or wets is out of scope, and the weather rules decide.

Under a full SC that is ending, the verdict still runs, because `planning.py` keeps the SC pit loss for that lap.

## Survival model

For each dry compound with a fresh set, hardest first:

- `laps_needed` is the number of laps remaining after this lap. The current lap counts at a reduced weight, set by one new threshold. Later laps count in full, because no estimate of the SC length exists.
- Pace life is `tyre_cliff_ms / deg_ms_per_lap`, the same formula that `plans.py:212` uses.
- With slope mean `m` and standard deviation `s`, pace survival is `Phi((tyre_cliff_ms / laps_needed - m) / s)`.
- If `s` is 0, no posterior exists, and pace survival is 1 or 0 from the mean alone.
- Wear life is the `usable_life` of the set. If `usable_life` is below `laps_needed`, survival is 0. If the Tyre Sets packet is absent, the wear gate does not apply.
- A compound survives if its survival is at or above `sc_survive_min`.
- The pick is the survivor with the lowest race time to the flag. `evaluate()` in `plans.py` already computes that time for a stop this lap.
- Only fresh sets count, the same rule as the existing fresh-set count. A used set is not a candidate in this version.
- The two-compound rule uses `PlanInputs.used`. A set count of -1 means unknown and does not remove a compound.
- The pit loss is the measured loss for the current phase plus `wing_loss_s`. The saving must be positive after the double-stack loss.
- If no compound survives, the verdict is "stay out" or the existing Plan C with two stops. The verdict never invents a one-stop finish.

The current set gets a ladder row too. The row uses the measured deg fit, the measured wear rate and the present tyre age.

## Build order

Each phase is one pull request with its own test.

### Phase 1: `sc_verdict()` in shadow

- Do the checks in the section "Open checks" first.
- Add `sc_verdict()` and its result type. Do not change `cheap_stop` or `reactive()`.
- Add `sc_survive_min` to `mindsets.yaml`, and one threshold for the weight of the current SC lap.
- Write the verdict and every ladder row to the decision log each lap that the verdict runs.
- Test: a synthetic 13-lap race with SC on lap 3. Assert the pick and the stay-out case.
- Test each gate: sprint, short race, VSC ending, laps left, stack loss, two-compound rule, `usable_life` below `laps_needed`, and `s` equal to 0.
- Update `docs/03-strategy.md` and `docs/18-race-engine.md`.
- Run `uv run pytest -m "not slow and not replay" -n auto --dist loadfile`.

### Phase 2: hindsight grading

Hindsight grades the ladder row of the set that ran after the SC, not the verdict as a whole. This grades a "stay out" too, and a stop that the driver made against the verdict.

- If the driver made a stop, grade the row of the fitted compound. If the driver made no stop, grade the row of the current set.
- Good: the row said "survives" and the set reached the flag with no later stop for tyres. Also good: the row said "does not survive" and the driver made a later stop for tyres.
- Wrong: the row and the result disagree.
- Censored: a later stop for rain, an owed second compound, damage or a penalty, or a second SC stop. The grader detects damage from a damage call (`front_wing_damage`, `front_wing_lost_box` or `puncture`) between the verdict and the stop. It detects a penalty from `penalty_served_stop_go` at the stop.
- Test: one synthetic race for each grade, and one for a "stay out".

### Phase 3: ladder on the Plan C card, and bench scenario

- Add the compound ladder to the Plan C card, as the section "Dashboard" describes for the shadow release.
- Send the data in the existing state payload. Add no frames, because the Hub closes a client with 64 unsent frames (#192).
- Show the verdict and its grade in review mode and on the debrief page.
- Add one bench scenario for the 13-lap case. `learning/bench.py` already has a `wear_scale` mutation.
- Update `docs/15-dashboard-design.md` and `docs/24-scenario-bench.md`.
- Run `pitwall bench --only <id>` for that scenario. Do not pass `--update-baseline` or `--accept-changes`.

### Phase 4: takeover

Start this phase only when the pass mark below is met and the repo owner has run the full bench.

**Pass mark.** These numbers are a first estimate, not a measured value. Change them after the first shadow sessions if they prove too easy or too hard.

- At least 12 graded rows.
- At least 5 of the 12 from real sessions. The other rows can come from bench scenarios and synthetic recordings.
- No more than 2 wrong grades in total.
- No more than 1 wrong grade where the row said "survives" and the set did not.

Work in this phase:

- `cheap_stop` and Plan C read the verdict. Remove the wear gate and `sc_stop_min_wear_pct` from `pitwindow.py`, `mindsets.yaml`, `docs/08-configuration.md` and `docs/18-race-engine.md`.
- Add the position guard: no stop if the expected net positions after regain are below 0, unless the current set does not survive. With `snap.rival_data_restricted`, the regain uses pace only.
- Add the rain gate: if the weather rules expect a change to intermediates before the flag, the verdict is "hold" and the weather rules decide.
- Give the verdict rule `decision_point: pit_entry`, the same as `plan_sc_box`. On a track with no `pit_entry_m`, the rule fires at the start of the lap.
- Add the verdict word, the box lap, the positions row and the badges to the Plan C card.
- Apply the clash table below.
- Add the radio lines: one stop line for each compound, and the stay-out line. Add all of them to `speech.warm_phrases`.
- Test: the new lines render from the cache, because a render longer than `speech.render_timeout_s` can drop the line (#191).
- Run the full suite one time: `uv run pytest -n auto --dist loadfile`.

### Phase 5: later, if the data supports it

- A wear-rate prior for each compound, scoped by race length, with a wear field on `CompoundModel`. This lifts the medium cap and adds the confidence word and the chip.
- Used sets as candidates, with their `wear` and `life_span`.
- `pit_entry_m` for the 10 packaged tracks that lack it.
- An estimate of the remaining SC laps.
- A prediction of which rivals stop under SC, for the positions row.
- Plan v2 items 8, 9, 12 and 13: second SC, restart warm-up, VSC savings and red flag.

## Clash table for phase 4

| # | Existing behaviour | Resolution |
|---|---|---|
| 1 | `cheap_stop` recommends a stop when wear is at or above 35% | The verdict decides. Wear feeds only the row of the current set. |
| 2 | `plan_sc_box` names the Plan C compound | Plan C takes its compound from the verdict. `plan_sc_box` reads the same value. |
| 3 | `reactive()` can pick two stops | Rank a survivor to the flag first. Offer two stops only when no compound survives. |
| 4 | `no_stop` says "Tyres make the end" under SC | Under SC the verdict owns the line. `no_stop` is silent until green. |
| 5 | `plan_invalid` fires after an SC with no stop | A "stay out" verdict does not switch to Plan C, so nothing becomes invalid. |
| 6 | `plan_switch` to C is spoken | The box line includes the switch. The switch is logged only. |
| 7 | `cover_box`, undercut and defend | The position guard uses their result. They are silent under SC. |
| 8 | `weather_box_now` and `tyre_switch` | The weather rules win. The card shows the rain badge. |
| 9 | `cheap_stop` confidence is the pit-loss source only | Under SC the survival of the pick is the plan confidence. `pit_confidence_min` still gates `box_now`. |
| 10 | `double_stack` | The saving after the stack loss feeds the verdict. If the verdict is "stay out", `double_stack` does not fire. |
| 11 | Reminder for a penalty not yet served | Add the penalty seconds to the pit loss in the verdict. |
| 12 | `sc_deployed`, `sc_delta_warn` and the call budget | Order: `sc_deployed`, then the verdict. `sc_delta_warn` can still interrupt. |
| 13 | Plan A and B frozen sequences | No change. The existing rebase after a stop covers it. |

## Dashboard

`docs/15-dashboard-design.md` sets the limit: the driver looks at the screen for less than a second. The radio is the main channel for the verdict. The screen confirms the radio.

### What the driver sees live

- One verdict on the Plan C card, and only when the verdict runs. Plan A and Plan B get no confidence chip, because three chips ask the driver to compare numbers.
- The compound ladder, one row for each dry compound, hardest first, then the current set: `M  life 14  need 10  +4`. A row with no fresh set is dim and reads `NO SET`.
- The ladder shows on the wide layout only. The phone layout hides it.
- No grade. The grade exists only after the flag.

### Shadow release (phase 3)

- The Plan C card shows the ladder when the verdict runs.
- The card does not show `BOX` or `STAY OUT`. `cheap_stop` still decides the radio, and a card that contradicts a box instruction is worse than no card.
- The ladder can still disagree with the radio, for example a box instruction with every row at a negative margin. The shadow log records each such lap.

### After the takeover (phase 4)

- Headline: `BOX, MED TO END` or `STAY OUT`.
- Box lap, `THIS LAP` or `NEXT LAP`, and the saving against a green stop. The box lap shows only on a track with `pit_entry_m`.
- Positions row: the rejoin position and the expected regain. The row reads `pace only` when rival telemetry is restricted.
- Badges only when they apply: second compound owed, double stack, rain due, pit entry passed.
- Phone layout: the headline only.
- After the SC ends, the card returns to the normal plan view.

### Review mode and debrief

- Each verdict is a tick on the transport strip in review mode, with the ladder for that lap.
- The debrief page lists each graded row with its grade, for example "L3, mediums, survives, good".
- The debrief page shows the count of good and wrong grades against the pass mark.
- The exact survival percentage shows here and not on the live card.

## Scope by plan v2 item

| Plan v2 item | Status |
|---|---|
| 2 green laps after SC | Phase 1, the current SC lap only. The rest is phase 5. |
| 4 double stack | Phase 1, the existing guard |
| 5 two-compound rule | Phase 1 |
| 6 sets available | Phase 1, fresh sets only. Used sets are phase 5. |
| 10 short-race priors | Phase 1, after open check 2 |
| 11 honest confidence | Phase 1, no confidence word until phase 5 |
| 3 pit-entry timing | Phase 4, on tracks with `pit_entry_m` |
| 7 rain-forecast gate | Phase 4 |
| 14 hidden-rival telemetry | Phase 4, with the position guard |
| 1 rivals stop together | Phase 5 |
| 8, 9, 12, 13 | Phase 5 |

## Open checks

Do these checks at the start of phase 1. I did not verify these points on main.

1. Whether `usable_life` changes with the race length, and whether its values are plausible for each compound. Log the value in a replay before any code relies on it.
2. Whether the deg prior that reaches `CompoundModel` is the one scoped by race length (`@13L`, `model/deg.py:347`).
3. Whether `CompoundModel.sets` comes from the fresh-set count at `state/session.py:2702`.
4. Whether the `inputs` of a decision record can carry the ladder, and whether hindsight can read the ladder without a schema change.
5. How the debrief page gets its data (`learning/debrief.py`), and what a new list on that page needs.
6. Which field tells the strategy code that the weather rules expect intermediates. `weather_in_10` and `weather_crossover_pct` are the candidates.
7. Whether any code predicts rival stops. A search by name found `stop_adjusted_gaps` in `state/race.py` and no prediction.

## Risks

- The takeover changes the result of every bench scenario that has an SC. The repo owner must run the full bench and accept the new baseline.
- A wrong "stay out" costs more than a wrong stop, because the cheap stop does not come back. The shadow release and the "stay out" grade exist to measure this before the radio changes.
- With a deg posterior of 0, pace survival is a step, and one lap of difference flips the verdict. The test in phase 1 covers this case.
- `usable_life` is the game's figure, not a measured one. If open check 1 shows that the figure is not plausible, phase 1 drops the wear gate and the plan returns to pace only.
- Later SC laps count in full, so survival is too low during a long SC. The error favours a harder compound or no stop.
- Real sessions give few SC periods. The pass mark can take many sessions to reach.
