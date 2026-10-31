# Scenario bench

`pitwall bench` replays scenario files, scores pitwall's calls, and compares the results
with a stored baseline. A change that worsens any guarded result fails the gate. Use the
bench to measure rule and strategy changes and check that learning improves pitwall
without regressions.

## Terms

- **Scenario.** A YAML file in `scenarios/`. It names a source recording, optional mutations and the calls that must or must not fire.
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

- `mutations` accepts `inject_sc`, `vsc`, `wear_scale`, `penalty`, `damage`, `teammate_pit` and `inject_rival_pit` keys. The bench applies them during replay.
- `inject_rival_pit: "LAP[:ahead|behind]"` sets the pit status of the car directly ahead (default) or behind the player on that lap. Positions and gaps do not change.
- The `damage` mutation clears the wing damage at the player's next pit stop.
- A `fire` check passes if one of its rules fires on a lap inside `laps`. The lap range includes both ends.
- An `absent` check passes if none of its rules fires inside `laps`.
- The bench finds the source by session UID in `--recordings` folders and checks its
  sha256. If the file is missing, the scenario is `skipped`.

Each scenario runs in its own empty database with default priors. Results do not depend on
scenario order or previously learned values.

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
| `damage` | Plan invalidation and box calls after wing damage or position loss | Pace and gap calls, because lap times do not change |
| `penalty` | Penalty calls and the penalty total | Finish-position calls, which use real gaps |
| `inject_rival_pit` | The rival-pitted flag that feeds `overcut`, `cover` and `defend` plans | Rival pace and gaps, which stay as recorded |

Rival stops, weather changes and red flags need a real recording or a generator. `src/pitwall/net/derive.py` cannot make them.

## Starter scenarios

The `brazil-*` scenarios from `brazil-race-real` to `brazil-double-stack` use the Brazil race from 6 Oct 2026: track 16, 17 laps, one stop on lap 11, no safety car. `brazil-quali-fuel-out` uses a Brazil short qualifying from 13 Oct 2026. Every other scenario names its source recording by session UID inside the file. The bench finds it in the `--recordings` folders.

| Scenario | Status | Checks |
|---|---|---|
| `brazil-race-real` | guard | Plan and window calls, no safety car calls |
| `brazil-sc-early` | guard | Safety car calls on laps 6 to 9 |
| `brazil-lap1-damage` | guard | A 100% lost wing on lap 1 rebuilds the plans and moves the target stop to now |
| `brazil-sc-in-window` | target | A box call when the safety car comes out inside the window |
| `brazil-vsc-early` | guard | VSC calls, never full safety car calls |
| `brazil-worn-tyres` | guard | Tyre-life warning and a box call in the window |
| `brazil-worn-tyres-late-stop` | target | No second stop with two laps left |
| `brazil-penalty` | guard | Corner-cutting penalty call on lap 5 |
| `brazil-double-stack` | guard | Teammate pit call on lap 11, not on lap 10 |
| `brazil-quali-fuel-out` | target | Box at the line when fuel is short after a cool lap |
| `brazil-overcut` | target | Stay out when the rival ahead pits on lap 6 |
| `mexico-undercut` | target | Box call with the car ahead inside 3 seconds |
| `brazil2-sc-lap-1` | target | No stop call under a safety car on laps 1 and 2 |
| `brazil3-penalty` | target | Corner-cutting penalty call on lap 8 |
| `brazil3-race-real` | target | Plan announced on lap 1, no safety car calls |
| `brazil3-sc-early` | target | Safety car calls on laps 2 to 5 |
| `brazil3-vsc-early` | target | VSC calls, never full safety car calls |
| `brazil4-sc-real` | target | Real safety car calls on laps 14 to 17 |
| `austin-vsc-real` | target | Real VSC call on lap 11, no safety car calls |
| `imola-race-real` | target | Plan on lap 1, a stop call before lap 10, no safety car calls |
| `imola-sc-last-laps` | target | No stop call under a safety car on laps 14 to 16 |
| `imola-wear-double` | target | Tyre-life or stop call on laps 5 to 9 with doubled wear |
| `imola-late-penalty` | target | Penalty, cost, covered and finish calls on lap 16 |
| `mexico-vsc-real` | target | Real VSC call on laps 12 and 13, no safety car calls |
| `mexico2-vsc-real` | target | Real VSC calls on laps 12 and 13, no safety car calls |
| `silverstone-sc-real` | target | Real safety car calls on laps 8 to 11, no VSC calls |
| `spa-wear-double` | target | Tyre-life or stop call on laps 3 to 7 with doubled wear |
| `zandvoort-race-real` | target | Plan on lap 1, a stop call before lap 7, no safety car calls |
## Backlog

- A VSC inside the window. The stop saves less time than under a full safety car. A stop call depends on tyre wear, position risk and laps left, so write the check against the rule inputs.
- Two safety cars in one race.
- Recheck the two targets `brazil-overcut` and `mexico-undercut`. In a local run on 30 Oct 2026, before the 0.42.0 merge, `overcut_stay_out` did not fire on laps 6 to 7 and `box_now` did not fire on laps 5 to 8.
- Wear twice as fast, so that Plan B with two stops is faster. The Imola and Spa scenarios check tyre-life and stop calls, not Plan B.

## Limits

- CI cannot run the bench, because recordings stay out of git. Run it on a machine that has the recordings, and put the output in the PR.
- The starter set has 9 real scenarios, but the last run graded 1 outcome. `real_accuracy` stays empty until real scenarios give 5 or more graded outcomes.
- Scenarios use default priors. Calls with learned priors can be different.
