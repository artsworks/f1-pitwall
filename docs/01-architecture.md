# Architecture

## Goal

A race engineer for F1 26. It reads UDP telemetry, tracks the session, decides what to
say, and speaks on a phone or second monitor. Calls must arrive in time to act and stay
rare enough to trust.

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
Stdlib `asyncio.DatagramProtocol` listens on UDP 20777. It reads the 29-byte header,
accepts only F1 26 / format 2026, and dispatches on (`m_packetId`, `m_packetVersion`)
to a generated parser. It logs unknown combinations once and drops them without
speculative parsing.

Every received datagram is optionally written verbatim to a recording file with its
receive timestamp before parsing. Recording is on by default, costs nothing, and
supplies the raw data for everything else.

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

Rules see only the snapshot, not raw packets. This makes them testable.

### 3. Rules
Declarative rule definitions evaluated against each snapshot. See `03-strategy.md`.
A rule produces a candidate *call* (text, priority, tags) or nothing.

### 4. Dispatcher
The dispatcher uses a priority queue with preemption, cooldowns, deduplication, a global
rate budget, and speak-time revalidation. It emits to the WebSocket. See `04-audio-ui.md`.

The dispatcher hands each audio call to a speaker. `audio/speaker.py` holds the speaker contract and the engine registry, and `speech.engine` picks the engine id. `pitwall start` and `pitwall replay --serve` connect every engine with `wire_speaker()`, so they have no engine-specific code. Vendor modules such as `piper_tts.py` and `omnivoice_tts.py` only render WAV bytes for `WavSpeaker`.

### 5. Clients
The client is one page served over the LAN for a phone or second monitor. It renders a
high-contrast state view and radio log. The backend on the game PC plays speech
(`04-audio-ui.md`). The page needs no build step.

## Cross-cutting decisions

**Recording and replay are first-class.** The replay tool feeds recorded datagrams through
the same ingest path at a configurable speed. Tests, threshold tuning, and demos all use
it. Each recording becomes a regression fixture.

**Parsers are generated from declarative layouts.** Use one table per (packet, version),
for format 2026 only. EA revises layouts in patches through `m_packetVersion`; a
schema-driven parser makes those updates data edits instead of rewrites.

**Persistence over minimalism.** Store lap summaries and a 10 Hz downsample in SQLite.
That history powers the degradation model, measured pit loss, and post-session debrief.
An in-memory-only design cannot improve over time.

**Measure the latency budget.** Target packet → WebSocket p99 < 50 ms and trigger → speech
start p99 < 300 ms for priority 1. Instrument both and expose them in the UI. Optimise
against measurements, not theories about garbage collection.

**Keep human-tuned values in config**, not code: thresholds, phrasings, cooldowns,
priorities, and per-track data. Layer defaults → profile → track/session overlay
→ live toggles. Hot-reload them and hash-stamp each recording. See `08-configuration.md`.

**The driver sets a mindset.** Balanced or aggressive (defend and survive later) is a set
of numeric biases that rules read (`mode.*`), not a parallel rule set. It biases judgement,
including pit gamble margin, energy policy, rival selection, and call frequency. It never
changes priority-1 facts. The assistant may suggest a change, but only the driver makes it.

**Measure the cost of co-hosting with the game.** Use below-normal process priority,
loopback UDP, and keep I/O off the hot path. Keep the second-monitor dashboard cheap
and display-only. Compare frame times against a no-backend baseline, not a micro-benchmark.
See `09-performance.md`.

## Stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 | struct parsing, fast iteration, fine at ~900 pkt/s |
| Server | FastAPI + uvicorn | WebSocket + static serving in one process |
| Async | stdlib asyncio | runs on Windows; uvloop does not |
| Storage | SQLite (stdlib) | zero-ops, queryable, portable |
| Config | YAML + pydantic | validated, hot-reloadable |
| Frontend | vanilla HTML/CSS/JS, vendored | no build, no CDN, works offline |
| Speech | Piper, Windows SAPI, opt-in OmniVoice on the LAN | no internet, no browser gesture or focus problems |
| Input | UDP Action via `BUTN`; Windows keyboard hook | wheel and spacebar (`12-driver-input.md`) |
| Tooling | uv, ruff, mypy, pytest | fast, standard |

The frontend has no framework because the UI is ~10 numbers and a banner. Without a build
step, it can be edited on the race PC between sessions.

## Repository layout

```
pitwall/
├── docs/
├── scripts/
├── src/pitwall/
│   ├── audio/
│   ├── config/
│   │   └── defaults/          # settings, rules, tracks, voice and menu data
│   ├── input/
│   ├── model/
│   ├── net/
│   ├── protocol/
│   ├── rules/
│   ├── server/
│   ├── state/
│   ├── store/
│   ├── strategy/
│   ├── voice/
│   ├── cli/
│   │   ├── __init__.py
│   │   ├── __main__.py
│   │   ├── common.py
│   │   ├── learn.py
│   │   ├── parser.py
│   │   ├── recordings.py
│   │   ├── serve.py
│   │   └── voice.py
│   └── engine.py
├── tests/
└── web/
```
