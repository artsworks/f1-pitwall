# Status

What f1-pitwall covers today on `main`, what comes next, and the known gaps. The
[changelog](../CHANGELOG.md) lists every release, and the [roadmap](05-roadmap.md) has the
forward plan.

## What works today

This list names areas, not individual calls. Each area grows with every release, so
check the changelog for what a given version added.

- **Install and update:** one-line installers for Windows, macOS and Linux, and
  `pitwall update` ([install guide](https://pitwall.arthurku.com/install.html)).
- **Qualifying:** release and traffic advice, abort guidance, a run plan, and fuel, battery
  and tyre-pressure advice.
- **Race strategy:** Plans A/B/C, pit windows, undercut and overcut calls, and stop advice
  for safety cars, VSCs, red flags and weather.
- **Race events:** battles, penalties, track limits, formation lap and race end.
- **Car management:** tyres, fuel, ERS, damage and thermal limits, with setup advice
  (`22-setup-advisor.md`).
- **Mindset and driver rating:** balanced and aggressive mindsets, and a learned driver
  score (`26-driver-rating.md`).
- **Learning and debrief:** session grading, `pitwall calibrate`, `pitwall evaluate`,
  `pitwall propose`, `pitwall stats --learned`, and an HTML debrief.
- **Dashboard, voice and phone:** a second-monitor dashboard, Piper or OmniVoice speech,
  menu voice requests, and a phone radio PWA.
- **Recovery:** the watchdog restarts a failed or stalled engine while the recorder continues.

## Next

- Measure frame time against a no-pitwall baseline and commit the numbers
  (`09-performance.md`). The target is no measurable effect on the game's 1 % lows.
- Build the tray app.
- Add more races until calibration reports `converged: true`.
- Record calls-off sessions for an on/off comparison (`speech.enabled: false` silences the
  speaker and stamps `calls_mode: off`). Sessions stamped `off` before 6 Oct were spoken,
  so ingest relabels them `on`.
- Recheck `release_go` and `release_hold` in a busy qualifying session. The two calls
  can still alternate every 30 s while cars pass the pit exit.
- Confirm in a 5-lap race that the plan has no mandatory stop (`plan_no_stop_max_laps`).

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
  calls, stop summaries and early-race pace need a separate follow-up. Drive-through
  tyre calls are back with a test, see [penalty escalation](research/penalty-escalation.md).
  The numbered terminal menu for a bare `pitwall` command remains removed. The driver
  menu on the wheel and dashboard is unaffected.
- CI runs lint, formatting and type checks on Linux. Pull requests also run the fast
  tests (`pytest -m "not slow"`) and the replay determinism check. Pushes to `main` run
  the full suite. Tests that need real recordings or Windows still skip in CI.

## History

The original plan had five milestones, M0 capture and replay, M1 one call end to end,
M2 qualifying, M3 race and M4 learning. All five were done by early October 2026, each
checked against live F1 26 sessions. The plan and its exit criteria are in the
[roadmap history](05-roadmap.md#history-the-m0-to-m4-plan). The early planning docs are
under "History / early planning" in the [developer guide](https://pitwall.arthurku.com/#/developer).
