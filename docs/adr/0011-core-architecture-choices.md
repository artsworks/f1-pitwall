# ADR 0011: Core architecture choices

- **Status:** accepted (2026-11). Records the goal, non-goals and cross-cutting decisions
  that the [architecture doc](https://github.com/artsworks/f1-pitwall/blob/4b9a517/docs/01-architecture.md)
  held before it was removed. The repository layout and stack
  now come from the tree and `pyproject.toml`.

## Context

Pitwall is a race engineer for F1 26: it reads UDP telemetry, tracks the session, decides
what to say, and speaks on the game PC with a second monitor or phone. Calls must arrive in
time to act and stay rare enough to trust. The hard problems are judgement (what to say,
when to stay quiet) and iteration speed (tuning without driving a race per change).
Parsing and plumbing are the easy parts.

## Decision

Non-goals:

- Not a telemetry dashboard. SimHub and others do gauges; pitwall's value is decisions and
  speech.
- Not a cloud service. Everything runs on the LAN and works offline.
- Not an input-injecting assistant. Telemetry is read-only; the driver acts.

Cross-cutting decisions:

- **Recording and replay are first-class.** Replay feeds recorded datagrams through the
  same ingest path. Tests, tuning and demos use it, and each recording can become a
  regression fixture.
- **Parsers are generated from declarative layouts**, one table per (packet, version), for
  format 2026 only. EA revises layouts through `m_packetVersion`; a layout edit is then a
  data change, not a rewrite.
- **Rules see only the snapshot.** A rule reads the frozen per-tick snapshot, never raw
  packets, so every rule is testable with replay.
- **Persistence over minimalism.** Lap summaries and a 10 Hz downsample go to SQLite. Every
  model reads its priors from SQLite, fits on committed rows and writes fitted parameters
  back. In-memory state is a cache of the database, never the source of truth.
- **Measure the latency budget.** Target packet → WebSocket p99 < 50 ms and trigger →
  speech start p99 < 300 ms for priority 1. Optimise against measurements, not theories
  about garbage collection.
- **Human-tuned values live in config**, not code: thresholds, phrasings, cooldowns,
  priorities and per-track data, layered defaults → profile → track overlay → live toggles.
- **The mindset is numeric biases**, read by rules as `mode.*`, not a parallel rule set
  (`glossary.md`). It never changes priority-1 facts, and only the driver changes it.
- **Co-hosting with the game is measured**: below-normal process priority, loopback UDP,
  I/O off the hot path, and a cheap display-only dashboard (`09-performance.md`).

Stack choices that are not obvious from `pyproject.toml`:

- Stdlib `asyncio`, because it runs on Windows and uvloop does not.
- Vanilla HTML/CSS/JS with vendored assets: no build step, no CDN, works offline, and can
  be edited on the race PC between sessions. The UI is about ten numbers and a banner.
- Local speech engines (Piper, opt-in OmniVoice on the LAN): no internet, and no browser
  gesture or focus problems.

## Consequences

- A change that needs the network, a cloud service or input injection contradicts this
  record; supersede it with a new ADR first.
- New models persist their parameters in SQLite instead of keeping learned state in memory.
- New tunable values go to YAML config with a default, not a constant in code.
