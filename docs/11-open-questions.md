# Open questions

Decisions that change the design and that should not be made unilaterally. Recommended
answers are given; only the items under "Settled" are settled.

## Settled

**Repository.** `f1-pitwall`, **public**, **GPLv3** (copyleft: distributed improvements must stay
open source; commercial use is permitted by the licence). Layout tables are re-expressed
field definitions, not copies of EA's document; the specification itself is not committed.
No recordings containing friends' online IDs are committed (`pitwall trim` redacts).

**Replay.** Yes, and first: raw datagram recording plus a replay tool that drives the
same ingest path, with review mode and config A/B diffing. See `07-replay-and-debug.md`.

**Mindset control.** Yes: balanced (default) and aggressive for the pilot, implemented as
a vector of numeric biases the rules read, driver-switched only, never applied to
priority-1 calls. Defend and survive after M3. See `08-configuration.md`.

**Game build.** F1 26 only. Parser accepts `m_packetFormat == 2026` and `m_gameYear == 26`
and drops everything else.

**UDP send rate.** 30 Hz (options below 60 to be confirmed in the F1 26 menu; historically
10/20/30/60).

**Driver input.** Fanatec button 2 (bound to UDP Action 1) plus spacebar; single press =
acknowledge, double press = negative. See `12-driver-input.md`.

**Host and client.** Backend on the game PC; dashboard with radio log on its second
monitor, display-only during a session; speech played by the backend.

**Target.** Friends-only online Grand Prix league; tested first on single-player Grand
Prix, with a built-in feedback loop. See `13-league-multiplayer.md`.

**LLM.** None in the pilot, no API key. Revisit only for post-race Q&A.

## Still open

### 1. Speech engine
**Recommendation:** Windows SAPI from the backend for the pilot (no browser click, no
focus stealing), Piper in M4 if the voice disappoints. Web Speech only for an optional
phone client.

### 2. How opinionated should the assistant be by default?
Now partly answered by the mindset control, but the baseline phrasing style is still a
choice. **Recommendation:** directive for priority 1 ("box, box"), advisory with the
number for priority 2 ("undercut is worth 1.2 — box lap 26 if you want it").

### 3. Recordings in git?
Full sessions are ~0.85 GB per hour raw at 30 Hz. **Recommendation:** trimmed clips in-repo as test
fixtures, full sessions outside git, retention policy in config.

### 4. Is a spotter in scope, and when?
Proximity awareness is a different latency class and a different voice from strategy
(`10-angles-not-yet-considered.md`, item 6). It is genuinely useful and genuinely a
second product. **Recommendation:** keep it architecturally separate, decide after M3.

### 5. Career/season context worth modelling?
Component allocation, grid penalties and championship position change what the right call
is, and mostly are not in telemetry, so they would be configured. **Recommendation:**
yes, but only after M3 — and only if you play career rather than one-off races.

### 6. Anything already installed to integrate with?
SimHub, a wheel display, a Streamdeck? The acknowledgement channel is now on wheel
button 2 and spacebar; a Streamdeck could add a mindset toggle and quiet mode.
