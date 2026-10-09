# Audio dispatcher, UI and transport

## Urgency classes

| Class | Rank | Typical calls |
|---|---:|---|
| Safety | 0 | Red flags, yellows, punctures, hazards, and blue flags |
| Execution | 1 | Pit, penalty, and qualifying actions |
| Reply | 2 | Answers to driver-menu requests |
| Tactical | 3 | Battle plans, energy, fuel, and strategy |
| Info | 4 | Gaps, wear, status, and position |
| Coaching | 5 | Lock-ups, saved moments, and driving advice |

Lower ranks enter the queue first. Promoted execution calls rank -1 near a decision
point. Numeric priority still controls budgets, deadlines, quiet mode, and exemptions.

## Preemption

The dispatcher cuts a call only before its estimated speech time ends.

| Incoming class | Calls it can cut |
|---|---|
| Safety | Every call except promoted execution |
| Execution | Info and coaching. Tactical only when promoted |
| Reply | Info and coaching |
| Tactical | Info and coaching |
| Info or coaching | None |

The dispatcher re-queues a cut reply once with a fresh queue time. It does not re-queue
menu prompts.

## Staying quiet

The dispatcher's main job is suppression. Layered, all configurable:

1. **Hysteresis** at the rule level (separate trigger and clear predicates).
2. **Per-rule cooldown** and `max_per_stint`.
3. **Dedupe**: identical or semantically equal text inside a window is dropped.
4. **Global budget**: at most N calls per lap (default 12; driver menu "Radio calls" cycles it) and a minimum gap between calls
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
Revalidation is the real requirement. TTL is only a backstop for a stalled client.
Conflict groups keep a queued call only while its current predicate holds.
A call that waits behind speech starts its deadline when that speech ends.

Execution calls with a decision point calculate seconds to pit entry or the finish line.
The dispatcher promotes calls inside `decision_near_s` and logs their distance and time.
Calls inside `decision_missed_s` lose conflicts because their action window has closed.

An untagged safety call starts a focus window. Focus drops queued info and coaching calls.
It also suppresses new info and coaching calls until the window ends. Systems warnings do
not start focus. The dispatcher drops a location-specific call after a recent safety or
traffic call.

Conflicts first remove stale and explicitly superseded calls. Scored conflicts keep the
higher outcome score. Rotation pairs always choose a seeded random winner, regardless of
their scores. Other live conflicts keep the queued call. `resolved_by` rules suppress
resolved calls in either submission order. Escalations remove lower-state calls. Flush
events remove queued non-safety calls.

Rules can suppress obvious calls and silence provisional checks until an urgent condition
holds. Duplicate queued rules can merge into a count-aware `say_many` line.
A queued call absorbs a related menu reply. The dispatcher can combine due info calls into
one digest with the freshest briefs. It drops overflow and skips digests during battles.

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
negative, radio silent, bookmark, mindset toggle, quiet — has a wheel or keyboard binding
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
- On a phone, tap ENABLE SOUND on the dashboard, or ARM PHONE RADIO on `/radio`, to hear the Piper voice through the phone. The server sends each rendered WAV only to clients that tapped. With SAPI, the phone uses the browser voice instead. Add `?sound=1` to show the button on a desktop browser, or `?sound=0` to hide it on a phone. Other devices need the dashboard PIN. See [getting started](getting-started.md).
- The mindset (AGGRESSIVE / BALANCED in the pilot) is always visible and switchable in
  one press of its wheel/keyboard binding; it is the control most likely to be used mid-race.
- Green optimal, amber warning, red act-now — always paired with a word, never colour
  alone.
- The **connection and packet-age indicator is load-bearing**: a frozen dashboard that
  looks alive is worse than a blank one. Grey everything out above 1 s of staleness.
- Second monitor on the game PC is the primary client; the same page works on a phone.
- CSS is vendored in the repo. No CDN — the race PC may be offline and nothing should
  need the internet mid-race.
