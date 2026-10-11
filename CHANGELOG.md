# Changelog

This project follows [Semantic Versioning 2.0.0](https://semver.org) with the 0.x rule. While the version is 0.x, a breaking change bumps MINOR instead of MAJOR.

The release workflow writes entries after 0.1.0 from the Conventional Commit PR titles on `main`. The 0.1.0 entry was written by hand.

<!-- version list -->

## v0.1.0 (2026-10-11)

First version. It covers all work up to milestone M4 in [docs/status.md](docs/status.md).

### Telemetry and replay

- UDP ingest for the F1 26 `2026` format, with a session model that the rules read on each tick.
- Recording to `.f1bin` and `.f1bin.zst` with an index, recording profiles, and replay at any speed with the same decisions.
- Synthetic recordings and `pitwall bench` for scenario tests.

### Qualifying

- Traffic-aware release advice, abort guidance and a push, cool, box run plan.
- Fuel, battery and tyre-pressure reminders, a next-lap invalidation call and a teammate benchmark row.

### Race strategy

- Plans A, B and C, pit windows, and undercut and overcut calls.
- Safety-car and VSC stop advice, plan updates after a stop, and battle calls for overtake mode.
- Tyre life from worst-corner wear and pace cliff, plus fuel and ERS calls from live budgets.

### Learning loop

- Session grading, `pitwall calibrate`, `pitwall evaluate`, `pitwall propose` and `pitwall stats --learned`.
- Upkeep on each `pitwall start` refits track priors and rule cooldowns.
- Debrief as a standalone HTML export and an editable `/debrief` page.

### Voice and dashboard

- Speech through Piper or Windows SAPI, a phrase cache, and radio scheduling by urgency.
- Second-monitor dashboard, a `/radio` log view, a phone radio PWA, wheel-button gestures and voice input.

### Tooling

- `pitwall doctor`, setup advisor, cleanup and maintenance commands, and a watchdog that restarts a stalled engine.
- CI with Ruff, mypy, a start smoke test, and fast and full pytest tiers.
