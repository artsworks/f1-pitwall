# Open questions

These design decisions should not be made unilaterally. Recommendations are listed, but
only items under "Settled" are settled.

## Settled

**Repository.** `f1-pitwall`, **public**, **GPLv3** (copyleft: distributed improvements
must stay open source; commercial use is permitted by the licence). Layout tables
re-express field definitions rather than copy EA's document. The specification itself is
not committed. No recordings containing friends' online IDs are committed.

**Replay.** Yes, first. Record raw datagrams and use a replay tool that drives the same
ingest path, with review mode and config A/B diffing. See `07-replay-and-debug.md`.

**Mindset control.** Yes. The pilot uses balanced (default) and aggressive modes,
implemented as numeric biases read by rules. Only the driver can switch modes, and modes
never affect priority-1 calls. Defend and survive after M3. See `08-configuration.md`.

**Game build.** F1 26 only. The parser accepts `m_packetFormat == 2026` and drops
everything else. It does not check `m_gameYear`, which the live build does not report as
26.

**UDP send rate.** 30 Hz (confirm options below 60 in the F1 26 menu; historically
10/20/30/60).

**Driver input.** Fanatec button 2 (bound to UDP Action 1) and spacebar. A single press
acknowledges; a double press is negative. See `12-driver-input.md`.

**Host and client.** Backend on the game PC. The display-only dashboard and radio log run
on its second monitor during a session; the backend plays speech.

**Target.** Friends-only online Grand Prix league. Test first on single-player Grand Prix,
with a built-in feedback loop. See `13-league-multiplayer.md`.

**LLM.** None in the pilot and no API key. Since ADR 0009, an optional model may choose
among rule-generated candidates. Start in shadow mode and promote only through the
learning-loop gate. Phrasing stays deterministic.

## Still open

### 1. Speech engine
**Recommendation:** use Windows SAPI from the backend for the pilot, with no browser
click or focus stealing. Use Piper in M4 if the voice disappoints. Use Web Speech only
for an optional phone client.

### 2. How opinionated should the assistant be by default?
The mindset control partly answers this, but the baseline phrasing style remains open.
**Recommendation:** directive for priority 1 ("box, box"), advisory with the number for
priority 2 ("undercut is worth 1.2 — box lap 26 if you want it").

### 3. Recordings in git?
Full sessions use ~0.85 GB per hour raw at 30 Hz. **Recommendation:** keep trimmed clips
in the repo as test fixtures, full sessions outside git, and a retention policy in config.

### 4. Is a spotter in scope, and when?
Proximity awareness has a different latency class and voice from strategy
(`10-angles-not-yet-considered.md`, item 6). It is useful but is a second product.
**Recommendation:** keep it architecturally separate and decide after M3.

### 5. Career/season context worth modelling?
Component allocation, grid penalties, and championship position affect the right call.
Most are absent from telemetry, so configure them. **Recommendation:** yes, but only
after M3 and only if you play career rather than one-off races.

### 6. Anything already installed to integrate with?
SimHub, a wheel display, or a Streamdeck? The acknowledgement channel uses wheel button 2
and spacebar. A Streamdeck could add a mindset toggle and quiet mode.
