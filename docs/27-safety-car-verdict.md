# Safety car verdict: which compound reaches the flag (plan v4)

Status: phases 1 to 3 are built (shadow log, hindsight grading, ladder card and debrief list). The core of phase 4 is built, see "Takeover, 2026-11-24". The rest of phase 4 and phase 5 are not built. Plan checked against main at 0.60.6 (`734a02d`) on 2026-11-22.

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
7. **The survival threshold is a new mindset key.** `sc_survive_min` is 0.60 for balanced and 0.45 for aggressive. The first values were 0.75 and 0.60. They changed on 2026-11-23 without calibration data, because the model is too pessimistic. Check them again before phase 4. `pit_confidence_min` (0.70 and 0.55) keeps its present meaning and its present use.
8. **No confidence word until the high band can occur.** Without a learned wear prior the band stops at medium, so the word is always "medium". The radio line and the card omit the word until phase 5 lifts the cap.
9. **The learned wear prior waits for evidence.** It needs a new learning path. The shadow log shows how often `usable_life` is wrong. Build the prior only if that case is common.
10. **Learning moves the survival threshold.** Upkeep computes an offset on `sc_survive_min` from all `sc_row` grades of real sessions. A wrong "does not survive" lowers the threshold by 0.05. A wrong "survives" raises it by 0.10, because a wrong "stay out" costs more. The offset stays at 0 below 3 graded rows. The engine keeps the result between 0.45 and 0.85 and loads it at the start of a session. A row graded under an old threshold keeps its count. The limits bound that error.

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
- The verdict makes no pick when the current set survives and its race time is not slower than the pick's. The reason is "stay out, the current set is faster".
- If a second dry compound is still owed, the pick stays. The race time of the current set does not include the owed stop.
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
- Good: the row said "survives" and the set reached the flag with no later stop for tyres and with pace. Also good: the row said "does not survive" and the driver made a later stop for tyres.
- Pace at the flag: the grader takes the valid green laps of the graded stint. The pace loss is the median of the last 3 laps minus the median of the first 3 laps. A loss of `tyre_cliff_ms` or more means that the set had no fighting pace. A "does not survive" row is then good, and a "survives" row is wrong.
- With fewer than 6 valid green laps in the stint, the grader does not measure pace. A set that reached the flag then counts as "survives".
- The pace loss has no fuel correction. The fuel burn makes the late laps faster, so the measured loss is too small.
- Also good: the row said "does not survive" and a damage stop followed. A set with no pace left makes a spin more likely. A damage stop after a "survives" row stays censored.
- Wrong: the row and the result disagree.
- Censored: a later stop for rain, an owed second compound, damage or a penalty, or a second SC stop. The grader detects damage from a damage call (`front_wing_damage`, `front_wing_lost_box` or `puncture`) between the verdict and the stop. It detects a penalty from `penalty_served_stop_go` at the stop.
- Test: one synthetic race for each grade, and one for a "stay out".

### Phase 3: ladder on the Plan C card, and bench scenario

- Add the compound ladder to the Plan C card, as the section "Dashboard" describes for the shadow release.
- Send the data in the existing state payload. Add no frames, because the Hub closes a client with 64 unsent frames (#192).
- Show the verdict and its grade in review mode and on the debrief page.
- The bench scenario moves to phase 4. Bench `expect` checks read fired calls only, and the shadow record is suppressed.
- Update `docs/15-dashboard-design.md`.

### Phase 4: takeover

Start this phase only when the pass mark below is met and the repo owner has run the full bench.

Override on 2026-11-24: the repo owner started the takeover at 5 graded rows, below the pass mark. The section "Takeover, 2026-11-24" lists what is built.

**Pass mark.** These numbers are a first estimate, not a measured value. Change them after the first shadow sessions if they prove too easy or too hard.

- At least 12 graded rows.
- At least 5 of the 12 from real sessions. The other rows can come from bench scenarios and synthetic recordings.
- No more than 2 wrong grades in total.
- No more than 1 wrong grade where the row said "survives" and the set did not.

Work in this phase:

- `cheap_stop` and Plan C read the verdict. Remove the wear gate and `sc_stop_min_wear_pct` from `pitwindow.py`, `mindsets.yaml`, `docs/08-configuration.md` and `docs/18-race-engine.md`.
- Add the position guard: no stop if the expected net positions after regain are below 0, unless the current set does not survive. With `snap.rival_data_restricted`, the regain uses pace only.
- Add the damage stop to the verdict. A stop to repair damage is right when the damage is severe and the car cannot hold its position. The stop is also right when the positions lost are few against the gain in survival to the flag.
- Add the rain gate: if the weather rules expect a change to intermediates before the flag, the verdict is "hold" and the weather rules decide.
- Give the verdict rule `decision_point: pit_entry`, the same as `plan_sc_box`. On a track with no `pit_entry_m`, the rule fires at the start of the lap.
- Add the verdict word, the box lap, the positions row and the badges to the Plan C card.
- Apply the clash table below.
- Add one bench scenario for the 13-lap case, and update `docs/24-scenario-bench.md`. `learning/bench.py` already has a `wear_scale` mutation.
- Run `pitwall bench --only <id>` for that scenario. Do not pass `--update-baseline` or `--accept-changes`.
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
   Partly answered on 2026-11-23, see "Shadow sessions". The values at 16 laps are plausible for each compound. One race length cannot show whether the value changes with the race length.
2. Whether the deg prior that reaches `CompoundModel` is the one scoped by race length (`@13L`, `model/deg.py:347`).
3. Whether `CompoundModel.sets` comes from the fresh-set count at `state/session.py:2702`.
4. Whether the `inputs` of a decision record can carry the ladder, and whether hindsight can read the ladder without a schema change.
5. How the debrief page gets its data (`learning/debrief.py`), and what a new list on that page needs.
6. Which field tells the strategy code that the weather rules expect intermediates. `weather_in_10` and `weather_crossover_pct` are the candidates.
7. Whether any code predicts rival stops. A search by name found `stop_adjusted_gaps` in `state/race.py` and no prediction.

## Shadow sessions

### Count against the pass mark

Status on 2026-11-23: 1 graded row of 12 from a live session, 0 wrong. The database shows this row as censored until hindsight grades the session again.

With the replay batch below, and with the finish fault fixed, the count was 5 graded rows, 3 good and 2 wrong. With the pace grade the count is 5 graded rows, 4 good and 1 wrong. No "survives" row is wrong.

Decision on 2026-11-23: the verdict does not take over yet. The finish fault and the pick fault are fixed. The batch ran again after the pick fix, and the grades did not change. The grader now measures pace at the flag, see "Pace grade". The pessimistic model is still open.

### Imola, 16 laps, 2026-11-23 (session 14001174973346760379)

What happened:

- Red flag on lap 1. Restart on lap 3 with mediums.
- VSC on lap 3. The dashboard showed `BOX NOW` on the Plan C card and on the action card. The driver followed the dashboard and made a stop on lap 3 for softs.
- The radio spoke no stop call. `plan_sc_box` needs Plan C to be active, and the active plan stayed on Plan A.
- The softs ran laps 4 to 14. The driver reported no usable pace on the last laps, then a spin and a lost front wing. The stop on lap 14 followed.

What the shadow log shows:

- Lap 3: no pick, "stay out, nothing reaches the flag", `laps_needed` 13.5, saving 21.5 s.
- Lap 3 ladder, pace life and `usable_life`: hard 5.3 and 23, medium 6.4 and 18, soft 6.2 and 12, current medium 3.3 and 18.
- Every row had survival 0 and a negative margin. The Plan C card showed `BOX NOW M` on the same lap.
- Lap 4 has a second record. The engine wrote it 5.6 s after `vsc_ending`, with the car in the pit lane.
- Grade: "S row L3 does not survive (0%), damage stop L14". The first grade was censored. The grade is good under the damage rule in phase 2.

Findings:

- **The verdict was right and the dashboard was wrong.** The verdict said that no compound reaches the flag. The softs did not reach the flag with fighting pace. `cheap_stop` recommended the stop at 3% wear, because a stop was owed and the VSC saving was larger. `cheap_stop` does not ask whether the new set lasts. This is the case that the takeover exists to close.
- **Disagreement on lap 3.** The dashboard said `BOX NOW` and every ladder row had a negative margin. The rule in this doc counts radio calls, and the radio was silent. The count of disagreements must include the dashboard cards too.
- **The row was right.** The driver names tyre wear as the cause of the spin. The grader cannot see that cause, so phase 2 now grades a damage stop after a "does not survive" row as good.
- **Open check 1.** `usable_life` was 23, 18 and 12 laps for hard, medium and soft at 16 laps. The softs ran 11 laps to 46% wear, and the driver reported them finished. The soft value of 12 agrees with that. The wear gate stays.
- **The pace lives agree with earlier stints.** The deg priors for Imola at 16 laps were 281 (hard), 234 (medium) and 243 (soft) ms for each lap. Three medium stints measured 350 to 406, and two hard stints measured 335 and 381. The priors are not too steep for this driver.
- **The priors are noisy between compounds.** Each prior comes from 2 or 3 stints. The hard got a shorter pace life than the medium for this reason.
- **Survival is a step when the prior is learned.** `_compound_models` sets the standard deviation to 0 for a learned or overlay prior. Every row then has survival 0 or 1, and the noise in the prior is ignored.
- **The Plan C card and the action card named different compounds.** The Plan C card said medium, and the action card said `PLAN A` with soft.
- **Fixed: a record after the VSC ended.** SCAR event 3 (resume) set `sc_ending` back to false while `race_phase` was still `vsc`. The engine wrote the lap 4 record 0.1 s later. `sc_ending` now stays true through the resume event.

### Replay batch, 2026-11-23

Method:

- A census read each recording of 10 MB or more without the engine. 16 files, 12 races, 7 races with SC or VSC laps on a dry compound.
- Each race ran through `pitwall replay --speed max --seed-db` into its own copy of the database. The real database was read only.
- The replays used the deg priors of 2026-11-23. Those priors include these races, so the result is better than a live run on the day.

| Race | SC laps | Driver | Row graded | Grade before the finish fix | Grade after the finish fix |
|---|---|---|---|---|---|
| Imola 16 laps, 2026-11-23 | VSC 3 | stop L3 for softs, damage stop L14 | S, does not survive | good | good |
| Imola 16 laps, 2026-11-19 | VSC 2 to 3 | stop L2 for hards, ran to the flag | H, does not survive | censored, no finish | wrong |
| Track 32, 14 laps | SC 10 to 13 | no stop, softs 1 lap old | S on car, survives | censored, no finish | good |
| Track 16, 18 laps | SC 14 to 16 | no stop, mediums 7 laps old | M on car, does not survive (53%) | censored, no finish | wrong |
| Track 7, 13 laps | SC 8 to 10 | stop L8 for softs | S, survives | censored, no finish | good |
| Track 14, 15 laps | SC 5 to 7, VSC 10 to 11 | stops L5 and L10, retired | H and S | censored | censored |
| Imola 16 laps, 2026-10-22 | SC 1 to 3 | stop L1, rain stop L8 | M | censored, rain | censored |

Faults found:

- **Fixed: hindsight never saw a finish.** The engine did not store the final lap, because the game holds the lap counter at the flag. The engine now stores the final lap when the result status of the player becomes "finished". A second replay of the batch gave the grades in the last column. Before the fix: Of 55 races in the database, none had a lap row for the final lap. `grade_session` sets `finished` only when the last stored lap equals `total_laps`. Because of this, no real row could get the grade "reached the flag". The fuel and plan grades use the same value. Races stored before the fix keep the gap.
- **Fixed: the verdict picked a stop that was slower than no stop.** The pick was the fastest fresh survivor. The pick was never compared with the row of the current set. At track 32 on lap 10 the pick was softs at 443.5 s, and the current softs, 1 lap old, were at 435.1 s and survive. After a takeover the radio would call that stop from P1. In 11 of the 13 records with a pick, the current set survives and is faster than the pick. The verdict now makes no pick in that case, unless a second compound is owed. The table "Third replay" below shows each record.
- **The model is too pessimistic, and the grader asks a different question.** The verdict asks for fighting pace (`tyre_cliff_ms`). The grader asks only whether a later stop came. The Imola hards ran 13 laps to the flag, and the last 4 laps were 2 to 4 s slower. The row was right on pace and wrong for the grader. At track 16 the mediums needed 3.5 laps with a margin of +1.0, and survival was 53%.

Other results:

- Both wrong grades are "does not survive" rows. No "survives" row was wrong.
- Track 14, lap 5: the radio said "box, softs". The soft row did not survive, because `usable_life` was 10 and `laps_needed` was 10.5. The pick was hards, and the driver fitted hards.
- Track 7, laps 8 and 9: the radio and the pick agreed on softs. After the pick fix the verdict says "stay out" on both laps, and the radio still says "box".

Third replay, after the pick fix:

| Race | Lap | Pick before | Pick after | Stop time, s | Current set, s |
|---|---|---|---|---|---|
| Track 32 | 10 | soft | none | 443.5 | 435.1 |
| Track 32 | 11 | soft | none | 356.4 | 348.3 |
| Track 7 | 8 | soft | none | 570.2 | 563.8 |
| Track 7 | 9 | soft | none | 482.7 | 478.7 |
| Track 7 | 10 | medium | none | 385.2 | 375.9 |
| Track 14 | 7 | medium | none | 812.7 | 806.3 |
| Track 14 | 8 | medium | none | 722.8 | 717.0 |
| Track 14 | 10 | soft | none | 556.7 | 555.6 |
| Track 14 | 11 | soft | none | 466.4 | 463.2 |
| Track 14 | 5 | hard | hard | 996.9 | 988.9 |
| Track 14 | 6 | soft | soft | 902.4 | 899.4 |
| Track 16 | 14 | soft | soft | 374.5 | 370.4 |
| Track 16 | 15 | soft | soft | 300.8 | 296.6 |

- 9 of the 13 picks changed to no pick. The 7 records with no pick before did not change.
- Track 14, laps 5 and 6: the current mediums survive and are faster, and the pick stays. The car had raced only mediums, so a second compound was owed.
- Track 16, laps 14 and 15: the pick stays, because the current mediums do not survive (53%). The current set is faster by 4 s and reached the flag. This is the pessimistic model fault.
- The grades did not change: 5 graded rows, 3 good, 2 wrong, 0 wrong "survives". The grade reads the ladder row and not the pick.
- Track 7, lap 8: the driver made the stop for softs, and the softs reached the flag. The verdict now says "stay out" there. The grader cannot say which choice was faster.

### Pace grade, 2026-11-23

The grader asked only whether a later stop came. The grader now asks the same question as the verdict, as phase 2 describes. The databases of the third replay were graded again, with no new replay.

| Race | Row graded | Grade before | Grade with the pace grade |
|---|---|---|---|
| Imola 16 laps, 2026-11-23 | S, does not survive | good | good, damage stop L14 |
| Imola 16 laps, 2026-11-19 | H, does not survive | wrong | good, lost 3.5 s of pace |
| Track 32, 14 laps | S on car, survives | good | good, 1 green lap, no pace measured |
| Track 16, 18 laps | M on car, does not survive (53%) | wrong | wrong, lost 0.4 s of pace |
| Track 7, 13 laps | S, survives | good | good, 3 green laps, no pace measured |

- The 3 censored rows did not change.
- Imola hards: the first three green laps were 79.6, 81.0 and 80.6 s. The last three were 84.5, 84.1 and 80.7 s. Lap 14 had no ERS deployment. Lap 16 had full ERS deployment and a track limits penalty. The median ignores one such lap.
- Track 16 mediums: the stint has 6 valid green laps, 5 before the SC and 1 after it. The set kept its pace, so the row was wrong. This row is the evidence for the pessimistic model.
- Two of the three "survives" grades rest on the stop rule alone, because their stints had fewer than 6 green laps.

### Takeover, 2026-11-24

The repo owner decided to take over before the pass mark was met. The count stood at 5 graded rows, 4 good, 1 wrong, 0 wrong "survives".

Built (clash rows 1 to 6, 9 and 10):

- The verdict runs before the pit-window optimiser. A pick gives `cheap_stop`. No pick gives `stay`. The wear gate, the two owed-stop gates and `sc_stop_min_wear_pct` are removed.
- Plan C takes its sequence from the verdict. A "stay out" lists no Plan C and switches no plan.
- When nothing reaches the flag, the verdict compares a stop now plus one later stop with the best plan that has no stop now. The verdict picks the stop only when it is faster. The headline then reads `BOX, MED` without `TO END`.
- The survival of the pick is the plan confidence. `box_now` keeps `cheap_stop` and the `pit_confidence_min` gate. `plan_sc_box` speaks when `box_now` is gated out.
- New rule `sc_stay_out` speaks one time for each SC period.
- `pitwall voice warm` renders the `plan_sc_box` lines for each dry compound and the stay-out lines. The default of `speech.warm_phrases` is 170.
- The card shows the headline `BOX, MED TO END`, `BOX, MED` or `STAY OUT`. The phone layout shows the headline only.
- With no deg fit, the engine now plans from the fresh prior while the verdict runs. Before the takeover the engine made no pit call in that case.
- With damage and no pick, the pit-window optimiser decides as before. The damage stop is not in the verdict yet.
- A "stay out" verdict also stops undercut and cover calls under SC.

Not built: position guard, damage stop, rain gate, penalty seconds, `decision_point: pit_entry` for the stay-out line, box lap, positions row, badges and the bench scenario (clash rows 7, 8, 11 and 12).

Learning:

- `sc_row` grades no longer feed `cooldown_mult:sc_verdict`. The grades judge the survival model and not the timing of a call. They still move the offset on `sc_survive_min`.
- `pitwall tune` prints the offset and the number of graded rows.
- `pitwall replay --seed-db` loads the offset. The replay passes the database to the engine in the same way as a live session.
- The grader now grades rows that the driver follows. A driver who obeys a "stay out" never tests a fresh-set row with "does not survive". The evidence for those rows stops, and a too-pessimistic fresh-set row cannot be found from real sessions. A wrong "survives" on the current set still shows.
- The decision record of the verdict still has `suppressed_by: shadow`. The name is out of date.

Replay batch after the takeover (7 races, deg priors of 2026-11-23). The driver in a recording cannot follow the new radio.

| Race | Verdict | Radio | Driver | Grade |
|---|---|---|---|---|
| Imola, 2026-11-23 | L3 stay out, nothing reaches the flag | `sc_stay_out` L3 | stop L3 for softs | good |
| Imola, 2026-11-19 | L2 and L3 stay out, nothing reaches the flag | none, wing damage on L2 | stop L2 for hards | good |
| Track 32 | L10 and L11 stay out, current set is faster | `sc_stay_out` L10 | no stop | good |
| Track 16 | L14 and L15 softs to the flag | `plan_sc_box` L14, softs | no stop | wrong |
| Track 7 | L8 to L10 stay out, current set is faster | `sc_stay_out` L8 | stop L8 for softs | good |
| Track 14 | L5 hards, L6 softs, then stay out | `plan_sc_box` L5, hards | stops L5 and L10 | censored |
| Imola, 2026-10-22 | L1 to L3 stay out, nothing reaches the flag | `sc_stay_out` L3 | stop L1, rain stop L8 | censored |

- Track 7, laps 8 and 9: the radio says "stay out" and no stop call fires. Before the takeover `cheap_stop` said "box" on both laps.
- Imola, 2026-11-23: the radio says "stay out" on lap 3. Before the takeover the dashboard said `BOX NOW`. The two-stop was not faster than the best plan with no stop now.
- Track 16: the radio now calls a stop for softs from mediums that kept their pace. This is the pessimistic model fault, and it is now a spoken call.
- Imola, 2026-11-19: with wing damage and no pick, the verdict is silent and the other rules decide.
- Imola, 2026-10-22: the verdict ran on laps 1 to 3, and the stay-out line fired on lap 3 only. The cause is in "Close-out, 2026-11-24".
- `plan_invalid` fires after the SC in five races. In four of them the recorded driver did not follow the verdict. Track 14, lap 8 was a fault: the driver made the stop on lap 5, and the radio still said "off Plan C". The fix is in "Close-out, 2026-11-24".
- The grades did not change: 5 graded rows, 4 good, 1 wrong, 0 wrong "survives".

Open:

- `optimise()` still takes `wear_mean`, `green_pit_loss_s` and `stop_owed`, and no longer reads them.
- The repo owner must run the full bench and accept the baseline.
- The headline on the dashboard is not checked in a browser.

### Close-out, 2026-11-24

Causes found:

- Track 14, lap 8, "off Plan C". The engine counted a stop only when the race phase changed to `out_lap`. The phase stays `sc` or `vsc` for the whole period, so the engine did not count a stop made under SC. At the green flag the plan tracker read Plan C as not taken. The engine now also counts a stop when `num_pit_stops` rises under SC or VSC. The replay of track 14 has no `plan_invalid` on lap 8.
- Track 14, lap 11 now has a `plan_invalid`. The driver made a second stop on lap 10 for a new nose, which no plan had.
- Imola, 2026-10-22, the late stay-out line. On lap 1 the wing damage skipped the verdict call, as designed. The driver made the stop at the end of lap 1, and the repair cleared the damage on lap 2. The car was then in the pit lane, and `sc_stay_out` needs `pit_status == 0`. The game was paused from 124 s to 238 s. The engine does not evaluate rules during a pause, so the line fired when the pause ended. No fault.

Built (clash rows 8 and 11):

- Rain gate. When `weather_crossover` is `to_inter` or `to_wet`, the verdict is "hold, rain is due". The verdict picks no compound, and `sc_call` is "". `sc_stay_out` is silent and the weather rules own the radio. The ladder is still logged, with `hold` in the record.
- The rain gate does not compare the forecast time with the laps left. A crossover that the weather rules announce is enough.
- Penalty seconds. `penalty_s` adds to the SC pit loss in the verdict. The saving, the race time of each fresh-set row and the two-stop include it. The row of the current set does not.
- Drive-through and stop-go penalties are not in the verdict. The snapshot has no seconds for them.
- Imola, 2026-10-22 after the change: laps 1 to 3 read "hold, rain is due", and no stay-out line fires. The rain stop came on lap 8.

Not built: position guard, damage stop, `decision_point: pit_entry` for the stay-out line, box lap, positions row, badges with the rain badge, and the bench scenario (clash rows 7 and 12).

## Risks

- The takeover changes the result of every bench scenario that has an SC. The repo owner must run the full bench and accept the new baseline.
- A wrong "stay out" costs more than a wrong stop, because the cheap stop does not come back. The shadow release and the "stay out" grade exist to measure this before the radio changes.
- With a deg posterior of 0, pace survival is a step, and one lap of difference flips the verdict. The test in phase 1 covers this case.
- `usable_life` is the game's figure, not a measured one. If open check 1 shows that the figure is not plausible, phase 1 drops the wear gate and the plan returns to pace only.
- Later SC laps count in full, so survival is too low during a long SC. The error favours a harder compound or no stop.
- Real sessions give few SC periods. The pass mark can take many sessions to reach.
