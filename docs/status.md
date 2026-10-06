# Status

Where the build stands against the [roadmap](05-roadmap.md).

_Last updated: M4 ([PR #21](https://github.com/artsworks/f1-pitwall/pull/21))._

## Milestones

| Milestone | State | Evidence |
|---|---|---|
| **M0** Capture and replay | Done | Live F1 26 practice recorded and replayed with matching packet census and decisions |
| **M1** One call, end to end | Built, live exit pending | Parsers, state, rules, dispatcher, dashboard and SAPI speech run against the live game; the front-wing damage call fires on replay of the live recording and is audible. Pending: out-lap tyre call inside 300 ms, frame-time A/B, tray app |
| **M2** Qualifying | Done | Full Q1–Q3 driven with the assistant; decision logs reviewed and tuned |
| **M3** Race | Built; PR #7 merged | 25% and 100% races recorded; strategy fixes from the 100% race are in PR #21 |
| **M4** Better over time | Built in PR #21 | Debrief produced from a real sprint and 100% race. Calibration is not yet converged. No calls-off sessions are recorded, so on/off evaluation has no comparison. |

## What works today

- **Qualifying:** traffic-aware release advice, abort guidance, a push/cool/box/push-now run plan, fuel and battery reminders, and tyre-pressure advice.
- **Race strategy:** Plans A/B/C, pit windows, undercut and overcut calls, safety-car and VSC stop advice, and plan updates after a stop.
- **Tyres, fuel and ERS:** tyre life is the minimum of worst-corner wear life and pace-cliff life; fuel and ERS calls use live budgets.
- **Learning:** session grading, `pitwall calibrate`, `pitwall evaluate`, `pitwall propose`, and `pitwall stats --learned`. Upkeep runs on each `pitwall start` and refits track priors and rule cooldowns.
- **Debrief:** standalone HTML export and an editable `/debrief/<uid>` page.
- **Voice and phone:** Piper phrase cache, plus a phone radio PWA that caches static assets.
- **Recovery:** the watchdog restarts a failed or stalled engine while the recorder continues.

## Next

- Record calls-off sessions for an on/off comparison.
- Add more races and check calibration convergence.

## Known gaps

- Calibration remains `converged: false`.
- Upkeep grades calls, refits priors and adjusts cooldowns. Grading calls at
  `/debrief/latest` and applying threshold YAML from `pitwall propose` stay manual by
  design, so rule thresholds never change without human review.
- There are no calls-off sessions yet, so `pitwall evaluate` has no on/off comparison.
- LLM debrief prose is deferred (item 21).
- Windows shutdown and EA Javelin compatibility still need a local check. Linux
  tests cannot verify either.
- The later 25% race fixes were reverted at the user's request. Mixed-weather
  calls, drive-through tyre calls, stop summaries and early-race pace need a
  separate follow-up. The numbered menu remains removed.
- CI runs lint, formatting and type checks on Linux. Pull requests also run the fast
  tests (`pytest -m "not slow"`) and the replay determinism check. Pushes to `main` run
  the full suite. Tests that need real recordings or Windows still skip in CI.
