# Status

What f1-pitwall does today on `main`, what comes next, and the known gaps. The
[roadmap](05-roadmap.md) has the forward plan.

_Last updated: v0.34.0, 24 Oct 2026._

## What works today

- **Install and update:** one-line installers for Windows, macOS and Linux, and
  `pitwall update` ([install guide](https://pitwall.arthurku.com/install.html)).
- **Qualifying:** traffic-aware release advice, abort guidance, a push/cool/box/push-now
  run plan, fuel and battery reminders, and tyre-pressure advice.
- **Race strategy:** Plans A/B/C with Monte Carlo confidence, pit windows, undercut and
  overcut calls, cover and defend against the car behind, safety-car and VSC stop advice,
  wet-weather pit calls, and plan updates after a stop.
- **Battles:** gap and tyre calls for the cars ahead and behind, the true rival across pit
  cycles, and a let-by call when defending is hopeless.
- **Mindset:** balanced and aggressive mindsets change plans and rules, switchable from the
  dashboard.
- **Tyres, fuel and ERS:** tyre life is the minimum of worst-corner wear life and
  pace-cliff life. Fuel and ERS calls use live budgets.
- **Setup advice:** tyre-pressure and setup recommendations from YAML rule tables, shown on
  the garage screen (`22-setup-advisor.md`).
- **Driver rating:** a learned per-track and global score that scales pit advice
  (`26-driver-rating.md`).
- **Learning:** session grading, `pitwall calibrate`, `pitwall evaluate`, `pitwall propose`,
  and `pitwall stats --learned`. Upkeep runs on each `pitwall start` and refits track
  priors and rule cooldowns.
- **Debrief:** standalone HTML export and an editable `/debrief/<uid>` page.
- **Dashboard:** second-monitor screen with rival tyres and track-limit warning counts.
- **Voice and phone:** Piper speech with a phrase cache, OmniVoice as an option, menu
  voice requests and the `V` listen key, and a phone radio PWA.
- **Recovery:** the watchdog restarts a failed or stalled engine while the recorder continues.

## Next

- Measure frame time against a no-pitwall baseline and commit the numbers
  (`09-performance.md`). The target is no measurable effect on the game's 1 % lows.
- Build the tray app.
- Add more races until calibration reports `converged: true`.
- Record calls-off sessions for an on/off comparison (`speech.enabled: false` silences the
  speaker and stamps `calls_mode: off`). Sessions stamped `off` before 6 Oct were spoken,
  so ingest relabels them `on`.
- Recheck the tuning backlog from the 6 Oct qualifying review:
  - `release_go` fires on garage entry and flip-flops with `release_hold`.
  - `cool_hot_mode` fires after `quali_through`.
  - The cool-lap digest fires three lines at once.
  - The fuel-laps estimate is one lap pessimistic.
  - The race plan proposes one stop in a 5-lap race.
  - Energy advice flips between under and over.
  - Wear and energy advice fires on the final lap.

## Known gaps

- Calibration remains `converged: false`.
- Upkeep grades calls, refits priors and adjusts cooldowns. Grading calls at
  `/debrief/latest` and applying threshold YAML from `pitwall propose` stay manual by
  design, so rule thresholds never change without human review.
- There are no true calls-off sessions yet, so `pitwall evaluate` has no on/off
  comparison. `policy.quiet` still passes priority 1 calls.
- LLM debrief prose is deferred (item 21).
- Windows shutdown and EA Javelin compatibility still need a local check. Linux
  tests cannot verify either.
- The later 25% race fixes were reverted at the owner's request. Mixed-weather
  calls, drive-through tyre calls, stop summaries and early-race pace need a
  separate follow-up. The numbered menu remains removed.
- CI runs lint, formatting and type checks on Linux. Pull requests also run the fast
  tests (`pytest -m "not slow"`) and the replay determinism check. Pushes to `main` run
  the full suite. Tests that need real recordings or Windows still skip in CI.

## History

The original plan had five milestones, M0 capture and replay, M1 one call end to end,
M2 qualifying, M3 race and M4 learning. All five were done by early October 2026, each
checked against live F1 26 sessions. The plan and its exit criteria are in the
[roadmap history](05-roadmap.md#history-the-m0-to-m4-plan). The early planning docs are
under "History / early planning" in the [developer guide](https://pitwall.arthurku.com/#/developer).
