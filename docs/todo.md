# Todo

Personal reminders. Each item says what it is, why it matters and where to start.

## Next session: PR 155 checklist

Written 30 Oct 2026. PR 155 (`chore/bench-scenarios-new-recordings`) adds 16 bench scenarios, this file, and a Bench note in `AGENTS.md`. It is pushed and mergeable. Do these steps in order.

1. Check the stash. Run `git stash list`. `stash@{0}` holds old local edits to `bench.py`, `derive.py`, `deg.py`, `test_deg.py`, `test_derive.py` and `docs/24-scenario-bench.md`. A copy is at `C:\Users\akus1\pitwall-local-changes.patch`.
   - Most of it duplicates `inject_rival_pit` (PR 153), which is already on `main`. Do not run `git stash pop` blindly.
   - The thermal-cutoff change in `fit_stint` (`deg.py`) and its test `test_thermal_cutoff_is_ignored_when_it_leaves_too_few_laps` may be new. Check if `main` has it. If it does not, make a separate PR. If it does, run `git stash drop`.
2. Rerun the bench on `main` 0.42.0. The "all 16 new targets pass" result came from a run before the merge, and the new strategy rules may change it. Run the two known gaps first: `uv run pitwall bench --recordings recordings --recordings "F:\Labs\data-pitwall\backup-recordings" --only brazil-overcut mexico-undercut`.
3. Look at the `UNSTABLE` status on the PR. Run `gh pr checks 155`.
4. Update the baseline on a machine that has the Brazil recordings: `pitwall bench --recordings <dir> --update-baseline --accept-changes --note "..."`. It refuses here, because the Brazil guards skip when the recordings are missing.
5. Merge PR 155 after you approve the 16 expectations.

## Recheck four bench targets

In a local run on 30 Oct 2026, both targets failed. That run used code from before the 0.42.0 merge, which changed the strategy rules. Run them again on the current `main` first. If they still fail, they are known gaps, not new regressions. Neither is proven to be a pitwall fault.

### `brazil-overcut`

- File: `scenarios/brazil-overcut.yaml`. Source: Brazil race `0xc235af0beb211b71`, 18 laps, one stop on lap 12. The car is P3 behind Hamilton at 0.4 to 0.9 seconds.
- Mutations: tyres wear twice as fast (`wear_scale: 2`) and the car ahead pits on lap 6 (`inject_rival_pit: "6:ahead"`).
- Expects: `overcut_stay_out` on laps 6 to 7. Neither `box_now` nor `cover_box` on those laps.
- Result on 30 Oct: `overcut_stay_out` never fired.
- The rule in `src/pitwall/config/defaults/rules/race_strategy.yaml` needs `pit_plan == 'overcut'` and `pit_plan_confidence >= mode.pit_confidence_min`. Check which of the two is false on laps 6 and 7.
- Why it matters: the car behind the rival should stay out and push on old tyres. If pitwall says nothing, the driver gets no overcut call.

### `mexico-undercut`

- File: `scenarios/mexico-undercut.yaml`. Source: Mexico race `0xc356d841fd4d0af7`, 18 laps, one stop on lap 8. The car is P2 behind Albon at 2.3 to 3.2 seconds.
- Mutation: tyres wear three times as fast (`wear_scale: 3`).
- Expects: `box_now` on laps 5 to 8.
- Result on 30 Oct: `box_now` never fired.
- The rule needs `pit_plan` to be `box_now`, `cheap_stop`, `free_stop` or `undercut`. It also needs enough confidence, a position-loss risk under the mode limit, a gain of zero or more, and enough laps left.
- This may not be a fault. A stop call depends on tyre wear, position risk and laps left. Check if the recorded car really loses nothing by stopping before deciding the expectation is right.

### `imola-race-real`

- File: `scenarios/imola-race-real.yaml`. Source: Imola race `0xa0548b236a00d328`, 16 laps, one stop on lap 10, no safety car.
- Expects: `plan_window_open` on laps 6 to 10, and a stop call (`plan_target_lap`, `box_now` or `box_in_n`) on laps 6 to 10.
- Result on 31 Oct, after the window and stop checks were split: the stop call fired, but `plan_window_open` never fired in the race.
- The rule needs `plan_window_open`, `on_plan` and `plan_target_lap > lap_num`. Check which one is false on laps 6 to 10, and whether a `plan` cooldown suppressed the call.

### `zandvoort-race-real`

- File: `scenarios/zandvoort-race-real.yaml`. Source: Zandvoort race `0x62763ef3aa8ff344`, 18 laps, stops on laps 7 and 17, no safety car.
- Expects: `plan_window_open` on laps 3 to 7, and a stop call (`plan_target_lap`, `box_now` or `box_in_n`) on laps 3 to 7.
- Result on 31 Oct, after the split: `plan_window_open` fired, but no stop call fired in the race.
- `plan_target_lap` needs `on_plan`, `plan_stops_left > 0` and `plan_target_lap == lap_num`. Check whether the plan target lap ever matched the lap the car pitted on.

### How to investigate

1. Run `uv run pitwall bench --recordings recordings --recordings "F:\Labs\data-pitwall\backup-recordings" --only brazil-overcut mexico-undercut imola-race-real zandvoort-race-real`.
2. The bench prints only the rules that fire inside the check window. To see every fired rule, call `pitwall.learning.bench.run_scenario` from a short script that wraps `evaluate_checks` and prints the `fired` list. I did this for `imola-late-penalty` on 30 Oct 2026.
3. Do not edit the `expect` block to make the scenario pass. Changes to `expect` need your approval.
4. Do not run the bench in a cloud session. See the Bench section in `AGENTS.md`.

## Bench follow-ups

- The 16 scenarios added on 30 Oct 2026 are all `status: target`. They are not in `baseline.json` yet. Run `pitwall bench --update-baseline --accept-changes` on a machine that has the Brazil recordings. The bench refuses to write a baseline while a guard is skipped.
- Promote a passing target to a guard only when you agree its expectation is correct.
- `real_accuracy` stays empty. The last run graded 1 outcome and it needs 5 or more. More real scenarios with graded calls fix this.
- Two scenarios were not added because a stop call under a safety car or VSC depends on tyre wear, position risk and laps left: a VSC inside the stop window and a safety car at the recorded stop. A better test would set those inputs directly. The first is in the Backlog of `docs/24-scenario-bench.md`.
- Two safety cars in one race cannot be built with a mutation. Look for a real recording.

## Missing recordings

The original Brazil recordings are lost on this machine: the 6 Oct race `0x2188302d49bba3bb` and the Brazil Q1 `0x02768451bbe4c8dd`. Ten guard and target scenarios use them: the nine table rows from `brazil-race-real` to `brazil-double-stack` use the race, and `brazil-q1-cool-push-check` uses the Q1. They skip here, so the full bench reports the gate as incomplete (exit code 2) on this machine. They still run on a machine that has the files. If you find the files, check their sha256 against the values in the scenario YAML files.

Other recordings live in `recordings/` and in `F:\Labs\data-pitwall\backup-recordings`. The backup folder also holds `silverstone-sprintandrace.zip.001` to `.005` and `zandvoort.zip.001` to `.002`. Nobody has opened them yet.
