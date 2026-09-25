# Status

Where the build stands against the [roadmap](05-roadmap.md). Updated with each
milestone PR; lessons from live sessions go into the [ADRs](adr/README.md).

_Last updated: M1 build ([PR #2](https://github.com/artsworks/f1-pitwall/pull/2))._

## Milestones

| Milestone | State | Evidence |
|---|---|---|
| **M0** Capture and replay | Done | Live F1 26 session (Brazil practice, ~5 min, 69k datagrams): every packet accepted, zero size mismatches; replay at 10× and max gives identical census and decisions |
| **M1** One call, end to end | Built, live exit pending | Parsers, state, rules, dispatcher, dashboard and SAPI speech run against the live game; the front-wing damage call fires on replay of the live recording and is audible. Pending: out-lap tyre call inside 300 ms, frame-time A/B, tray app |
| **M2** Qualifying | Not started | — |
| **M3** Race | Not started | — |
| **M4** Better over time | Not started | Piper voice brought forward (built; awaiting listening test on the game PC) |

## What works today

- **Ingest**: UDP 20777, format 2026 gate (the game reports year 25, v1.26), per-packet size checks, `pitwall doctor` with raw vs accepted counts.
- **Recording**: `lite` by default (~35 MB per 3 h compressed), `pitwall start --record full` for debugging; see [Replay and debugging](07-replay-and-debug.md) and ADR 0006.
- **Replay**: `pitwall replay FILE --speed N --serve` drives the same engine and dashboard offline.
- **Rules**: sector-3 out-lap tyre check, `front_wing_damage` (≥ 15 %), boost left on into a corner, yellow ahead / behind by marshal zone, front and rear lock-ups (ADR 0007), spin → easy on the throttle rejoining. Calls rotate through phrase variants and escalate in tone on repeated mistakes (ADR 0008).
- **Dashboard**: the [redesign](15-dashboard-design.md) — call banner with lifecycle, tyre plan view, fuel, damage, radio log, stale handling; `/radio` compact view.
- **Speech**: Piper neural voice (`pitwall voices get`), with Windows SAPI as fallback (both confirmed on the game PC).

## Next

1. Close the M1 exit: out-lap tyre call run, frame-time A/B numbers.
2. M2 qualifying: remaining packets, flashback handling, release window, abort advisory, full dispatcher, SQLite.

## Known gaps

- GitHub Actions has not run: `ci.yml` only exists on the feature branch until it merges to `main`.
- Call "why" evidence, fuel target delta and the strategy zone are hidden until the backend supplies them.
