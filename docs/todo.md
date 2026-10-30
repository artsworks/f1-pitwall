# Todo

Personal reminders. Each item says what it is, why it matters and where to start.

## Blocker for PR 155: `inject_rival_pit` is not on main

`brazil-overcut` uses the mutation `inject_rival_pit: "6:ahead"`. The code for it is not on `main`. It sits in uncommitted changes in `src/pitwall/learning/bench.py`, `src/pitwall/net/derive.py`, `src/pitwall/model/deg.py`, `tests/test_deg.py` and `tests/test_derive.py`. The matching text in `docs/24-scenario-bench.md` is also uncommitted.

On `main` the bench rejects the key, so `brazil-overcut` ends as an error and the gate fails. Do one of these before merging PR 155:

- Commit and merge the `inject_rival_pit` code first, then merge PR 155.
- Or remove `brazil-overcut.yaml` from PR 155 and rerun `--update-baseline` without it.

## Two failing bench targets

Both fail in the baseline from 30 Oct 2026. The score was 95.7 with 17 of 19 targets passing. Both are known gaps, not new regressions. Neither is proven to be a pitwall fault yet. Find out which rule input blocks the call, then decide if the expectation or the rule needs a change.

### `brazil-overcut`

- File: `scenarios/brazil-overcut.yaml`. Source: Brazil race `0xc235af0beb211b71`, 18 laps, one stop on lap 12. The car is P3 behind Hamilton at 0.4 to 0.9 seconds.
- Mutations: tyres wear twice as fast (`wear_scale: 2`) and the car ahead pits on lap 6 (`inject_rival_pit: "6:ahead"`).
- Expects: `overcut_stay_out` on laps 6 to 7. Neither `box_now` nor `cover_box` on those laps.
- Result: `overcut_stay_out` never fires.
- The rule in `src/pitwall/config/defaults/rules/race_strategy.yaml` needs `pit_plan == 'overcut'` and `pit_plan_confidence >= mode.pit_confidence_min`. Check which of the two is false on laps 6 and 7.
- Why it matters: the car behind the rival should stay out and push on old tyres. Pitwall says nothing, so the driver gets no overcut call.

### `mexico-undercut`

- File: `scenarios/mexico-undercut.yaml`. Source: Mexico race `0xc356d841fd4d0af7`, 18 laps, one stop on lap 8. The car is P2 behind Albon at 2.3 to 3.2 seconds.
- Mutation: tyres wear three times as fast (`wear_scale: 3`).
- Expects: `box_now` on laps 5 to 8.
- Result: `box_now` never fires.
- The rule needs `pit_plan` to be `box_now`, `cheap_stop`, `free_stop` or `undercut`. It also needs enough confidence, a position-loss risk under the mode limit, a gain of zero or more, and enough laps left.
- This may not be a fault. A stop call depends on tyre wear, position risk and laps left. Check if the recorded car really loses nothing by stopping before deciding the expectation is right.

### How to investigate

1. Run `uv run pitwall bench --recordings recordings --recordings "F:\Labs\data-pitwall\backup-recordings" --only brazil-overcut mexico-undercut`.
2. The bench prints only the rules that fire inside the check window. To see every fired rule, call `pitwall.learning.bench.run_scenario` from a short script that wraps `evaluate_checks` and prints the `fired` list. I did this for `imola-late-penalty` on 30 Oct 2026.
3. Do not edit the `expect` block to make the scenario pass. Changes to `expect` need your approval.
4. Do not run the bench in a cloud session. See the Bench section in `AGENTS.md`.

## Bench follow-ups

- All 19 scenarios are `status: target`. There are no guards. Promote a passing target to a guard only when you agree its expectation is correct. Until then the gate fails only on a regression of a baseline check.
- `real_accuracy` stays empty. The last run graded 1 outcome and it needs 5 or more. More real scenarios with graded calls fix this.
- Two scenarios were removed because a stop call under a safety car or VSC depends on tyre wear, position risk and laps left: `brazil2-vsc-in-window` and `brazil3-sc-in-window`. A better test would set those inputs directly. They are listed in the Backlog of `docs/24-scenario-bench.md`.
- Two safety cars in one race cannot be built with a mutation. Look for a real recording.

## Missing recordings

The original Brazil recordings are lost: the 6 Oct race `0x2188302d49bba3bb` and the Brazil Q1 `0x02768451bbe4c8dd`. Eight scenarios used them. PR 155 removed those scenarios. They are in git history. If you find the files, check their sha256 against the removed YAML files, then restore the scenarios.

Other recordings live in `recordings/` and in `F:\Labs\data-pitwall\backup-recordings`. The backup folder also holds `silverstone-sprintandrace.zip.001` to `.005` and `zandvoort.zip.001` to `.002`. Nobody has opened them yet.
