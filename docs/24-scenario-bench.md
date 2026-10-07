# Scenario bench

`pitwall bench` replays a set of scenario files and scores the calls pitwall makes. It compares the score with a stored baseline. A change that makes any guarded result worse fails the gate.
Use the bench to measure each rule or strategy change, and to check that learning improves pitwall instead of degrading it.

## Terms

- **Scenario.** A YAML file in `scenarios/`. It names a source recording, optional `pitwall derive` mutations and the calls that must or must not fire.
- **Real scenario.** A scenario with no mutations. The bench replays the source as it was recorded.
- **Guard.** A scenario that must pass. A failed guard fails the gate.
- **Target.** A scenario that shows a known gap. Pitwall does not pass it yet. A change that makes a target pass is an improvement.
- **Baseline.** `scenarios/baseline.json`, the last accepted scorecard.
- **History.** `scenarios/history.jsonl`, one line for each accepted baseline.

## Scenario file

```yaml
id: brazil-sc-in-window
title: Safety car on laps 10 and 11, inside the window
status: target
source:
  session_uid: "0x2188302d49bba3bb"
  sha256: "cd3d036fdd5fbf9ee19a2325f3e6d16a0c92421b8ec418a89daaf2cd5091c2b0"
  where: "Brazil race, 6 Oct 2026"
mutations:
  inject_sc: "10-11"
expect:
  fire:
    - {rule: sc_deployed, laps: [10, 10]}
    - {rule: [plan_sc_box, box_now], laps: [10, 11]}
  absent:
    - {rule: box_now, laps: [15, 17]}
why: The mediums are 9 laps old and the window is open, so the stop is cheap.
```

- `mutations` takes the `pitwall derive` options: `inject_sc`, `vsc`, `wear_scale` and `penalty`.
- A `fire` check passes if one of its rules fires on a lap inside `laps`. The lap range includes both ends.
- An `absent` check passes if none of its rules fires inside `laps`.
- The bench finds the source by session UID in the `--recordings` folders and checks its sha256. If the file is missing, the scenario is `skipped`.

Each scenario runs in its own empty database with default priors. The result does not depend on the order of scenarios or on what the database has learned.

## Scorecard

| Metric | Meaning |
|---|---|
| `score` | Percent of checks passed, over all scenarios that ran |
| `guards`, `targets` | Scenarios passed out of scenarios run, for each status |
| `real_accuracy` | Hindsight `good / (good + wrong)` on real scenarios. It uses the same filter as `pitwall tune` |
| `stop_cost_mae_s`, `laps_of_pace_mae` | Median absolute error of stop cost and tyre life on real scenarios |

The bench does not use hindsight grades from synthetic scenarios. The recorded driver does not react to a mutation, so those grades describe the source race.

## Gate

The gate fails, and `pitwall bench` exits with code 1, if one of these is true:

1. A guard scenario fails.
2. A scenario that passed in the baseline fails now.
3. A check that passed in the baseline fails now.
4. `real_accuracy` drops by more than `--tolerance` (default 0.02). This applies only when both runs have 5 or more graded outcomes.
5. The accuracy of one rule drops by more than 0.10. This applies only when that rule has 5 or more graded outcomes in both runs.
6. A median error rises by more than 10%.
7. A scenario has a replay error. An error says nothing about the expected calls.

The gate is incomplete, and the exit code is 2, if one of these is true:

- A guard or a scenario from the baseline is skipped. This usually means a recording is missing.
- A scenario from the baseline did not run, for example because of `--only`.
- A scenario from the baseline was removed.
- A check that passed in the baseline was removed or changed. The output lists it as `changed`.

Otherwise the gate passes with exit code 0 and lists the improvements.

Without a baseline, the gate still checks rules 1 and 7 and skipped guards.

`pitwall bench --update-baseline --note "..."` writes a new baseline and adds one line to the history. It refuses if the gate fails, if a scenario was skipped or did not run, or if the gate lists `changed` items. To save `changed` items, add `--accept-changes`. The output then lists them as `accepted`. Use `--accept-changes` only after the repo owner approves the new expectations.

## Trend

`pitwall bench --trend` reads the history and gives one verdict for the last 5 entries:

- **improving.** The latest score is higher than the first score in the window.
- **flat.** The score did not change.
- **stagnant.** Five or more entries moved the score by less than 0.5 points and no new target passed.
- **degrading.** The latest score is more than 0.5 points below the best of the other entries in the window.

When the trend is stagnant, add new targets or new real recordings. Do not keep tuning against the same targets.

## Add a scenario

1. Choose a gap from the backlog below or from a real session.
2. Find a real source recording with the right shape. Read its UID with `pitwall recordings` and its hash with `sha256sum`.
3. Write the scenario file. Write the expectations from race logic, not from the calls pitwall makes today.
4. Run `pitwall bench --only <id> --recordings <dir>`. Read the failed checks and the laps where the rules fired.
5. If pitwall fails the scenario, set `status: target`. Set `status: guard` only if pitwall passes it and the expectations are correct.
6. Open a PR with the scenario file only. The repo owner approves the expectations before anyone changes rules against the scenario.

## Improve pitwall against a target

1. Run `pitwall bench` on the base branch. Save the output as the before result.
2. Change the rules or the strategy code.
3. Run `pitwall bench` again. The gate must pass and the target must pass.
4. Run the fast tests: `uv run pytest -m "not slow" -n auto`.
5. Run `pitwall bench --update-baseline --note "<what changed>"`.
6. Commit the change, `baseline.json` and `history.jsonl` together.
7. Put the before and after scores, the scenarios that changed and the gate output in the PR description.

## Rules for agents

- Do not edit the `expect` block of a scenario to make it pass. Changes to `expect` need approval from the repo owner.
- Do not change a guard to a target.
- If the gate lists `changed` items, stop. Do not use `--accept-changes` until the repo owner approves the expectation change.
- Do not edit `baseline.json` or `history.jsonl` by hand.
- Do not commit recordings, indexes or databases.
- Work on one target in each PR.
- Do not tune from hindsight grades of synthetic sessions.
- Stop at the end of the timebox. Report what you tried and why it did not pass.

## What a mutation can test

See [Synthetic recordings](23-synthetic-recordings.md) for the full audit.

| Mutation | Trust | Do not trust |
|---|---|---|
| `inject_sc`, `vsc` | Order and timing of safety car calls, plan and box calls under a safety car | Gaps, positions and rival calls, because the field does not bunch up |
| `wear_scale` | Tyre-life and box calls as wear grows | Pace calls, because lap times do not drop. A new set starts worn |
| `penalty` | Penalty calls and the penalty total | Finish-position calls, which use real gaps |

Rival stops, weather changes and red flags need a real recording or a generator. `pitwall derive` cannot make them.

## Starter scenarios

All starter scenarios use the Brazil race from 6 Oct 2026: track 16, 17 laps, one stop on lap 11, no safety car.

| Scenario | Status | Checks |
|---|---|---|
| `brazil-race-real` | guard | Plan and window calls, no safety car calls |
| `brazil-sc-early` | guard | Safety car calls on laps 6 to 9 |
| `brazil-sc-in-window` | target | A box call when the safety car comes out inside the window |
| `brazil-vsc-early` | guard | VSC calls, never full safety car calls |
| `brazil-worn-tyres` | guard | Tyre-life warning and a box call in the window |
| `brazil-worn-tyres-late-stop` | target | No second stop with two laps left |
| `brazil-penalty` | guard | Corner-cutting penalty call on lap 5 |

## Backlog

- A safety car on lap 1 or 2, before any stop makes sense.
- A safety car in the last 3 laps. Pitwall must not call a stop.
- A VSC inside the window. The stop saves less time than under a full safety car.
- Two safety cars in one race.
- Wear twice as fast, so that Plan B with two stops is faster.
- A penalty in the last 3 laps, for the penalty cost and penalty covered calls.

## Limits

- CI cannot run the bench, because recordings stay out of git. Run it on a machine that has the recordings, and put the output in the PR.
- The starter set has one real race. `real_accuracy` stays empty until real scenarios give 5 or more graded outcomes. Add real races with a safety car and two stops.
- Scenarios use default priors. Calls with learned priors can be different.
