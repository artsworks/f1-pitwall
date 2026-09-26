# Status

Where the build stands against the [roadmap](05-roadmap.md). Updated with each
milestone PR; lessons from live sessions go into the [ADRs](adr/README.md).

_Last updated: M2 build ([PR #6](https://github.com/artsworks/f1-pitwall/pull/6))._

## Milestones

| Milestone | State | Evidence |
|---|---|---|
| **M0** Capture and replay | Done | Live F1 26 session (Brazil practice, ~5 min, 69k datagrams): every packet accepted, zero size mismatches; replay at 10× and max gives identical census and decisions |
| **M1** One call, end to end | Built, live exit pending | Parsers, state, rules, dispatcher, dashboard and SAPI speech run against the live game; the front-wing damage call fires on replay of the live recording and is audible. Pending: out-lap tyre call inside 300 ms, frame-time A/B, tray app |
| **M2** Qualifying | Done | Full Q1–Q3 driven with the assistant; each decision log reviewed for false positives and tuned from the recordings (fuel limits, release-too-late, duplicate traffic calls, ERS recharge mode 0, 3 s acknowledgement window) |
| **M3** Race | Not started | — |
| **M4** Better over time | Not started | Piper voice brought forward (built; awaiting listening test on the game PC) |

## What works today

- **Ingest**: UDP 20777, format 2026 gate (the game reports year 25, v1.26), per-packet size checks, `pitwall doctor` with raw vs accepted counts.
- **Recording**: `lite` by default (~35 MB per 3 h compressed), `pitwall start --record full` for debugging; see [Replay and debugging](07-replay-and-debug.md) and ADR 0006.
- **Replay**: `pitwall replay FILE --speed N --serve` drives the same engine and dashboard offline.
- **Rules**: sector-3 out-lap tyre check, `front_wing_damage` (≥ 15 %), boost left on into a corner, yellow ahead / behind by marshal zone, front and rear lock-ups (ADR 0007), spin or big slide → easy on the throttle rejoining, repeated lock-ups in the same braking zone called as such. Calls rotate through phrase variants and escalate in tone on repeated mistakes (ADR 0008).
- **Dashboard**: the [redesign](15-dashboard-design.md) — call banner with lifecycle, tyre plan view, fuel, damage, radio log, stale handling; `/radio` compact view.
- **Speech**: Piper neural voice (`pitwall voices get`), with Windows SAPI as fallback (both confirmed on the game PC).

- **Qualifying (M2)**: all packets parsed; flashback/pause/red-flag handling; qualifying phase and run tracking; clean-air release with a "no time for a lap" check; abort advisory; fuel/battery/cooldown run plan with a recharge-mode reminder; per-corner tyre pressure advice clamped to setup limits.
- **Dispatcher**: priorities, preemption, dedupe, shared cooldown groups, budgets, verbosity presets, quiet mode.
- **Driver input**: UDP Action 1 single = acknowledge, double = negative, hold = radio silent on/off (UDP Action 3 as a fallback toggle); spoken replies with variants. See [driver input](12-driver-input.md).
- **Persistence and review**: SQLite (with migrations) and the JSONL decision log; review mode with timeline and grading; `pitwall diff`, `pitwall report`.
- **Pit board**: full-screen garage / pitting view with release light, per-corner pressure targets, next-run summary and the car setup in game-menu order.

## Next

1. M3 race: stint and pit-stop strategy, undercut / overcut, safety-car calls.
2. Confirm on the wheel that a held UDP Action 1 toggles radio silent (else bind UDP Action 3).
3. Listen in game to the tuned fuel limits and the pit board with real pressure advice.

## Known gaps

- GitHub Actions has not run: `ci.yml` only exists on the feature branch until it merges to `main`.
- Call "why" evidence, fuel target delta and the strategy zone are hidden until the backend supplies them.
