# Audio dispatcher, UI and transport

## Priorities

| Level | Contents | Behaviour |
|---|---|---|
| 1 critical | safety car / VSC / red flag, *forced* box (fuel, puncture, damage), imminent penalty | cancels speech in progress and speaks immediately |
| 2 tactical | strategic box recommendation (pit window, undercut, SC cheap stop), thermal, wear, fuel saving, undercut threat | preempts level 3, queues behind level 1 |
| 3 informational | gaps, sector deltas, ERS state, position changes | queues; dropped first under pressure |

Level 1 is **not** merged with anything. Plan v1 merged simultaneous P1 and P2 into a
single sentence, which delays the critical half by seconds. Speak the P1, queue the P2.

## Staying quiet

The dispatcher's main job is suppression. Layered, all configurable:

1. **Hysteresis** at the rule level (separate trigger and clear predicates).
2. **Per-rule cooldown** and `max_per_stint`.
3. **Dedupe**: identical or semantically equal text inside a window is dropped.
4. **Global budget**: at most N calls per lap (default 4) and a minimum gap between calls
   (default 3 s), excluding level 1.
5. **Screen-only demotion**: anything already legible on the dashboard is not spoken
   unless it just crossed a threshold.
6. **Workload gating** for level 3 only: hold the message until `m_lapDistance` is inside
   a straight (from per-track zones), so gap updates do not arrive mid-corner.
7. **Quiet mode**: a UI toggle that restricts output to level 1, plus a
   mute-until-lap-N control.
8. **Verbosity presets** (silent / critical / normal / coach) that set the budget and the
   priority floor together, so one control does the work of six
   (`08-configuration.md`).
9. **Acknowledgement**: Fanatec button 2 or spacebar — single press acknowledges, double
   press is negative (`12-driver-input.md`). The dispatcher knows whether a call landed,
   stops repeating it, and backs off rules the driver keeps rejecting.

## Deadlines and revalidation

Per-priority deadlines replace the single 1500 ms TTL, which was shorter than the time a
message takes to speak: level 1 5 s, level 2 3 s, level 3 1.5 s. On top of that, every
message carries its rule's `still_true` predicate; the backend re-evaluates it at the
moment the message reaches the front of the queue and drops it if the world has moved on.
Revalidation is the real requirement — TTL is only a backstop for a wedged client.

## Speech

The dashboard lives on the game PC's second monitor, so speech plays on the game PC too.
That changes the choice of engine.

**Primary (pilot): speech in the backend**, via Windows' built-in SAPI voices through a
small `Speaker` interface, played to a configurable output device (normally the same
headset as the game). Why not the browser:

- Web Speech needs a click to arm, and **clicking the second monitor while the game is
  fullscreen takes focus from the game** (exclusive fullscreen minimises). The browser
  must never need a click mid-session.
- A backend voice starts faster, is not subject to tab throttling or autoplay policy, and
  keeps latency measurement in one process.
- No HTTPS or wake-lock question for the pilot.

**Upgrade (M4): Piper** behind the same interface for a better voice, with the ~50 most
common phrases pre-rendered. **Optional:** Web Speech on a phone client, for when the
backend moves off the game PC.

**Mixing with game audio:** the radio shares the headset with the game, so it needs its
own volume setting and a short radio "click" before each call. Configurable ducking of
the game is not attempted; if calls are hard to hear, lower the in-game volume or send the
radio to a different device.

**The dashboard is display-only during a session.** Every mid-race control — acknowledge,
negative, bookmark, mindset toggle, quiet — has a wheel or keyboard binding
(`12-driver-input.md`). Run the game in borderless windowed mode if you want to click the
dashboard between sessions without minimising it.

## WebSocket protocol

Versioned envelope; two logical channels on one socket:

```json
{"v": 1, "type": "state",  "seq": 4821, "t": 1727241993.412, "payload": { ... }}
{"v": 1, "type": "call",   "seq": 4822, "t": 1727241993.500,
 "payload": {"id": "c-1191", "priority": 1, "text": "Safety car. Box this lap.",
             "deadline_ms": 5000, "tags": ["sc", "pit"]}}
{"v": 1, "type": "cancel", "payload": {"id": "c-1188"}}
```

- `state` at **5–10 Hz**, not 60 Hz. The display cannot be read faster, and the lower rate
  keeps the second-monitor browser cheap on the game's GPU.
- The speaker reports `spoken` / `dropped` (with a reason) so the decision log records
  what the driver actually heard; the dashboard shows the call in the radio log.
- On reconnect the client sends its last `seq` and receives a full state snapshot.
- Handshake rejects a protocol version mismatch loudly rather than rendering nonsense.

## Dashboard

Design rules: readable at arm's length in peripheral vision, no animation, state changes
visible as a colour and text change on the same frame.

```
┌─────────────────────────────────────────────────────────────────────┐
│ LIVE  60Hz  age 18ms        RACE  LAP 24/44        FUEL +0.4 LAPS   │
├──────────────────────────────┬──────────────────────────────────────┤
│ TYRES            MED  14 LAPS│ AHEAD   P4 NORRIS      +1.4  (HARD)  │
│  FL 106  OVERHEAT │ FR  98 OK│ BEHIND  P6 LECLERC     -2.1  DRS     │
│  RL  94 OK        │ RR  95 OK│ PIT EXIT RIVAL         P7 SAINZ      │
│  WEAR 38%   LIFE ~5 LAPS     │ PIT WINDOW  LAP 26-28  UNDERCUT +1.2 │
├──────────────────────────────┴──────────────────────────────────────┤
│  >>>  BOX LAP 26 — UNDERCUT NORRIS  <<<                             │
├─────────────────────────────────────────────────────────────────────┤
│ RADIO      MINDSET [AGGR]| BAL          NORMAL ▾         [QUIET ▢] │
│ L24 ▸ "Front left 106. Ease the trail braking."              ACK    │
│ L23 ▸ "Norris is 1.4 ahead and losing two tenths a lap."     NEG    │
└─────────────────────────────────────────────────────────────────────┘
```

- Primary banner ≥ 72 px, secondary blocks ≥ 36 px, monospace, high contrast.
- The radio log shows the last ~12 calls with their ACK / NEG outcome; `/radio` is a
  log-only page in large type for a narrow second screen.
- The mindset (AGGRESSIVE / BALANCED in the pilot) is always visible and switchable in
  one press of its wheel/keyboard binding; it is the control most likely to be used mid-race.
- Green optimal, amber warning, red act-now — always paired with a word, never colour
  alone.
- The **connection and packet-age indicator is load-bearing**: a frozen dashboard that
  looks alive is worse than a blank one. Grey everything out above 1 s of staleness.
- Second monitor on the game PC is the primary client; the same page works on a phone.
- CSS is vendored in the repo. No CDN — the race PC may be offline and nothing should
  need the internet mid-race.
