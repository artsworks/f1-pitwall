# Changelog

This project follows [Semantic Versioning 2.0.0](https://semver.org) with the 0.x rule. While the version is 0.x, a breaking change bumps MINOR instead of MAJOR.

The release workflow writes entries after 0.1.0 from the Conventional Commit PR titles on `main`. The 0.1.0 entry was written by hand.

## Unreleased

- `speech.engine: "sapi"` is removed. Pitwall prints an unknown-engine warning and resolves the engine with `auto` (Piper if its voice is installed, else null). `auto` no longer picks SAPI on Windows, and `speech.voice` is gone.

<!-- version list -->

## v0.31.1 (2026-10-20)

### Bug Fixes

- **quali**: Stop false "through" calls; sync Melbourne overlay
  ([#123](https://github.com/artsworks/f1-pitwall/pull/123),
  [`0a12b2b`](https://github.com/artsworks/f1-pitwall/commit/0a12b2b6076164eca35b2b4fc5499b6a64eaa86e))


## v0.31.0 (2026-10-19)

### Bug Fixes

- **driver**: Filter pit-lane passes and unknown SC laps
  ([#120](https://github.com/artsworks/f1-pitwall/pull/120),
  [`cd9e063`](https://github.com/artsworks/f1-pitwall/commit/cd9e0637114a7497ae3e9f50ffb78ae09225cb50))

- **driver**: Preserve folds and backfill stored sessions
  ([#120](https://github.com/artsworks/f1-pitwall/pull/120),
  [`cd9e063`](https://github.com/artsworks/f1-pitwall/commit/cd9e0637114a7497ae3e9f50ffb78ae09225cb50))

### Code Style

- **web**: Dim driver rating source in DRV chip
  ([#120](https://github.com/artsworks/f1-pitwall/pull/120),
  [`cd9e063`](https://github.com/artsworks/f1-pitwall/commit/cd9e0637114a7497ae3e9f50ffb78ae09225cb50))

### Features

- **driver**: Overtake and infringement components
  ([#120](https://github.com/artsworks/f1-pitwall/pull/120),
  [`cd9e063`](https://github.com/artsworks/f1-pitwall/commit/cd9e0637114a7497ae3e9f50ffb78ae09225cb50))

- **strategy**: Per-track driver rating scales pit advice
  ([#120](https://github.com/artsworks/f1-pitwall/pull/120),
  [`cd9e063`](https://github.com/artsworks/f1-pitwall/commit/cd9e0637114a7497ae3e9f50ffb78ae09225cb50))

- **web,cli**: Show learned driver rating on dashboard and terminal
  ([#120](https://github.com/artsworks/f1-pitwall/pull/120),
  [`cd9e063`](https://github.com/artsworks/f1-pitwall/commit/cd9e0637114a7497ae3e9f50ffb78ae09225cb50))


## v0.30.0 (2026-10-19)

### Features

- **cli**: Print progress to stderr for recording commands
  ([#122](https://github.com/artsworks/f1-pitwall/pull/122),
  [`cbfeb20`](https://github.com/artsworks/f1-pitwall/commit/cbfeb2026442fe299222e116d8db5916147bf23c))


## v0.29.0 (2026-10-18)

### Bug Fixes

- **rules**: Keep pit-exit and energy-over variants accurate
  ([#119](https://github.com/artsworks/f1-pitwall/pull/119),
  [`d7b5187`](https://github.com/artsworks/f1-pitwall/commit/d7b518769e8a2b6c9df0d4bf16f9a15d5a8c2eb7))

### Continuous Integration

- Add job timeouts, run fast tests on main, add manual full-suite job
  ([#118](https://github.com/artsworks/f1-pitwall/pull/118),
  [`cfa33e7`](https://github.com/artsworks/f1-pitwall/commit/cfa33e7a29dba59b3d05c9d507ec326fa96385a0))

### Features

- **rules**: Double radio phrasing variants
  ([#119](https://github.com/artsworks/f1-pitwall/pull/119),
  [`d7b5187`](https://github.com/artsworks/f1-pitwall/commit/d7b518769e8a2b6c9df0d4bf16f9a15d5a8c2eb7))


## v0.28.0 (2026-10-18)

### Bug Fixes

- **battle**: Treat unknown rival tyre age as unknown, not fresh
  ([#115](https://github.com/artsworks/f1-pitwall/pull/115),
  [`262d0f6`](https://github.com/artsworks/f1-pitwall/commit/262d0f616a8a0756b7b547e720a25c9da804ede1))

- **rules**: Guard battle_let_by on rival change without a generic bind
  ([#115](https://github.com/artsworks/f1-pitwall/pull/115),
  [`262d0f6`](https://github.com/artsworks/f1-pitwall/commit/262d0f616a8a0756b7b547e720a25c9da804ede1))

- **rules**: Re-arm and revalidate battle_let_by, bind it to the rival
  ([#115](https://github.com/artsworks/f1-pitwall/pull/115),
  [`262d0f6`](https://github.com/artsworks/f1-pitwall/commit/262d0f616a8a0756b7b547e720a25c9da804ede1))

### Features

- **rules**: Add battle_let_by call when defending is hopeless
  ([#115](https://github.com/artsworks/f1-pitwall/pull/115),
  [`262d0f6`](https://github.com/artsworks/f1-pitwall/commit/262d0f616a8a0756b7b547e720a25c9da804ede1))

### Testing

- **dispatcher**: Mark battle tyre data as known
  ([#115](https://github.com/artsworks/f1-pitwall/pull/115),
  [`262d0f6`](https://github.com/artsworks/f1-pitwall/commit/262d0f616a8a0756b7b547e720a25c9da804ede1))


## v0.27.0 (2026-10-18)

### Bug Fixes

- **config**: Reject zero pressure_size_large_c and medium above large
  ([#112](https://github.com/artsworks/f1-pitwall/pull/112),
  [`535de7d`](https://github.com/artsworks/f1-pitwall/commit/535de7dab75165aa9e289cca35cd60e944ab9066))

- **config**: Roll back a live override the reload rejects
  ([#112](https://github.com/artsworks/f1-pitwall/pull/112),
  [`535de7d`](https://github.com/artsworks/f1-pitwall/commit/535de7dab75165aa9e289cca35cd60e944ab9066))

- **engine**: Count run fuel from the end of the out lap
  ([#112](https://github.com/artsworks/f1-pitwall/pull/112),
  [`535de7d`](https://github.com/artsworks/f1-pitwall/commit/535de7dab75165aa9e289cca35cd60e944ab9066))

- **engine**: Drop run fuel deltas a flashback erased
  ([#112](https://github.com/artsworks/f1-pitwall/pull/112),
  [`535de7d`](https://github.com/artsworks/f1-pitwall/commit/535de7dab75165aa9e289cca35cd60e944ab9066))

- **engine**: Drop session fuel deltas a flashback erased
  ([#112](https://github.com/artsworks/f1-pitwall/pull/112),
  [`535de7d`](https://github.com/artsworks/f1-pitwall/commit/535de7dab75165aa9e289cca35cd60e944ab9066))

- **setup**: Ignore experiment-tier recs for the stop wing call
  ([#112](https://github.com/artsworks/f1-pitwall/pull/112),
  [`535de7d`](https://github.com/artsworks/f1-pitwall/commit/535de7dab75165aa9e289cca35cd60e944ab9066))

- **state**: Cap the tyre window inset on narrow windows
  ([#112](https://github.com/artsworks/f1-pitwall/pull/112),
  [`535de7d`](https://github.com/artsworks/f1-pitwall/commit/535de7dab75165aa9e289cca35cd60e944ab9066))

- **state**: Fill sector benchmarks in races
  ([#112](https://github.com/artsworks/f1-pitwall/pull/112),
  [`535de7d`](https://github.com/artsworks/f1-pitwall/commit/535de7dab75165aa9e289cca35cd60e944ab9066))

- **web**: Wrap setup legend so TRY note is not clipped
  ([#112](https://github.com/artsworks/f1-pitwall/pull/112),
  [`535de7d`](https://github.com/artsworks/f1-pitwall/commit/535de7dab75165aa9e289cca35cd60e944ab9066))

### Features

- Retune setup and pressure advice ([#112](https://github.com/artsworks/f1-pitwall/pull/112),
  [`535de7d`](https://github.com/artsworks/f1-pitwall/commit/535de7dab75165aa9e289cca35cd60e944ab9066))

- Retune setup and pressure advice, redesign the garage screen
  ([#112](https://github.com/artsworks/f1-pitwall/pull/112),
  [`535de7d`](https://github.com/artsworks/f1-pitwall/commit/535de7dab75165aa9e289cca35cd60e944ab9066))

- **web**: Name sector benchmarks on the pit board
  ([#112](https://github.com/artsworks/f1-pitwall/pull/112),
  [`535de7d`](https://github.com/artsworks/f1-pitwall/commit/535de7dab75165aa9e289cca35cd60e944ab9066))

- **web**: Redesign garage screen with run fuel, tyre temps and sector gaps
  ([#112](https://github.com/artsworks/f1-pitwall/pull/112),
  [`535de7d`](https://github.com/artsworks/f1-pitwall/commit/535de7dab75165aa9e289cca35cd60e944ab9066))

- **web**: Show the pit board in races while in the pit lane
  ([#112](https://github.com/artsworks/f1-pitwall/pull/112),
  [`535de7d`](https://github.com/artsworks/f1-pitwall/commit/535de7dab75165aa9e289cca35cd60e944ab9066))


## v0.26.0 (2026-10-18)

### Continuous Integration

- Split PR tests into fast and replay shards
  ([#109](https://github.com/artsworks/f1-pitwall/pull/109),
  [`19ff630`](https://github.com/artsworks/f1-pitwall/commit/19ff6305e4e1624ccc7b060969b04b614e5bb9aa))

### Documentation

- Add search and social metadata to the landing page
  ([#110](https://github.com/artsworks/f1-pitwall/pull/110),
  [`bc236d9`](https://github.com/artsworks/f1-pitwall/commit/bc236d9769e3122acde952019e153af916741aa4))

- Embed the demo video on the landing page
  ([#111](https://github.com/artsworks/f1-pitwall/pull/111),
  [`be434b5`](https://github.com/artsworks/f1-pitwall/commit/be434b544f227dccd6f3b96d616839e7bc0e2797))

- Use the owner's favicon set and add a web manifest
  ([#110](https://github.com/artsworks/f1-pitwall/pull/110),
  [`bc236d9`](https://github.com/artsworks/f1-pitwall/commit/bc236d9769e3122acde952019e153af916741aa4))

### Features

- **radio**: Budget steps silence, 15 and unlimited
  ([#114](https://github.com/artsworks/f1-pitwall/pull/114),
  [`9304ddf`](https://github.com/artsworks/f1-pitwall/commit/9304ddf1ab86d4e723bdf94fff22142873a198c9))


## v0.25.0 (2026-10-16)

### Features

- **strategy**: Model degradation and plan uncertainty
  ([#108](https://github.com/artsworks/f1-pitwall/pull/108),
  [`5ab3724`](https://github.com/artsworks/f1-pitwall/commit/5ab37249705de64b64b68b8345a0e90b772ae7fc))


## v0.24.0 (2026-10-16)

### Bug Fixes

- **strategy**: Ignore neutralised laps and require net-positive confirmation laps
  ([#107](https://github.com/artsworks/f1-pitwall/pull/107),
  [`1db3c84`](https://github.com/artsworks/f1-pitwall/commit/1db3c8466e28537f9b64a3b5923a0fe1ea713ba4))

### Features

- **strategy**: Wet-weather pit calls from projected net time gain
  ([#107](https://github.com/artsworks/f1-pitwall/pull/107),
  [`1db3c84`](https://github.com/artsworks/f1-pitwall/commit/1db3c8466e28537f9b64a3b5923a0fe1ea713ba4))


## v0.23.0 (2026-10-16)

### Bug Fixes

- **strategy**: Score cover and defend plans by cover decision
  ([#106](https://github.com/artsworks/f1-pitwall/pull/106),
  [`5e855c2`](https://github.com/artsworks/f1-pitwall/commit/5e855c2c4644bcbbd0500c39891cf86f27ea939c))

### Chores

- **deps**: Bump astral-sh/setup-uv ([#103](https://github.com/artsworks/f1-pitwall/pull/103),
  [`8db1455`](https://github.com/artsworks/f1-pitwall/commit/8db145574278652a2fe917c34c690d85a09ecf97))

### Features

- **strategy**: Recompute true rival under SC and skip owed stops in wet races
  ([#106](https://github.com/artsworks/f1-pitwall/pull/106),
  [`5e855c2`](https://github.com/artsworks/f1-pitwall/commit/5e855c2c4644bcbbd0500c39891cf86f27ea939c))

- **strategy**: Track the true rival across pit cycles
  ([#106](https://github.com/artsworks/f1-pitwall/pull/106),
  [`5e855c2`](https://github.com/artsworks/f1-pitwall/commit/5e855c2c4644bcbbd0500c39891cf86f27ea939c))


## v0.22.0 (2026-10-16)

### Bug Fixes

- **test**: Record pit rules without replay coverage
  ([#104](https://github.com/artsworks/f1-pitwall/pull/104),
  [`3c12031`](https://github.com/artsworks/f1-pitwall/commit/3c12031450c6e61e0a74bb49c91f6af24c5cc4d4))

### Features

- **strategy**: Cover or defend against an undercut from the car behind
  ([#104](https://github.com/artsworks/f1-pitwall/pull/104),
  [`3c12031`](https://github.com/artsworks/f1-pitwall/commit/3c12031450c6e61e0a74bb49c91f6af24c5cc4d4))

- **strategy**: Defend against undercuts from behind
  ([#104](https://github.com/artsworks/f1-pitwall/pull/104),
  [`3c12031`](https://github.com/artsworks/f1-pitwall/commit/3c12031450c6e61e0a74bb49c91f6af24c5cc4d4))


## v0.21.0 (2026-10-16)

### Documentation

- **strategy**: Document Monte Carlo plan agreement and tyre degradation
  ([#105](https://github.com/artsworks/f1-pitwall/pull/105),
  [`218518d`](https://github.com/artsworks/f1-pitwall/commit/218518d7d1f38bdb04483149cc694f3303704daf))

### Features

- **deg**: Traffic and thermal lap exclusion, quadratic deg with cliff solve
  ([#105](https://github.com/artsworks/f1-pitwall/pull/105),
  [`218518d`](https://github.com/artsworks/f1-pitwall/commit/218518d7d1f38bdb04483149cc694f3303704daf))

- **deg**: Traffic and thermal lap exclusion, quadratic deg, Monte Carlo plan confidence
  ([#105](https://github.com/artsworks/f1-pitwall/pull/105),
  [`218518d`](https://github.com/artsworks/f1-pitwall/commit/218518d7d1f38bdb04483149cc694f3303704daf))

- **strategy**: Monte Carlo pit plan agreement scales plan confidence
  ([#105](https://github.com/artsworks/f1-pitwall/pull/105),
  [`218518d`](https://github.com/artsworks/f1-pitwall/commit/218518d7d1f38bdb04483149cc694f3303704daf))


## v0.20.1 (2026-10-16)

### Bug Fixes

- Handle time rewinds and zero closing rates
  ([#102](https://github.com/artsworks/f1-pitwall/pull/102),
  [`b47a564`](https://github.com/artsworks/f1-pitwall/commit/b47a564f3fd9084e7d3b07ccf0bf1199fffe0f93))

- Log predicate evaluation failures ([#102](https://github.com/artsworks/f1-pitwall/pull/102),
  [`b47a564`](https://github.com/artsworks/f1-pitwall/commit/b47a564f3fd9084e7d3b07ccf0bf1199fffe0f93))

- Surface suppressed rule evaluation errors
  ([#102](https://github.com/artsworks/f1-pitwall/pull/102),
  [`b47a564`](https://github.com/artsworks/f1-pitwall/commit/b47a564f3fd9084e7d3b07ccf0bf1199fffe0f93))

- Surface swallowed rule and predicate errors, enforce BLE001, add rule replay coverage
  ([#102](https://github.com/artsworks/f1-pitwall/pull/102),
  [`b47a564`](https://github.com/artsworks/f1-pitwall/commit/b47a564f3fd9084e7d3b07ccf0bf1199fffe0f93))

### Chores

- Enforce BLE001 and remove dead declarations
  ([#102](https://github.com/artsworks/f1-pitwall/pull/102),
  [`b47a564`](https://github.com/artsworks/f1-pitwall/commit/b47a564f3fd9084e7d3b07ccf0bf1199fffe0f93))

### Testing

- Cache race_stream and precompile packet layouts
  ([#102](https://github.com/artsworks/f1-pitwall/pull/102),
  [`b47a564`](https://github.com/artsworks/f1-pitwall/commit/b47a564f3fd9084e7d3b07ccf0bf1199fffe0f93))

- Guard that every YAML rule has a replay test
  ([#102](https://github.com/artsworks/f1-pitwall/pull/102),
  [`b47a564`](https://github.com/artsworks/f1-pitwall/commit/b47a564f3fd9084e7d3b07ccf0bf1199fffe0f93))

- Replay coverage for rules without tests ([#102](https://github.com/artsworks/f1-pitwall/pull/102),
  [`b47a564`](https://github.com/artsworks/f1-pitwall/commit/b47a564f3fd9084e7d3b07ccf0bf1199fffe0f93))

- Shared Snapshot and SessionState factories
  ([#102](https://github.com/artsworks/f1-pitwall/pull/102),
  [`b47a564`](https://github.com/artsworks/f1-pitwall/commit/b47a564f3fd9084e7d3b07ccf0bf1199fffe0f93))


## v0.20.0 (2026-10-16)

### Bug Fixes

- **cleanup**: Credit imports only to the exact recording file
  ([#94](https://github.com/artsworks/f1-pitwall/pull/94),
  [`4bd4c08`](https://github.com/artsworks/f1-pitwall/commit/4bd4c081bcfc6d5837b482cecd340f22d5dc1b5e))

- **cli**: Learn only stable recordings in cleanup --learn
  ([#94](https://github.com/artsworks/f1-pitwall/pull/94),
  [`4bd4c08`](https://github.com/artsworks/f1-pitwall/commit/4bd4c081bcfc6d5837b482cecd340f22d5dc1b5e))

- **cli**: Make trim safe for same-path, zst and profile outputs
  ([#94](https://github.com/artsworks/f1-pitwall/pull/94),
  [`4bd4c08`](https://github.com/artsworks/f1-pitwall/commit/4bd4c081bcfc6d5837b482cecd340f22d5dc1b5e))

- **cli**: Show plain subcommand usage, simplify --help-all
  ([#94](https://github.com/artsworks/f1-pitwall/pull/94),
  [`4bd4c08`](https://github.com/artsworks/f1-pitwall/commit/4bd4c081bcfc6d5837b482cecd340f22d5dc1b5e))

- **cli**: Trim rejects unsafe output names and existing raw siblings
  ([#94](https://github.com/artsworks/f1-pitwall/pull/94),
  [`4bd4c08`](https://github.com/artsworks/f1-pitwall/commit/4bd4c081bcfc6d5837b482cecd340f22d5dc1b5e))

### Features

- Remove derive command, layer CLI help, add cleanup --learn
  ([#94](https://github.com/artsworks/f1-pitwall/pull/94),
  [`4bd4c08`](https://github.com/artsworks/f1-pitwall/commit/4bd4c081bcfc6d5837b482cecd340f22d5dc1b5e))

- Simplify CLI help and cleanup learning ([#94](https://github.com/artsworks/f1-pitwall/pull/94),
  [`4bd4c08`](https://github.com/artsworks/f1-pitwall/commit/4bd4c081bcfc6d5837b482cecd340f22d5dc1b5e))

- **cli**: Restore trim as an advanced command
  ([#94](https://github.com/artsworks/f1-pitwall/pull/94),
  [`4bd4c08`](https://github.com/artsworks/f1-pitwall/commit/4bd4c081bcfc6d5837b482cecd340f22d5dc1b5e))


## v0.19.0 (2026-10-16)

### Bug Fixes

- **radio**: A duplicate of the line on air merges into it
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- **radio**: Add owner race grades and meet them in the dispatcher
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- **radio**: Battle focus drops every info call
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- **radio**: Battle focus drops P3 info only, keep P2 news such as lap one position
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- **radio**: Drop calls that reach the age cap or deadline exactly
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- **radio**: Keep driver replies when a finish or red flag call flushes the queue
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- **radio**: Keep P2 info in focus, let tactical calls drop coaching and battle info
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- **radio**: Replies never cut, execution cuts by priority or promotion, cut replies requeue only
  after a hazard ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- **radio**: Safety leads and cuts promoted box calls, queue orders by priority inside a class
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- **radio**: The newest decision wins an unscored conflict, drop decision_missed_s
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- **rules**: Speak the contact check at once, the damage report or all clear supersedes it
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- **rules**: Split the battle cooldown group by side and let box_now supersede box_in_n
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- **rules**: The box call drops the tyre life line it carries
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- **rules**: Weigh pit risk once, score the overcut by the optimiser minimum, let the corner cost
  line supersede praise ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

### Features

- **radio**: Tactical lines join a digest on a quiet stretch
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- **rules**: Add the contradicts relation, puncture, fuel and contact checks drop the lines they
  contradict ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

### Testing

- Add owner race grading batch 4 to radio workbook
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- Apply owner regrade of g003 and g006 ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- Classify remaining batch 4 radio mismatches
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- Regrade batch 4 radio scenarios with the pit wall priority model
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- **radio**: Encode hypothetical rules on packaged rules and add the relations they need
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))

- **radio**: Regrade the last five scenarios to the dispatcher output
  ([#99](https://github.com/artsworks/f1-pitwall/pull/99),
  [`86b17bf`](https://github.com/artsworks/f1-pitwall/commit/86b17bfb38f4a128bd4cbc255ea6c55beadbd7e8))


## v0.18.0 (2026-10-15)

### Features

- **tracks**: Promote learned overlays from the 2026-10-16 pack
  ([#100](https://github.com/artsworks/f1-pitwall/pull/100),
  [`4290e8e`](https://github.com/artsworks/f1-pitwall/commit/4290e8e4459b00d9bd1b05992a16e9dd9e5270a4))


## v0.17.0 (2026-10-15)

### Bug Fixes

- **speech**: Drop failed OmniVoice calls when Piper is missing
  ([#98](https://github.com/artsworks/f1-pitwall/pull/98),
  [`db96ab1`](https://github.com/artsworks/f1-pitwall/commit/db96ab1535f70483ded852af34a35c2dd43cfbba))

### Features

- **speech**: Remove the SAPI speech engine ([#98](https://github.com/artsworks/f1-pitwall/pull/98),
  [`db96ab1`](https://github.com/artsworks/f1-pitwall/commit/db96ab1535f70483ded852af34a35c2dd43cfbba))


## v0.16.0 (2026-10-15)

### Features

- **learning**: Run upkeep at session end with progress lines
  ([#97](https://github.com/artsworks/f1-pitwall/pull/97),
  [`3ce3da6`](https://github.com/artsworks/f1-pitwall/commit/3ce3da6987fc9894b4b82a78c27707f59fdf8aa9))


## v0.15.0 (2026-10-14)

### Features

- **learning**: Write track overlays during startup upkeep
  ([#96](https://github.com/artsworks/f1-pitwall/pull/96),
  [`941a69e`](https://github.com/artsworks/f1-pitwall/commit/941a69e03dc705e09f3013b4871afadf1a850f2c))


## v0.14.1 (2026-10-14)

### Bug Fixes

- **input**: Require a 2 s hold to toggle radio silence
  ([#95](https://github.com/artsworks/f1-pitwall/pull/95),
  [`16eae48`](https://github.com/artsworks/f1-pitwall/commit/16eae486ea154ef79901de1e99ab4c05b11d7acc))

### Chores

- **cli**: Write debriefs to debriefs/ instead of the repo root
  ([#92](https://github.com/artsworks/f1-pitwall/pull/92),
  [`092c274`](https://github.com/artsworks/f1-pitwall/commit/092c274ddcf17c8dadce4afcb758245806bca31c))

### Documentation

- **cli**: Add a benefit line and an example to every command's -h
  ([#93](https://github.com/artsworks/f1-pitwall/pull/93),
  [`783356d`](https://github.com/artsworks/f1-pitwall/commit/783356db4347d0f9dfeb067584add188ff98479a))


## v0.14.0 (2026-10-13)

### Chores

- **audio**: Log OmniVoice render timing and add --log-level
  ([#89](https://github.com/artsworks/f1-pitwall/pull/89),
  [`7f0dc4f`](https://github.com/artsworks/f1-pitwall/commit/7f0dc4fd3883585cac6d50964eea139e4e7c737d))

### Documentation

- Rewrap the voice warm paragraph ([#90](https://github.com/artsworks/f1-pitwall/pull/90),
  [`22285bd`](https://github.com/artsworks/f1-pitwall/commit/22285bde64b3dfad8cb24ac347ea0f6b758315d5))

- State the startup prerender once per engine section
  ([#90](https://github.com/artsworks/f1-pitwall/pull/90),
  [`22285bd`](https://github.com/artsworks/f1-pitwall/commit/22285bde64b3dfad8cb24ac347ea0f6b758315d5))

### Features

- **audio**: Pre-render startup lines and widen the voice warm list
  ([#90](https://github.com/artsworks/f1-pitwall/pull/90),
  [`22285bd`](https://github.com/artsworks/f1-pitwall/commit/22285bde64b3dfad8cb24ac347ea0f6b758315d5))

- **audio**: Skip names and measured values in voice warm --replay
  ([#90](https://github.com/artsworks/f1-pitwall/pull/90),
  [`22285bd`](https://github.com/artsworks/f1-pitwall/commit/22285bde64b3dfad8cb24ac347ea0f6b758315d5))


## v0.13.0 (2026-10-13)

### Features

- **audio**: Wait for OmniVoice before Piper provisional audio
  ([#88](https://github.com/artsworks/f1-pitwall/pull/88),
  [`06e7262`](https://github.com/artsworks/f1-pitwall/commit/06e7262adddbc57578ed9e5983afe376b3add370))


## v0.12.0 (2026-10-13)

### Documentation

- **mockup**: Add looping demo race to the reimagined dashboard
  ([#86](https://github.com/artsworks/f1-pitwall/pull/86),
  [`a5896d7`](https://github.com/artsworks/f1-pitwall/commit/a5896d72b632ee4fdf234e9df04aa93e309a9b57))

### Features

- **audio**: Send only text to OmniVoice locked server
  ([#87](https://github.com/artsworks/f1-pitwall/pull/87),
  [`3cdbf65`](https://github.com/artsworks/f1-pitwall/commit/3cdbf65d8bcaf6e1efcd59f180f4d4f36f0f1a1b))

### Refactoring

- **config**: Drop unused OmniVoice settings fields
  ([#87](https://github.com/artsworks/f1-pitwall/pull/87),
  [`3cdbf65`](https://github.com/artsworks/f1-pitwall/commit/3cdbf65d8bcaf6e1efcd59f180f4d4f36f0f1a1b))


## v0.11.0 (2026-10-13)

### Bug Fixes

- **audio**: Close phrase-cache temp fd before writing
  ([#73](https://github.com/artsworks/f1-pitwall/pull/73),
  [`04ee075`](https://github.com/artsworks/f1-pitwall/commit/04ee075e7037bf37e06d5162abc56ae2c6ad8038))

- **audio**: Handle playback and fallback edge cases
  ([#73](https://github.com/artsworks/f1-pitwall/pull/73),
  [`04ee075`](https://github.com/artsworks/f1-pitwall/commit/04ee075e7037bf37e06d5162abc56ae2c6ad8038))

- **audio**: Harden OmniVoice fallback, warm/say engine and phrase cache
  ([#73](https://github.com/artsworks/f1-pitwall/pull/73),
  [`04ee075`](https://github.com/artsworks/f1-pitwall/commit/04ee075e7037bf37e06d5162abc56ae2c6ad8038))

- **audio**: Route fallback callbacks and warm selected engine
  ([#73](https://github.com/artsworks/f1-pitwall/pull/73),
  [`04ee075`](https://github.com/artsworks/f1-pitwall/commit/04ee075e7037bf37e06d5162abc56ae2c6ad8038))

- **voice**: Restore pywin32 type-ignore comments
  ([#73](https://github.com/artsworks/f1-pitwall/pull/73),
  [`04ee075`](https://github.com/artsworks/f1-pitwall/commit/04ee075e7037bf37e06d5162abc56ae2c6ad8038))

### Documentation

- Focus the landing page and move dev docs behind a developer guide
  ([#84](https://github.com/artsworks/f1-pitwall/pull/84),
  [`f535e5a`](https://github.com/artsworks/f1-pitwall/commit/f535e5a26fdc58bb0aa8ceb6d30c727cba1f73fd))

- Trim Design and Reference sidebar links to core docs
  ([#84](https://github.com/artsworks/f1-pitwall/pull/84),
  [`f535e5a`](https://github.com/artsworks/f1-pitwall/commit/f535e5a26fdc58bb0aa8ceb6d30c727cba1f73fd))

- **audio**: Document the speech engine contract
  ([#73](https://github.com/artsworks/f1-pitwall/pull/73),
  [`04ee075`](https://github.com/artsworks/f1-pitwall/commit/04ee075e7037bf37e06d5162abc56ae2c6ad8038))

- **audio**: State the callback order in the engine skill
  ([#73](https://github.com/artsworks/f1-pitwall/pull/73),
  [`04ee075`](https://github.com/artsworks/f1-pitwall/commit/04ee075e7037bf37e06d5162abc56ae2c6ad8038))

### Features

- **audio**: Add opt-in OmniVoice TTS backend
  ([#73](https://github.com/artsworks/f1-pitwall/pull/73),
  [`04ee075`](https://github.com/artsworks/f1-pitwall/commit/04ee075e7037bf37e06d5162abc56ae2c6ad8038))

- **audio**: Add vendor-independent speaker contract
  ([#73](https://github.com/artsworks/f1-pitwall/pull/73),
  [`04ee075`](https://github.com/artsworks/f1-pitwall/commit/04ee075e7037bf37e06d5162abc56ae2c6ad8038))

- **audio**: Add vendor-independent speech engine contract
  ([#73](https://github.com/artsworks/f1-pitwall/pull/73),
  [`04ee075`](https://github.com/artsworks/f1-pitwall/commit/04ee075e7037bf37e06d5162abc56ae2c6ad8038))


## v0.10.0 (2026-10-13)

### Documentation

- Add AGENTS.md with skills and do-not-commit list
  ([#83](https://github.com/artsworks/f1-pitwall/pull/83),
  [`e401a12`](https://github.com/artsworks/f1-pitwall/commit/e401a1277ca312bb4974e086e114a6df8bb7a46f))

- **skills**: Add track overlay sync skill ([#83](https://github.com/artsworks/f1-pitwall/pull/83),
  [`e401a12`](https://github.com/artsworks/f1-pitwall/commit/e401a1277ca312bb4974e086e114a6df8bb7a46f))

### Features

- **tracks**: Promote learned fuel and thresholds for tracks 0, 5, 7, 10, 17
  ([#83](https://github.com/artsworks/f1-pitwall/pull/83),
  [`e401a12`](https://github.com/artsworks/f1-pitwall/commit/e401a1277ca312bb4974e086e114a6df8bb7a46f))


## v0.9.0 (2026-10-13)

### Features

- **radio**: Drop calls older than 10 s, split voice queue and TTS latency
  ([#81](https://github.com/artsworks/f1-pitwall/pull/81),
  [`ce1ea88`](https://github.com/artsworks/f1-pitwall/commit/ce1ea880f066bf8403ee17ae90afcf2680244329))

### Testing

- Drop Windows-only test tweaks unrelated to the age cap
  ([#81](https://github.com/artsworks/f1-pitwall/pull/81),
  [`ce1ea88`](https://github.com/artsworks/f1-pitwall/commit/ce1ea880f066bf8403ee17ae90afcf2680244329))


## v0.8.0 (2026-10-13)

### Documentation

- **skills**: Add replay history and press timing notes for dashboard tests
  ([#79](https://github.com/artsworks/f1-pitwall/pull/79),
  [`2be063e`](https://github.com/artsworks/f1-pitwall/commit/2be063e4afd312e588ac84cb3ee9f3050c0830f6))

### Features

- **quali**: Confirm push laps flagged as cool
  ([#79](https://github.com/artsworks/f1-pitwall/pull/79),
  [`2be063e`](https://github.com/artsworks/f1-pitwall/commit/2be063e4afd312e588ac84cb3ee9f3050c0830f6))


## v0.7.1 (2026-10-13)

### Bug Fixes

- **quali**: Gate cool-lap pushes on projected fuel
  ([#78](https://github.com/artsworks/f1-pitwall/pull/78),
  [`3a3c9f2`](https://github.com/artsworks/f1-pitwall/commit/3a3c9f2f348380707fe76112e290733740680340))

- **quali**: Reserve cool + hot fuel for extra cool laps and retries
  ([#78](https://github.com/artsworks/f1-pitwall/pull/78),
  [`3a3c9f2`](https://github.com/artsworks/f1-pitwall/commit/3a3c9f2f348380707fe76112e290733740680340))

### Continuous Integration

- Skip docs-only runs, cancel superseded PR runs, skip non-releasing release jobs
  ([#82](https://github.com/artsworks/f1-pitwall/pull/82),
  [`4c45f95`](https://github.com/artsworks/f1-pitwall/commit/4c45f956356a28af6442418fc10e1fbd9b79479f))


## v0.7.0 (2026-10-13)

### Bug Fixes

- **web**: Fit last lap, energy and damage text in the tiles
  ([#77](https://github.com/artsworks/f1-pitwall/pull/77),
  [`dbbe8ca`](https://github.com/artsworks/f1-pitwall/commit/dbbe8cac2c641e07ef4b464a4a7cb41b6b912f41))

### Documentation

- Refresh dashboard screens for last two laps tile
  ([#77](https://github.com/artsworks/f1-pitwall/pull/77),
  [`dbbe8ca`](https://github.com/artsworks/f1-pitwall/commit/dbbe8cac2c641e07ef4b464a4a7cb41b6b912f41))

- Refresh dashboard screens for per-lap rows
  ([#77](https://github.com/artsworks/f1-pitwall/pull/77),
  [`dbbe8ca`](https://github.com/artsworks/f1-pitwall/commit/dbbe8cac2c641e07ef4b464a4a7cb41b6b912f41))

### Features

- **web**: Show last three laps with two sector comparison rows
  ([#77](https://github.com/artsworks/f1-pitwall/pull/77),
  [`dbbe8ca`](https://github.com/artsworks/f1-pitwall/commit/dbbe8cac2c641e07ef4b464a4a7cb41b6b912f41))

- **web**: Show last two laps with sector deltas, drop energy tile
  ([#77](https://github.com/artsworks/f1-pitwall/pull/77),
  [`dbbe8ca`](https://github.com/artsworks/f1-pitwall/commit/dbbe8cac2c641e07ef4b464a4a7cb41b6b912f41))

- **web**: Show per-lap rows with lap and sector deltas
  ([#77](https://github.com/artsworks/f1-pitwall/pull/77),
  [`dbbe8ca`](https://github.com/artsworks/f1-pitwall/commit/dbbe8cac2c641e07ef4b464a4a7cb41b6b912f41))

- **web**: Shrink damage to a strip and add a last laps tile
  ([#77](https://github.com/artsworks/f1-pitwall/pull/77),
  [`dbbe8ca`](https://github.com/artsworks/f1-pitwall/commit/dbbe8cac2c641e07ef4b464a4a7cb41b6b912f41))

- **web**: Shrink damage to a strip and add last lap and energy tiles
  ([#77](https://github.com/artsworks/f1-pitwall/pull/77),
  [`dbbe8ca`](https://github.com/artsworks/f1-pitwall/commit/dbbe8cac2c641e07ef4b464a4a7cb41b6b912f41))


## v0.6.0 (2026-10-12)

### Bug Fixes

- **quali**: Learn battery use per compound ([#76](https://github.com/artsworks/f1-pitwall/pull/76),
  [`c7d8da3`](https://github.com/artsworks/f1-pitwall/commit/c7d8da395483b931ae3f2ee95e07e4a5fdd0f8d5))

### Features

- **quali**: Lower battery floor on inters and wets for back-to-back hot laps
  ([#76](https://github.com/artsworks/f1-pitwall/pull/76),
  [`c7d8da3`](https://github.com/artsworks/f1-pitwall/commit/c7d8da395483b931ae3f2ee95e07e4a5fdd0f8d5))


## v0.5.1 (2026-10-12)

### Bug Fixes

- Write and read text files as utf-8 so Windows cp1252 no longer crashes debrief
  ([#80](https://github.com/artsworks/f1-pitwall/pull/80),
  [`06a1140`](https://github.com/artsworks/f1-pitwall/commit/06a11401dbea7786202ee804fb04a62ed1d73c1d))


## v0.5.0 (2026-10-12)

### Features

- **voice**: Make voice warm phrase count configurable via speech.warm_phrases
  ([#75](https://github.com/artsworks/f1-pitwall/pull/75),
  [`404fdc4`](https://github.com/artsworks/f1-pitwall/commit/404fdc40462d63a20ca8c4054d48ca61db2a0905))


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
