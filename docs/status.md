# Status

Where the build stands against the [roadmap](05-roadmap.md).

_Last updated: live qualifying review, 6 Oct 2026._

## Milestones

| Milestone | State | Evidence |
|---|---|---|
| **M0** Capture and replay | Done | Live F1 26 practice recorded and replayed with matching packet census and decisions |
| **M1** One call, end to end | Done | Parsers, state, rules, dispatcher, dashboard and SAPI speech run against the live game; the front-wing damage call fires on replay of the live recording and is audible. Out-lap tyre call measured at 41 to 101 ms on three live out-laps (6 Oct) |
| **M2** Qualifying | Done | Full Q1–Q3 driven with the assistant; decision logs reviewed and tuned. 6 Oct review added the next-lap invalidation call and gated the recharge nag |
| **M3** Race | Done | 25% and 100% races recorded and reviewed; strategy fixes from the 100% race are in PR #21 |
| **M4** Better over time | Done | Debrief produced from a real sprint and 100% race. Calibration, `pitwall evaluate` and `pitwall propose` run on recorded sessions |

## What works today

- **Qualifying:** traffic-aware release advice, abort guidance, a push/cool/box/push-now run plan, fuel and battery reminders, and tyre-pressure advice.
- **Race strategy:** Plans A/B/C, pit windows, undercut and overcut calls, safety-car and VSC stop advice, and plan updates after a stop.
- **Tyres, fuel and ERS:** tyre life is the minimum of worst-corner wear life and pace-cliff life; fuel and ERS calls use live budgets.
- **Learning:** session grading, `pitwall calibrate`, `pitwall evaluate`, `pitwall propose`, and `pitwall stats --learned`. Upkeep runs on each `pitwall start` and refits track priors and rule cooldowns.
- **Debrief:** standalone HTML export and an editable `/debrief/<uid>` page.
- **Voice and phone:** Piper phrase cache, plus a phone radio PWA that caches static assets.
- **Recovery:** the watchdog restarts a failed or stalled engine while the recorder continues.

## Next

Open items from the M1 and M4 exit criteria:

- Run the frame-time A/B against a no-backend baseline and commit the numbers (`09-performance.md`). M1 asks for no measurable effect on the game's 1% lows.
- Build the tray app.
- Get calibration to `converged: true`.
- Record calls-off sessions for an on/off comparison (`speech.enabled: false` now silences the speaker and stamps `calls_mode: off`). Sessions stamped `off` before 6 Oct were spoken, so ingest relabels them `on`.
- Tuning backlog from the 6 Oct qualifying review:
  - `release_go` fires on garage entry and flip-flops with `release_hold`.
  - `cool_hot_mode` fires after `quali_through`.
  - The cool-lap digest fires three lines at once.
  - The fuel-laps estimate is one lap pessimistic.
  - The race plan proposes one stop in a 5-lap race.
  - Energy advice flips between under and over.
  - Wear and energy advice fires on the final lap.
- Add more races and check calibration convergence.

## Known gaps

- Calibration remains `converged: false`.
- Upkeep grades calls, refits priors and adjusts cooldowns. Grading calls at
  `/debrief/latest` and applying threshold YAML from `pitwall propose` stay manual by
  design, so rule thresholds never change without human review.
- There are no true calls-off sessions yet, so `pitwall evaluate` has no on/off comparison. `policy.quiet` still passes priority 1 calls.
- Player lap rows in `laps` have no sector times, and lap rows are written twice per lap.
- LLM debrief prose is deferred (item 21).
- Windows shutdown and EA Javelin compatibility still need a local check. Linux
  tests cannot verify either.
- The later 25% race fixes were reverted at the user's request. Mixed-weather
  calls, drive-through tyre calls, stop summaries and early-race pace need a
  separate follow-up. The numbered menu remains removed.
- CI runs lint, formatting and type checks on Linux. Pull requests also run the fast
  tests (`pytest -m "not slow"`) and the replay determinism check. Pushes to `main` run
  the full suite. Tests that need real recordings or Windows still skip in CI.
