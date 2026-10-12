# Changelog

This project follows [Semantic Versioning 2.0.0](https://semver.org) with the 0.x rule. While the version is 0.x, a breaking change bumps MINOR instead of MAJOR.

The release workflow writes entries after 0.1.0 from the Conventional Commit PR titles on `main`. The 0.1.0 entry was written by hand.

<!-- version list -->

## v0.4.0 (2026-10-12)

### Bug Fixes

- **server**: State frames carry the engine's processed snapshot
  ([#74](https://github.com/artsworks/f1-pitwall/pull/74),
  [`c9ac551`](https://github.com/artsworks/f1-pitwall/commit/c9ac551fc2ccba341286e80197e5c0fe0dcc044c))

- **state**: Drop implausible second-car gaps
  ([#74](https://github.com/artsworks/f1-pitwall/pull/74),
  [`c9ac551`](https://github.com/artsworks/f1-pitwall/commit/c9ac551fc2ccba341286e80197e5c0fe0dcc044c))

- **state**: Report first crossing minute for weather crossover
  ([#74](https://github.com/artsworks/f1-pitwall/pull/74),
  [`c9ac551`](https://github.com/artsworks/f1-pitwall/commit/c9ac551fc2ccba341286e80197e5c0fe0dcc044c))

- **web**: Battle rows follow battle.mode, plan C only under SC/VSC
  ([#74](https://github.com/artsworks/f1-pitwall/pull/74),
  [`c9ac551`](https://github.com/artsworks/f1-pitwall/commit/c9ac551fc2ccba341286e80197e5c0fe0dcc044c))

- **web**: Cool-down last lap and pole gap, phone review layout
  ([#74](https://github.com/artsworks/f1-pitwall/pull/74),
  [`c9ac551`](https://github.com/artsworks/f1-pitwall/commit/c9ac551fc2ccba341286e80197e5c0fe0dcc044c))

- **web**: Restore phone radio header wrap and keep the tyre row below the phone banner
  ([#74](https://github.com/artsworks/f1-pitwall/pull/74),
  [`c9ac551`](https://github.com/artsworks/f1-pitwall/commit/c9ac551fc2ccba341286e80197e5c0fe0dcc044c))

### Documentation

- One-screen dashboard docs and site screenshots
  ([#74](https://github.com/artsworks/f1-pitwall/pull/74),
  [`c9ac551`](https://github.com/artsworks/f1-pitwall/commit/c9ac551fc2ccba341286e80197e5c0fe0dcc044c))

- Resize box-call hero to 1920x1080 ([#74](https://github.com/artsworks/f1-pitwall/pull/74),
  [`c9ac551`](https://github.com/artsworks/f1-pitwall/commit/c9ac551fc2ccba341286e80197e5c0fe0dcc044c))

- Retake box-call hero on the desktop layout
  ([#74](https://github.com/artsworks/f1-pitwall/pull/74),
  [`c9ac551`](https://github.com/artsworks/f1-pitwall/commit/c9ac551fc2ccba341286e80197e5c0fe0dcc044c))

- Retake phone race and quali out-lap shots ([#74](https://github.com/artsworks/f1-pitwall/pull/74),
  [`c9ac551`](https://github.com/artsworks/f1-pitwall/commit/c9ac551fc2ccba341286e80197e5c0fe0dcc044c))

- Retake phone race shot ([#74](https://github.com/artsworks/f1-pitwall/pull/74),
  [`c9ac551`](https://github.com/artsworks/f1-pitwall/commit/c9ac551fc2ccba341286e80197e5c0fe0dcc044c))

- Retake phone shots and update final-lap caption
  ([#74](https://github.com/artsworks/f1-pitwall/pull/74),
  [`c9ac551`](https://github.com/artsworks/f1-pitwall/commit/c9ac551fc2ccba341286e80197e5c0fe0dcc044c))

### Features

- **engine**: Page input resets to the race layout
  ([#74](https://github.com/artsworks/f1-pitwall/pull/74),
  [`c9ac551`](https://github.com/artsworks/f1-pitwall/commit/c9ac551fc2ccba341286e80197e5c0fe0dcc044c))

- **server**: Add dashboard payload fields for reimagined dashboard
  ([#74](https://github.com/artsworks/f1-pitwall/pull/74),
  [`c9ac551`](https://github.com/artsworks/f1-pitwall/commit/c9ac551fc2ccba341286e80197e5c0fe0dcc044c))

- **web**: Rebuild dashboard on the reimagined single-screen layout
  ([#74](https://github.com/artsworks/f1-pitwall/pull/74),
  [`c9ac551`](https://github.com/artsworks/f1-pitwall/commit/c9ac551fc2ccba341286e80197e5c0fe0dcc044c))


## v0.3.0 (2026-10-12)

### Features

- **server**: Add dashboard payload fields for reimagined dashboard
  ([#72](https://github.com/artsworks/f1-pitwall/pull/72),
  [`250b550`](https://github.com/artsworks/f1-pitwall/commit/250b55040911def5b9dcbfb14c515f5380a6596c))


## v0.2.0 (2026-10-12)

### Continuous Integration

- Semantic-release versioning, CHANGELOG and PR-title lint
  ([#67](https://github.com/artsworks/f1-pitwall/pull/67),
  [`057af9f`](https://github.com/artsworks/f1-pitwall/commit/057af9fbeab0546413096aa7daf6d289a769b884))

### Documentation

- Refresh dashboard screenshots and tighten docs
  ([#69](https://github.com/artsworks/f1-pitwall/pull/69),
  [`4836c74`](https://github.com/artsworks/f1-pitwall/commit/4836c74a8081595a3f5cac4a645496ca4ee26695))

- **mockups**: Add multi-car battle rows with scrap and sandwich previews
  ([#71](https://github.com/artsworks/f1-pitwall/pull/71),
  [`de73828`](https://github.com/artsworks/f1-pitwall/commit/de73828b30ff249986ae6be0810c30f14a50b119))

- **mockups**: Add reimagined race-engineer dashboard mockup
  ([#71](https://github.com/artsworks/f1-pitwall/pull/71),
  [`de73828`](https://github.com/artsworks/f1-pitwall/commit/de73828b30ff249986ae6be0810c30f14a50b119))

- **mockups**: Add tyre trend, window meter, condition tags and lock-up border
  ([#71](https://github.com/artsworks/f1-pitwall/pull/71),
  [`de73828`](https://github.com/artsworks/f1-pitwall/commit/de73828b30ff249986ae6be0810c30f14a50b119))

- **mockups**: Add weather icons to the weather tile
  ([#71](https://github.com/artsworks/f1-pitwall/pull/71),
  [`de73828`](https://github.com/artsworks/f1-pitwall/commit/de73828b30ff249986ae6be0810c30f14a50b119))

- **mockups**: Colour-coded tyre trend arrow in place of status word, bigger wear value
  ([#71](https://github.com/artsworks/f1-pitwall/pull/71),
  [`de73828`](https://github.com/artsworks/f1-pitwall/commit/de73828b30ff249986ae6be0810c30f14a50b119))

- **mockups**: Show weather crossover as tile marker and safety card
  ([#71](https://github.com/artsworks/f1-pitwall/pull/71),
  [`de73828`](https://github.com/artsworks/f1-pitwall/commit/de73828b30ff249986ae6be0810c30f14a50b119))

- **mockups**: Shrink banner call text ([#71](https://github.com/artsworks/f1-pitwall/pull/71),
  [`de73828`](https://github.com/artsworks/f1-pitwall/commit/de73828b30ff249986ae6be0810c30f14a50b119))

### Features

- Stream Piper audio to the browser in replay --serve
  ([#70](https://github.com/artsworks/f1-pitwall/pull/70),
  [`b20c8e8`](https://github.com/artsworks/f1-pitwall/commit/b20c8e891fde454d03d557fcff3ff1e3473d39e1))


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
