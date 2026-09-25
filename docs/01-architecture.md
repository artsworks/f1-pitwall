# Architecture

## Goal

A race engineer that talks to you. It ingests F1 26 UDP telemetry, maintains a model of
the session, decides what is worth saying, and says it — on a phone or second monitor —
fast enough to act on, and rarely enough to trust.

The hard problems are **judgement** (what to say, when to shut up) and **iteration speed**
(tuning without driving a race per change). Parsing and plumbing are the easy parts and
should be commodity code.

## Non-goals

- Not a telemetry dashboard. SimHub and others already do gauges well; the differentiator
  is decisions and speech.
- Not a cloud service. Everything runs on the LAN, offline-capable.
- Not an input-injecting assistant. Read-only telemetry; the driver acts.

## Layers

```
F1 26 (Windows PC) ──UDP 20777──▶ ingest ──▶ session state ──▶ rules ──▶ dispatcher
                                     │             │             │           │
                                 recorder      persistence    decision    WebSocket
                                  (.f1bin)      (SQLite)        log         (8000)
                                     │                                        │
                                  replay ◀── tests / offline tuning      2nd monitor (UI)
                                                                         speech: backend
```

### 1. Ingest
Stdlib `asyncio.DatagramProtocol` on UDP 20777. Reads the 29-byte header, accepts only
F1 26 / format 2026 and dispatches on (`m_packetId`, `m_packetVersion`) to a generated parser. Unknown
combinations are logged once and dropped — never parsed speculatively.

Every received datagram is optionally written verbatim to a recording file with its
receive timestamp, before parsing. Recording is on by default; it costs nothing and is
the raw material for everything else.

### 2. Session state
A single mutable `SessionState` updated in place, from which an immutable snapshot is
taken per evaluation tick (10 Hz). Holds:

- per-car current values (24 slots) with a staleness timestamp per source packet, since
  Session History and Tyre Sets arrive round-robin
- the player's rolling windows: fast (3 s) and slow (30 s) EMAs for tyre and brake
  temperatures
- the current lap accumulator, and completed `LapSummary` rows
- session context: track, session type, total laps, weather forecast, safety car state,
  DRS/active-aero zones, flags

Rules never see raw packets — only the snapshot. This is what makes them testable.

### 3. Rules
Declarative rule definitions evaluated against each snapshot. See `03-strategy.md`.
A rule produces a candidate *call* (text, priority, tags) or nothing.

### 4. Dispatcher
Priority queue with preemption, cooldowns, dedupe, global rate budget, and speak-time
revalidation. Emits to the WebSocket. See `04-audio-ui.md`.

### 5. Clients
One page served over the LAN, opened on a phone or a second monitor. Renders a
high-contrast state view and the radio log. Speech is played by the backend on the game PC
(`04-audio-ui.md`). No build step required to run it.

## Cross-cutting decisions

**Recording and replay are first-class.** The replay tool feeds recorded datagrams through
the exact same ingest path at a configurable speed. Every test, every threshold tune, and
every demo runs through it. A session recorded once is a regression fixture forever.

**Parsers are generated from declarative layouts.** One table per (packet,
version), format 2026 only. EA revises layouts in patches via `m_packetVersion`; a
schema-driven parser turns that from a rewrite into a data edit.

**Persistence over minimalism.** Lap summaries and a 10 Hz downsample go to SQLite. That
history powers the degradation model, measured pit loss, and the post-session debrief. An
in-memory-only design cannot get better over time.

**Latency budget, measured.** Targets: packet → WebSocket p99 < 50 ms; trigger → speech
start p99 < 300 ms for priority 1. Instrument these and expose them in the UI. Optimise
against the measurement, not against a theory about garbage collection.

**Everything a human would tune lives in config**, not in code: thresholds, phrasings,
cooldowns, priorities, per-track data. Layered defaults → profile → track/session overlay
→ live toggles, hot-reloaded and hash-stamped into every recording. See
`08-configuration.md`.

**The driver sets a mindset.** Balanced or aggressive (defend and survive later) is a vector of numeric
biases that rules read (`mode.*`), not a parallel rule set. It biases judgement — pit
gamble margin, energy policy, which rival is watched, how much is said — and never
changes priority-1 facts. The assistant may suggest a change; only the driver makes one.

**Co-hosting with the game is a measured constraint.** Below-normal process priority,
loopback UDP, I/O off the hot path, and a deliberately cheap, display-only dashboard page on
the second monitor. The acceptance test is a frame-time A/B against a no-backend baseline, not a
micro-benchmark. See `09-performance.md`.

## Stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 | struct parsing, fast iteration, fine at ~900 pkt/s |
| Server | FastAPI + uvicorn | WebSocket + static serving in one process |
| Async | stdlib asyncio | runs on Windows; uvloop does not |
| Storage | SQLite (stdlib) | zero-ops, queryable, portable |
| Config | YAML + pydantic | validated, hot-reloadable |
| Frontend | vanilla HTML/CSS/JS, vendored | no build, no CDN, works offline |
| Speech | Windows SAPI (pilot), Piper later | offline, no browser gesture or focus problems |
| Input | UDP Action via `BUTN`; Windows keyboard hook | wheel and spacebar (`12-driver-input.md`) |
| Tooling | uv, ruff, mypy, pytest | fast, standard |

Deliberately no framework on the frontend: the UI is ~10 numbers and a banner, and
avoiding a build step means it can be edited on the race PC between sessions.

## Repository layout

```
pitwall/
├── docs/
├── config/
│   ├── defaults/              # packaged defaults, never user-edited
│   ├── rules/                 # rule definitions (quali.yaml, race.yaml, shared.yaml)
│   ├── mindsets.yaml          # balanced / aggressive parameter sets (defend, survive later)
│   ├── thresholds.yaml        # thermal windows etc. by compound/track
│   └── tracks.yaml            # pit-loss priors, braking zones, pit-exit distance
├── src/pitwall/
│   ├── net/                   # udp listener, recorder, replay
│   ├── protocol/              # layout tables + generated parsers, enums
│   ├── state/                 # session state, lap accumulator, ema, persistence
│   ├── config/                # layered loading, validation, hot reload, hashing
│   ├── rules/                 # rule engine + predicate library
│   ├── model/                 # lap-time / degradation / pit-loss model
│   ├── audio/                 # dispatcher, queue, phrasing
│   └── server/                # FastAPI app, websocket protocol
├── web/                       # index.html, app.js, vendored css
├── tests/
│   ├── fixtures/              # golden packet bytes + short recordings
│   └── replays/               # full-session recordings (git-lfs or external)
└── tools/                     # replay, diff, trim, doctor, calibrate, debrief, bench
```
