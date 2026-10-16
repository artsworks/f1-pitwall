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

Lower ranks enter the queue first. Near a decision point, promoted execution calls rank
-1. Numeric priority still controls budgets, deadlines, quiet mode, and exemptions.

## Preemption

The dispatcher cuts a call only before its estimated speech time ends.

| Incoming class | Calls it can cut |
|---|---|
| Safety | Every call except promoted execution |
| Execution | Info and coaching. Tactical only when promoted |
| Reply | Info and coaching |
| Tactical | Info and coaching |
| Info or coaching | None |

The dispatcher re-queues a cut reply once with a fresh queue time. It never re-queues
menu prompts.

## Staying quiet

The dispatcher mainly suppresses calls. Configure each layer:

1. **Hysteresis** at the rule level (separate trigger and clear predicates).
2. **Per-rule cooldown** and `max_per_stint`.
3. **Dedupe**: identical or semantically equal text inside a window is dropped.
4. **Global budget**: allow at most N calls per lap (default 12; the "Radio calls" driver
   menu cycles this value). Enforce a minimum gap between calls (default 3 s), excluding
   level 1.
5. **Screen-only demotion**: anything already legible on the dashboard is not spoken
   unless it just crossed a threshold.
6. **Workload gating** for level 3 only: hold messages until `m_lapDistance` enters a
   straight, based on per-track zones. This prevents gap updates mid-corner.
7. **Quiet mode**: a UI toggle that restricts output to level 1, plus a
   mute-until-lap-N control.
8. **Verbosity presets** (silent / critical / normal / coach) set the budget and priority
   floor together. One control replaces six (`08-configuration.md`).
9. **Acknowledgement**: Fanatec button 2 or spacebar. A single press acknowledges a call;
   a double press rejects it (`12-driver-input.md`). The dispatcher tracks the response,
   stops repeating acknowledged calls, and backs off rules the driver keeps rejecting.

## Deadlines and revalidation

Per-priority deadlines replace the single 1500 ms TTL, which was shorter than speech
time: level 1 gets 5 s, level 2 gets 3 s, and level 3 gets 1.5 s. Each message also carries
its rule's `still_true` predicate. The backend re-evaluates it when the message reaches
the queue front and drops it if the condition no longer holds. Revalidation is required.
TTL is only a backstop for a stalled client. Conflict groups keep a queued call only while
its current predicate holds. A call waiting behind speech starts its deadline when speech
ends.

The `max_call_age_s` cap measures from `trigger_t` and does not reset while a call waits or
is held. A call that reaches the cap or its deadline is late and drops. Replies and menu
prompts are exempt from the cap but keep their deadline. The Piper worker drops calls that exceed the
cap before playback.

Execution calls with a decision point calculate the time to pit entry or the finish line.
The dispatcher promotes calls inside `decision_near_s` and logs distance and time.
Calls inside `decision_missed_s` lose conflicts because their action window has closed.

An untagged safety call starts a focus window. Focus drops queued coaching and P3 info
calls and suppresses new ones until the window ends. P2 info stays. An untagged tactical
call starts its own window that drops coaching, and every info call while a rival is
inside `digest_battle_gap_s`. Systems warnings do not start focus.
The dispatcher drops location-specific calls after recent safety or traffic calls.

Conflicts first remove stale and explicitly superseded calls. Scored conflicts keep the
higher outcome score. Rotation pairs choose a seeded random winner, regardless of score.
Other live conflicts keep the queued call. `resolved_by` rules suppress resolved calls in
either submission order. Escalations remove lower-state calls. Flush events remove
queued calls, except safety calls and replies the driver asked for.

Rules can suppress obvious calls and silence provisional checks until an urgent condition
holds. Duplicate queued rules can merge into a count-aware `say_many` line. A queued call
absorbs a related menu reply. The dispatcher can combine due info calls into one digest
with the freshest briefs. It drops overflow and skips digests during battles.

## Speech

The dashboard and speech run on the game PC. This determines the speech engine.

**Primary (pilot): speech in the backend**, using Windows' built-in SAPI voices through a
small `Speaker` interface. Speech plays on a configurable output device, usually the game
headset. The browser is unsuitable because:

- Web Speech needs a click to arm, and **clicking the second monitor while the game is
  fullscreen takes focus from the game** (exclusive fullscreen minimises). The browser
  must never need a click mid-session.
- A backend voice starts faster, avoids tab throttling and autoplay policy, and keeps
  latency measurement in one process.
- No HTTPS or wake-lock question for the pilot.

**Upgrade (M4): Piper** behind the same interface for a better voice. Pre-render the ~50
most common phrases. **Optional:** use Web Speech on a phone client if the backend moves
off the game PC.

For an opt-in OmniVoice server on a Linux GPU box, setup, registration, caching, and fallback
details are in [Natural voice (OmniVoice)](getting-started.md#natural-voice-omnivoice-opt-in).

### Speech engine contract

`src/pitwall/audio/speaker.py` defines the contract for every speech engine. An engine meets the contract directly, or an adapter in front of it does. `speech.engine` picks an engine id from the registry in the same file.

The dispatcher calls `speak(call)` with the whole `Call`, not only the text. Engines must use these fields:

- `id`: the key for every callback and web frame.
- `priority`: lower numbers play first. A P1 call stops the call that is playing.
- `handoff_age_s` and `max_age_s`: the engine drops the call if its age passes `max_age_s` before playback.

Engines can read `deadline_ms`, `tags`, `lap`, `urgency` and `rank` for logs or extensions. The dispatcher owns `screen_only` and `still_true`. It does not send `screen_only` calls to an engine with `speaks_audio` set to true.

| Protocol | Members | Caller |
|---|---|---|
| `Speaker` | `name`, `speaks_audio`, `supports_audio_stream`, `on_spoken`, `speak`, `cancel`, `close` | dispatcher |
| `StreamableSpeaker` | `on_audio(call_id, priority, wav_bytes)` | engine, to the hub |
| `OutcomeSpeaker` | `on_dropped(call_id)` | engine, to the dispatcher and hub |
| `WarmableSpeaker` | `warm(phrases)` | `pitwall voice warm` |
| `FlushableSpeaker` | `flush()` | replay seek and reset |
| `QueueStatusSpeaker` | `queue_status()` | engine queue count and oldest age |

For each call, a streaming engine calls back in this order:

1. Local playback starts.
2. `on_audio` sends the complete WAV.
3. `on_spoken(call_id, wall_time)` reports the playback start. `Metrics.tts_ms` measures from dispatcher handoff to this time.

Each callback fires at most once per call. A dropped call gets `on_dropped` and no `on_spoken`. After `cancel()`, the engine sends no `on_audio` for that call. After `flush()`, the engine sends no callback for a call it received before the flush.

`wire_speaker()` connects the callbacks for `pitwall start` and `pitwall replay --serve`:

- `on_spoken` goes to `hub.spoken` and `Metrics.note_spoken`.
- `on_dropped` goes to `dispatcher.note_dropped` and `hub.cancel`.
- `on_audio` goes to `hub.audio`, only if the engine streams.

`hub.streams_audio` is true only when the engine sets `supports_audio_stream` and has `on_audio`. SAPI plays audio but produces no WAV bytes, so SAPI does not stream.

Each `audio` frame carries one complete WAV in base64. The WAV is RIFF, mono, 16-bit PCM, with a sample rate above 0 and at least one frame.

`WavSpeaker` in `wav_speaker.py` is the translation layer for WAV engines. It owns the queue, P1 preemption, cancellation, age drops, the phrase cache, the player and the callbacks. A vendor adapter is a `WavRenderer`. Its `render(text, priority)` returns `(wav_bytes, seconds)`. It also gives a `label`, a `cache_identity` and `per_priority`. If a vendor returns PCM or chunks, the adapter collects them in order and converts them with `pcm_to_wav()`. Convert MP3 or Opus to PCM first. If rendering fails, `WavSpeaker` plays the call on its fallback speaker, or calls `on_dropped`.

A renderer can also give `provisional(text, priority)`. On a cache miss, `WavSpeaker` first waits for `render(..., background=False)`. If that render fails or times out, `WavSpeaker` plays the provisional audio and queues `render(..., background=True)` to fill the cache. OmniVoice waits up to `omnivoice_wait_s` and uses Piper as its provisional audio. The age check runs after the wait, so a call that passes `max_age_s` while it waits is dropped.

**Mixing with game audio:** the radio shares the game headset. Give it a separate volume
setting and play a short radio "click" before each call. Do not duck game audio. If calls
are hard to hear, lower the in-game volume or send the radio to another device.

**The dashboard is display-only during a session.** Bind every mid-race control
(acknowledge, negative, radio silent, bookmark, mindset toggle, and quiet) to the wheel or
keyboard (`12-driver-input.md`). Run the game in borderless windowed mode to click the
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

Make the dashboard readable at arm's length in peripheral vision. Use no animation.
Show state changes with colour and text on the same frame.

```
┌─────────────────────────────────────────────────────────────────────┐
│ LIVE  60Hz  age 18ms        RACE  LAP 24/44        FUEL +0.4 LAPS   │
├──────────────────────────────┬──────────────────────────────────────┤
│ TYRES            MED  14 LAPS│ AHEAD   P4 NORRIS      +1.4  (HARD)  │
│  FL 106  OVERHEAT │ FR  98 OK│ BEHIND  P6 LECLERC     -2.1  OT      │
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

- Use a primary banner ≥ 72 px and secondary blocks ≥ 36 px. Use monospace and high contrast.
- The radio log shows the last ~12 calls with their ACK / NEG outcome; `/radio` is a
  log-only page in large type for a narrow second screen.
- On a phone, tap ENABLE SOUND on the dashboard, or ARM PHONE RADIO on `/radio`, to hear the backend voice through the phone. The server sends each rendered WAV only to clients that tapped. With an engine that does not stream, such as SAPI, the phone uses the browser voice instead. Add `?sound=1` to show the button on a desktop browser, or `?sound=0` to hide it on a phone. Other devices need the dashboard PIN. See [getting started](getting-started.md).
- Keep the mindset (AGGRESSIVE / BALANCED in the pilot) visible. Switch it with one press
  of its wheel/keyboard binding. It is the control most likely to be used mid-race.
- Pair green for optimal, amber for warning, and red for act-now with a word. Never rely
  on colour alone.
- The **connection and packet-age indicator is load-bearing**. A frozen dashboard that
  looks alive is worse than a blank one. Grey everything out above 1 s of staleness.
- The second monitor on the game PC is the primary client. The same page works on a phone.
- Vendor CSS in the repo. The race PC may be offline, so nothing should need the internet
  mid-race.
