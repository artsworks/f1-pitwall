# Voice command: driver → pit wall by speech

Feasibility, design and impact analysis. Design only — nothing here is implemented.

The driver menu (doc 12, M3) already lets the driver ask the pit wall a preset question
with the thumb stick: open, scroll, confirm, and a deterministic handler answers from the
snapshot. That works, but it costs a hand off the wheel and a glance at the second screen,
and in a battle neither is free. Voice removes the navigation: the driver taps a button
to open the channel, says "gap ahead?", and the same handler answers.

The short version:

- **Feasible, with a small footprint,** if it is a *talk toggle* (tap to open the channel,
  tap or silence to close it) with a *closed grammar* of roughly 40 phrases, recognised
  offline on the CPU in a **separate process** at below-normal priority. Idle cost is an
  audio callback; the recogniser only runs while the channel is open (capped at a few
  seconds) and for ~300 ms after it closes.
- **The driver's request takes priority on the radio.** Opening the channel puts the
  dispatcher on hold: P2/P3 calls queue instead of speaking (P1 safety calls still
  interrupt). After the reply, each held call is dropped if it is stale, dropped if the
  reply already covered it, spoken if it is the only one left, or folded with the others
  into a single deterministic "Also: …" digest. Every outcome is in the decision log.
- **Voice is a new input route into the existing menu, not a new answer system.** The
  reply side (cases, templates, variants, decision log, `driver_inputs` table) is reused
  untouched. No LLM anywhere (ADR 0008 applies to the ear as much as the mouth).
- **Memory is not the constraint on 32 GB.** The recommended recogniser adds an estimated
  150–300 MB RSS; the game plus browser plus backend leave well over 10 GB free. The risk
  that matters is the same one as in doc 09: a CPU burst landing on the render thread.
  Channel gating, process priority, affinity and a single decode thread bound that burst,
  and the frame-time A/B in doc 09 is how we prove it.
- **Not recommended:** always-on wake word (continuous inference and false triggers from
  game audio), open-vocabulary Whisper-class models (bursty, larger, unneeded), and
  anything cloud (network mid-race, non-replayable).

## 1. Goals and non-goals

Goals

1. Every driver-menu item is reachable by a spoken phrase, hands on the wheel.
2. Reply within ~0.5 s of the driver finishing the sentence.
3. Zero measurable effect on game frame times (doc 09 criterion: 1 % / 0.1 % lows inside
   run-to-run noise in the A/B).
4. Deterministic and replayable: the *recognised intent* is recorded and replayed; a
   replay produces the same answers as the live session.
5. Degrades to nothing: if the microphone, model or process is missing, the stick menu
   works exactly as before.

Non-goals (for the first version)

- Free-form conversation or questions outside the grammar.
- Wake word / hands-free activation (kept as an optional later phase, section 9).
- Speaker identification, multi-driver (league) voice.
- Recognising the pit wall's own voice, or game radio audio.

## 2. Interaction design

### Talk toggle (open the channel, say it, channel closes)

Tap once to open the channel, speak, and the channel closes **by itself** on the first
of:

1. **the request is recognised** — the streaming partial result matches a complete
   grammar phrase and `voice.early_close_ms` (default 300 ms) of silence follows; the
   reply is the acknowledgement, so the driver never has to toggle off;
2. `voice.close_silence_ms` (default 1.0 s) of silence after speech was heard (the
   words did not match a phrase yet — finalise and try);
3. a second tap (manual close, also cancels: a tap during the reply stops it);
4. the hard cap `voice.max_open_s` (default 6 s).

In normal use the driver taps once and talks; the channel is closed by the time the
answer starts. The recogniser starts on open (with a 400 ms
pre-roll from the ring buffer, so a word that started a fraction before the tap is not
lost) and finalises on close. A tap is all it takes, so the binding works identically on
a wheel button and on a Stream Deck key, and nothing depends on the game reporting a
*held* UDP Action (doc 12 flags that as untested).

Why a toggle rather than hold-to-talk: one tap and the hand is back on the wheel, and
the open channel is an explicit state the dispatcher can act on (below). Why not a wake
word: nothing runs while the channel is closed, so idle cost is zero; the game's engine
note, crowd, the engineer's own replies and Discord never reach the recogniser; and
"pit wall, gap ahead?" becomes just "gap ahead?".

End-of-speech detection is a plain RMS energy gate in the voice process (speech = above
`voice.vad_db` for 100 ms; silence = below it for `close_silence_ms`). It is a few
multiplies per 20 ms block, and it only runs while the channel is open. The hard cap is
the safety net for a channel left open by mistake, and it bounds the recogniser's CPU
per request.

Binding: **Action 1 is repurposed** (`input.ack_bit`, `0x00100000`, the existing wheel
button — no new binding, and Actions 9–12 all stay free). With `voice.enabled: true`:

| Action 1 gesture | Today (doc 12) | With voice |
|---|---|---|
| single tap | acknowledge | **open the channel** (tap again = close) |
| double tap | negative | negative (unchanged) |
| long press | radio silent | radio silent (unchanged) |
| tap while the stick menu is open | confirm | confirm (unchanged; the menu owns the button) |

Acknowledge moves to the voice grammar ("copy", "understood", "got it") with one
fallback that keeps the old muscle memory: a tap that **closes on silence with nothing
heard while a call's response window is open** is recorded as `ack` — the driver tapped
and said nothing, exactly as before, and it lands ~1 s later than today. A tap with a
recognised request is *not* an acknowledge; the request is answered and the response
window stays open. With `voice.enabled: false` (or the voice process down) Action 1
reverts to doc 12 behaviour, so the wheel never has a dead button. The dashboard key `V`
mirrors the tap. The UDP route is preferred for the same reasons as doc 12: no hook, no
anti-cheat question, and the tap is in the recording. No new `*_bit` and no new collision
case; `PressDetector` already separates single / double / long for Action 1.

Feedback so the driver knows the channel is open, without looking:

- **Open:** the existing radio blip (the two-pip open-channel sound), slightly softer.
- **Close, recognised:** the reply itself, within ~0.5 s. No extra "copy".
- **Close, not recognised:** "Say again?" once; a second miss in a row is silent (the
  dashboard shows what was heard). Never guess at a low-confidence action.
- **Channel still open with nothing understood** (mis-tap, or the driver changed their
  mind): no speech. The dashboard pill turns amber `CHANNEL OPEN` with a countdown to
  the hard cap; at the cap the channel closes with a single soft closing pip. A forgotten
  toggle is therefore visible on the second screen but never talked about on the radio,
  and it can only happen when no request was recognised.
- **Dashboard:** a `LISTENING` pill beside the call banner while open (amber with a
  countdown once `voice.open_warn_s`, default 3 s, has passed without a recognised
  request); then the recognised text and intent for 3 s (or `?` and the raw text on a
  miss). Same overlay slot as the menu, so nothing reflows.

### The driver's request takes priority: the dispatcher hold

When the channel opens the dispatcher enters **hold** and stays there until the reply
has finished speaking (or the channel closed with nothing to answer). While on hold:

- Speech in progress at P2/P3 is cut at the end of the current sentence (Piper renders
  one call as one WAV; it is stopped, and the call is re-queued as held).
- New P2/P3 candidates are accepted by the rules and dispatcher as usual (cooldowns,
  dedupe and budget still book them) but go into the **held list** with their
  submission time, priority, `still_true` predicate and topic, instead of being spoken.
- Per-priority deadlines (P2 3 s, P3 1.5 s) **pause** during the hold; otherwise every
  held P3 would expire before the driver finished the sentence. `still_true` remains the
  real gate.
- **P1 still speaks and preempts**, including the reply. Safety car, puncture, forced
  box, imminent penalty do not wait for a gap question. This is the one exception to
  "driver first", and it is the same exception radio silent makes.

When the hold ends, the dispatcher runs **release** once, in priority then age order,
and each held call gets exactly one of four outcomes:

| Outcome | Rule | Example |
|---|---|---|
| **drop, stale** | `still_true` now false, or the call's paused deadline has less than 0 left after re-adding the hold duration, or a `clear_when` fired during the hold | "Norris closing, 0.8" held while the driver asked about fuel; Norris has since passed — dropped |
| **drop, covered** | the reply's item has a topic that matches the held call's `topic` (both are YAML tags: `gap_ahead`, `gap_behind`, `fuel`, `tyres`, `pit_plan`, `energy`, `weather`, `position`) | driver asked "gap ahead?", the held call was a gap-ahead update — the answer already said it |
| **speak** | one call remains (or a P2 recommendation remains — recommendations are never digested) | "Box this lap, box box." spoken right after the reply |
| **digest** | two or more P3 (and non-recommendation P2) calls remain | "Also: Piastri 0.9 behind closing. Front-left temps high." |

The digest is deterministic and comes from YAML like everything else (ADR 0008): each
rule may carry a `brief:` template (a 3–6 word form of the call; `say` variants are the
long form). Release joins up to `voice.digest_max_items` (default 3) briefs, highest
priority first, under `voice.digest_prefix` ("Also: "); anything beyond the cap is
dropped as `digest_overflow`. A rule without `brief` cannot be digested — if it survives
to release it is spoken in full only if it is the sole survivor, else dropped. The
digest counts as **one** call against the lap budget and against the minimum gap, and it
is a P2 so it is not itself workload-gated to a straight.

Every held call's outcome is a decision-log record (`held` → `spoken_after_hold` |
`dropped_stale` | `dropped_covered` | `digested` | `digest_overflow`, with the hold
duration and the driver intent that caused it) and a row in `calls` with that outcome,
so review mode can show what the driver's question displaced and the learning loop
(doc 20) can grade whether the digest was worth saying. In replay the hold is driven by
the recorded channel-open/close and intent records, so release makes the same choices.

The same hold and release also apply to the stick menu (`menu.hold_radio`, default on):
an open menu is a driver request too. Today the menu does not hold P2/P3, which is why a
gap update can talk over a menu prompt.

### What can be said

The grammar is a YAML file, one intent per entry, each with a list of phrasings.
Intents map onto the menu: most are existing `menu.items` ids, so the answer path is
unchanged. A few are new because they are natural to say and awkward to scroll to.

| Intent | Maps to | Phrasings (examples; the file has 3–6 each) | Kind |
|---|---|---|---|
| `tyres` | menu `tyres` | "tyres gone", "how are the tyres", "tyre check" | question |
| `pit` | menu `pit` | "pit now", "should I box", "box this lap?" | question |
| `gap` | menu `gap` | "gap ahead", "gap to the car ahead", "where's the car ahead" | question |
| `gap_behind` | menu `gap_behind` | "gap behind", "who's behind", "car behind" | question |
| `fuel` | menu `fuel` | "fuel", "fuel check", "how's the fuel" | question |
| `plan` | menu `plan` | "what's the plan", "plan", "strategy" | question |
| `push` | menu `push` | "push or save", "can I push", "do I save" | question |
| `rain` | menu `rain` | "rain coming", "weather", "is it going to rain" | question |
| `race_stat` | menu `race_stat` | "race stat", "status", "where are we" | question |
| `fight` | menu `fight` | "fight", "who am I racing", "battle" | question |
| `laps_left` | new question | "laps left", "how many laps", "how long to go" | question |
| `position` | new question | "position", "what position", "where am I" | question |
| `understeer` | menu `understeer` | "understeer", "I've got understeer", "front's washing out" | opinion |
| `oversteer` | menu `oversteer` | "oversteer", "rear's loose", "I've got oversteer" | opinion |
| `boxing` | new statement | "boxing this lap", "I'm coming in", "box box" | statement → confirms pit plan for this lap; plan handler treats it like an accepted box recommendation |
| `staying_out` | new statement | "staying out", "I'll stay out", "not stopping" | statement → rejects the current box recommendation |
| `ack` | press ACK | "copy", "understood", "got it" | acts on the open response window; also the outcome of a silent tap while a window is open (see binding) |
| `negative` | press NEG | "negative", "no", "not now" | same as a double press |
| `say_again` | say again | "say again", "repeat", "what was that" | re-speaks the last call inside `say_again_window_s` |
| `mindset` | menu `mindset` | "aggressive", "go aggressive", "balanced", "calm it down" | action; the word chooses the mindset rather than toggling |
| `silent` | menu `silent` | "radio silent", "leave me alone", "radio on", "talk to me" | action; on/off chosen by the phrase |
| `budget` | menu `budget` | "less radio", "more radio" | action; steps down/up instead of cycling |
| `page` | menu `page` | "next page", "battle page", "car page", "track page" | action; a named page jumps straight to it |

Rules for the grammar:

- Phrasings are short (2–5 words), start with a distinctive word, and avoid pairs that
  differ by one unstressed word. "Gap ahead" / "gap behind" are fine (stressed content
  word differs); "push" / "plush" is not a concern because the vocabulary is closed.
- Questions and opinions execute on a single confident hit. **Actions** with a lasting
  effect (`silent` on, `mindset`, `budget`) also execute on a confident hit but are
  confirmed by voice as today; a *low-confidence* action is not executed — the pit wall
  says "Say again?".
- `voice.confidence_min` (default 0.7) and `voice.margin_min` (default 0.15, gap to the
  second-best intent) are YAML. Below either: miss.
- `pitwall rules check` validates the file: every intent maps to a known menu item or
  built-in, no duplicate phrasings, every phrasing lower-case ASCII words in the model's
  lexicon (unknown words are reported so they can be respelled, e.g. "tyres" → the
  model may only know "tires").

## 3. Architecture

```
game ──UDP──▶ backend (ingest → state → rules → dispatcher → speaker)
                 ▲                        │
                 │ ws {"type":"intent"}   │ ws state {voice: {listening, heard, intent}}
                 │                        ▼
             voice process  ◀── mic (WASAPI shared, 16 kHz mono) ── ring buffer
             (below-normal priority, pinned, 1 decode thread)
                 ▲
                 └── channel tap: BUTN Action 1 single tap (via backend ws) or key
```

**A separate process, not a thread.** Three reasons, all from doc 09: the recogniser's
memory and any native-library thread pool are isolated from the ingest hot path; it can
be given its own priority and affinity; and it can crash, hang or be disabled without
the race engineer noticing. It is a second console-less process started by `pitwall
start` when `voice.enabled` is true (`pitwall voice` runs it standalone), and it talks to
the backend over the dashboard WebSocket exactly like the keyboard route does today. That
also means it can run on a second machine with the microphone, if the backend ever
moves off the game PC.

**Backend side (small):**

- A `voice` section in settings; `voice.yaml` for the grammar (defaults shipped,
  overridable per profile like `menu.yaml`).
- `Engine` handles a new WS message `{"type":"intent","intent":..,"slots":{..},"text":..,
  "conf":..,"t_press":..,"t_release":..}`: it maps the intent to a menu item and calls
  `pitwall.input.menu.answer()`, or to the press/say-again/page paths. New question
  handlers (`laps_left`, `position`) and the two statements go into `menu.ANSWERS` and
  `menu.yaml` so they are also available on the stick.
- A single tap on `BUTN` Action 1 toggles the channel; the backend owns the channel state and
  tells the voice process `{"type":"channel","open":true|false,"t":..}`; a `V` key on
  the dashboard toggles the same. The voice process reports a silence/cap close back as
  `{"type":"channel","open":false,"reason":"silence"|"cap"}` so the backend state
  matches.
- Dispatcher: `hold()` on channel open, `release(reply_topic)` when the reply has been
  spoken (or the channel closed with a miss), with the four outcomes above. The reply is
  a P1 reply as menu answers already are.
- Recording: channel open/close and the intent message are written as synthetic records
  (reserved packet id, like the planned keyboard records), so `pitwall replay` re-injects
  them at the same session time and never runs the recogniser. Decision log: a
  `driver_input` record with `source: "voice"`, `heard`, `conf`, plus the held-call
  outcome records; SQLite `driver_inputs` gains `source` and `heard` columns and `calls`
  gains the hold outcomes (migration).
- Rules YAML: optional `brief:` and `topic:` per rule; menu items gain `topic:` so
  "covered" can be decided. `pitwall rules check` warns about P3 rules without `brief`.
- State frame: `voice: {available, channel_open, heard, intent, conf, age_s, held: n}`.

**Voice process:**

1. Opens the default (or `voice.device`) input in **WASAPI shared mode**, 16 kHz mono
   int16, ~20 ms blocks, into a 1 s ring buffer. Shared mode never changes the device's
   format or touches the game's output stream.
2. Loads the model at startup and warms it with one silent decode, so the first press in
   the race pays nothing.
3. On `channel open`: takes the last 400 ms from the ring and streams audio into a fresh
   recogniser configured with the grammar; the energy gate watches for speech then
   silence. On close (second tap, silence after speech, or `max_open_s`): feeds the
   trailing 200 ms, finalises, matches, sends the intent (or a `miss`) and the close
   reason to the backend.
4. Idle: nothing but the audio callback. No energy gate, no wake word, no timers.
5. Optional `voice.save_audio: true` writes each utterance as a WAV next to the recording,
   for offline accuracy measurement (never committed, ADR 0005 applies).

Matching: the recogniser is constrained to the grammar's vocabulary, so it returns a
string made of grammar words (or `[unk]`). The matcher scores each intent's phrasings by
token-level edit similarity, takes the best, applies the confidence and margin
thresholds, and extracts slots (`aggressive|balanced`, page names, `on|off`).

## 4. Recogniser choice

Requirements: offline, CPU only (the GPU is the game's), Windows, Python-callable, small
memory, streaming or fast-final, and — most important — able to be **constrained to a
phrase list**, because a closed grammar of 40 phrases is both more accurate and far
cheaper than open transcription.

| Option | Grammar-constrained | Est. RSS | CPU while decoding | Idle CPU | Notes |
|---|---|---|---|---|---|
| **Vosk** (Kaldi, `vosk-model-small-en-us-0.15`, ~40 MB on disk) | yes — `KaldiRecognizer(model, rate, grammar_json)` | ~150–300 MB | streaming, RTF ≈ 0.1–0.3 on one core | 0 | pip wheel bundles the runtime; MIT/Apache; British-accent handling to be verified in the spike |
| Windows in-proc SAPI 5.4 recogniser (`SpInProcRecognizer`, SRGS grammar) via pywin32 | yes | ~50–100 MB (est.) | low | 0 | no new dependency (pywin32 is already there for SAPI speech); needs the language pack; accuracy with a headset mic and engine noise unknown; Windows-only; COM threading care |
| sherpa-onnx (streaming zipformer / keyword spotter) | keyword spotting yes; grammar via hotwords, not hard constraint | ~200–400 MB | RTF ≈ 0.1–0.3 | 0 | ONNX Runtime dependency; more moving parts than Vosk for the same job |
| Picovoice Rhino (speech-to-intent) + Porcupine | yes — purpose-built intent grammar | tens of MB | very low | very low | closest fit technically; requires an account and access key, licence terms to check; grammar compiled on their console, which cuts against "everything in YAML" |
| faster-whisper `tiny.en` / `base.en` int8 | no (prompting only) | ~300–600 MB | non-streaming burst: ~0.3–1 s per 3 s utterance on 2–4 threads | 0 | best open-vocabulary accuracy, worst fit: bursty CPU, bigger, and we do not need open vocabulary |
| Cloud (Azure / Google / Whisper API) | partly | — | — | — | rejected: network mid-race, ~1 s+ latency, cost, not replayable |

**Recommendation: Vosk small English, grammar-constrained, with the Windows in-proc SAPI
recogniser as the zero-dependency fallback** (same shape as Piper primary / SAPI fallback
on the speaking side). Rhino stays on the list if the spike shows Vosk's accuracy on the
driver's accent through a headset is not good enough — its intent model is likely the
most robust, at the cost of a licence and an external grammar tool.

The RSS and RTF figures are estimates from the projects' own published small-model
numbers and general experience, not measurements on the game PC; the spike (section 8)
replaces them with measured values before anything is built on them.

## 5. Performance impact (same PC as the game, Windows, 32 GB)

Doc 09 ranks the contention channels: CPU core contention, GPU, disk, memory, timer
resolution. Voice command touches the first, fourth and — new — the audio stack.

### Memory

| Component | Today (approx., doc 09 / measured earlier) | With voice |
|---|---|---|
| F1 26 | 8–12 GB working set (title-dependent; measure) | unchanged |
| Chrome dashboard (two pages) | 300–600 MB | unchanged |
| Backend with Piper | ~200 MB | unchanged (voice is a separate process) |
| **Voice process (Vosk small)** | — | **~150–300 MB** est., steady; no growth (fixed ring buffer, recogniser recreated per press and freed) |
| Voice process (SAPI fallback) | — | ~50–100 MB est. |

Total added is roughly 1 % of 32 GB; free memory stays above 10 GB with a wide margin.
Memory is not the risk. Two things do matter: the model is **memory-mapped from disk
once at startup** (a one-off ~40 MB read, done before the session, not on first press),
and the process must not swap — keep the working set steady (no per-press allocations
beyond the recogniser object) and confirm with a perf-counter log over a full race.

### CPU

- **Idle:** the WASAPI capture callback delivers 20 ms blocks into a ring buffer. This
  is well under 0.5 % of one core and has no burst; it is the same work Discord does
  while you are muted.
- **While the channel is open (1–3 s per question, 6 s hard cap):** streaming decode at
  RTF ≈ 0.1–0.3 means 10–30 % of one core *while open only*, plus the energy gate, which
  is negligible. Kaldi's BLAS thread count is pinned to 1 (`OPENBLAS_NUM_THREADS=1`) so
  it cannot fan out across cores. The cap bounds the worst case for a channel left open.
- **On close:** finalisation, ≈ 100–300 ms of one core. This is the only burst and it
  is small and bounded.
- **Per race:** at 10–30 questions a race that is a few core-seconds in total.

Mitigations, all default-on and all from doc 09's playbook:

1. The voice process runs at **below-normal priority** (`SetPriorityClass`); the game
   wins every scheduling contest. The hold/release logic itself is a few list
   operations inside the existing dispatcher drain — no measurable cost.
2. **Affinity** to the same trailing cores as the backend (`voice.cpu_affinity`,
   inherits `cpu_affinity` when unset). On CPUs with E-cores, pin to E-cores.
3. **One decode thread.** No thread pools, no GPU/DirectML providers.
4. **Model load and warm-up at startup**, in the garage, never mid-lap.
5. **No timer-resolution changes**; channel taps arrive as events, no polling loops.
6. `voice.enabled: false` removes the process entirely — a one-line A/B.

Where micro-stutter could still come from, and the check for each:

| Suspected cause | Why it is unlikely | How we check |
|---|---|---|
| Decode burst on the render core | below-normal priority + affinity keep it off the busy cores; ≤ 300 ms of one core | frame-time A/B with a scripted channel open/question every 20 s |
| Channel left open (6 s of decoding) | hard cap; silence close | scripted worst case: open with no speech, repeated |
| Audio device contention | shared-mode capture is a separate WASAPI stream; the game's output is unaffected; never exclusive mode | verify no format change / no dropouts in the game audio while listening |
| Python GIL / event loop | separate process; the backend only handles a 200-byte WS message per question | tick-overrun histogram unchanged |
| Page faults on first press | model warm-up at startup; steady RSS | RSS log flat across the race |
| Antivirus scanning the model | mmap'd once at startup; add the voices/models dir to the Defender exclusion already recommended for recordings | startup time only |

### The measurement that settles it

Doc 09's procedure, extended: the same hotlap benchmark, three runs each of (a) backend
off, (b) backend on, (c) backend on + voice process idle, (d) backend on + a scripted
spoken question every 20 s. Capture with PresentMon or CapFrameX; compare 1 % and 0.1 %
lows, where (d) alternates real questions with worst-case opens that hit the 6 s cap.
Acceptance: (c) and (d) inside the run-to-run noise of (b). If (d) fails, the
first knob is affinity to E-cores, the second is the SAPI fallback, the third is moving
the voice process to another machine — the architecture allows all three without code
changes to the backend.

### Latency budget (channel close → reply starts)

| Step | Est. |
|---|---|
| Silence close: `close_silence_ms` after the last word (a second tap skips this) | 0–1000 ms |
| Close reaching the recogniser (tap: `BUTN` → backend → voice process on loopback) | 10–40 ms |
| Recogniser finalise (small model, streaming already consumed the audio) | 100–300 ms |
| Intent match + `menu.answer()` | < 5 ms |
| Piper synthesis (cached common replies: 0; else ~40 ms measured) | 0–60 ms |
| Playback start (winsound / device) | ~50 ms |
| **Total** | **~0.2–0.5 s after a closing tap; ~1.2–1.5 s after the last word on silence close** — the pause a real engineer takes before answering |

## 6. Determinism, replay and review

- The recogniser output is **not** deterministic across machines or model versions, so
  it is treated like a button press: what is recorded is the resolved intent, its slots,
  the heard text and confidence, timestamped in both clock domains (ADR 0002). Replay
  re-injects the intent; the answer is then fully deterministic (same snapshot, same
  templates, same seeded variant rotation).
- Misses are recorded too (`intent: null`), so the review timeline shows "asked, not
  understood" and the accuracy of the recogniser can be graded per race like calls are.
- Held-call outcomes are recorded per call, so the review can answer "what did the
  driver's question cost?" (how much was dropped, how much digested) and the grader can
  mark a digest or a stale-drop as right or wrong.
- Optional utterance WAVs (`voice.save_audio`) plus the recorded heard text give an
  offline test set for tuning the grammar; `pitwall voice eval <dir>` re-runs the
  recogniser over saved WAVs and reports intent accuracy and confusion pairs.
- The learning loop (doc 20) can treat a voice question as a signal: a driver who keeps
  asking "gap ahead?" is not being told the gap often enough.

## 7. Configuration (`settings.yaml`, `voice.yaml`)

```yaml
voice:
  enabled: false            # off by default until the spike passes the frame-time A/B
  engine: auto              # auto = vosk if its model is present, else sapi (Windows), else off
  model_dir: models/vosk-small-en-us
  device: null              # input device name filter; null = default
  # channel tap = Action 1 single tap (input.ack_bit); double / long press keep doc 12 meaning
  silent_tap_is_ack: true   # empty tap while a response window is open counts as acknowledge
  channel_key: "V"          # dashboard key, mirrors the wheel button
  early_close_ms: 300       # close as soon as a full phrase is recognised and this much silence follows
  close_silence_ms: 1000    # close after this much silence once speech was heard
  max_open_s: 6.0           # hard cap on an open channel
  open_warn_s: 3.0          # dashboard pill turns amber with a countdown after this
  vad_db: -35               # energy gate for speech / silence
  preroll_ms: 400
  confidence_min: 0.7
  margin_min: 0.15
  say_again_on_miss: true   # "Say again?" once, then silent until a hit
  hold: true                # driver first: hold P2/P3 while the channel is open and replying
  digest_max_items: 3       # held calls folded into one "Also: …" after the reply
  digest_prefix: "Also: "
  digest_priority: 2
  cpu_affinity: null        # inherits the backend setting
  save_audio: false
  grammar: voice.yaml
```

`voice.yaml` (excerpt):

```yaml
intents:
  - id: gap                 # menu item id, or a built-in: ack, negative, say_again, laps_left, position, boxing, staying_out
    say: ["gap ahead", "gap to the car ahead", "where's the car ahead", "how far ahead"]
  - id: mindset
    say: ["go {mindset}", "{mindset}", "mindset {mindset}"]
    slots:
      mindset: {aggressive: ["aggressive", "attack"], balanced: ["balanced", "calm it down", "settle"]}
  - id: page
    say: ["next page", "{page} page"]
    slots:
      page: {battle: ["battle", "fight"], car: ["car"], track: ["track"], race: ["race"], setup: ["setup"]}
```

## 8. Plan

Phase 0 — spike (one session, no backend changes, decides go/no-go)

1. On the game PC: install Vosk + the small English model; record 60 utterances of the
   grammar through the driving headset **while driving** (engine noise, breathing,
   clipped words), with tap timestamps; also record natural mid-sentence pause lengths
   to set `close_silence_ms` and `early_close_ms`.
2. Measure: intent accuracy and confusion pairs; process RSS and CPU during idle, hold
   and finalise; finalise latency.
3. Run the four-way frame-time A/B above with the standalone voice process.
4. Repeat 1–2 with the SAPI in-proc recogniser for the fallback numbers.
   Exit: ≥ 90 % intent accuracy on the driver's voice, frame-time lows inside noise, RSS
   under 300 MB. Otherwise try Rhino or stop.

Phase 1 — questions by voice (one to two sessions)

- `pitwall voice` process, channel toggle via Action 1 single tap and `V` with recognised /
  silence / cap auto-close, grammar for the existing menu questions and opinions,
  backend `intent` message → `menu.answer()`, dispatcher hold/release with the four
  outcomes and `brief`/`topic` on the race rules, state pill with the open-channel
  countdown, recording + replay, decision log, `pitwall rules check` for `voice.yaml`.
- Tests: intent matcher (unit, incl. thresholds and slots); channel state machine
  (early close on a recognised phrase, silence close, tap close, cap close, tap during
  the reply cancels it; silent tap in a response window → `ack`, silent tap outside one →
  nothing; double / long press unaffected; `voice.enabled: false` → doc 12 `ack`);
  dispatcher hold/release (unit: stale drop, covered drop, single
  survivor spoken, digest of 2–3, overflow, P1 preempting during hold, deadline pause,
  budget counting the digest as one); engine `intent` handling on a replay with
  synthetic channel and intent records; grammar validation; menu hold parity.

Phase 2 — actions and statements (one session)

- `ack`, `negative`, `say_again`, `boxing`, `staying_out`, `laps_left`, `position`,
  slot-driven `mindset` / `silent` / `budget` / `page`; `voice eval` over saved audio.

Phase 3 — optional, only if asked for

- Wake word ("pit wall") via a small keyword spotter, always-on. Costs a few percent of
  one core continuously and re-opens the false-trigger problem with game audio; only
  worth it if the tap proves to be a hand-off-the-wheel problem in practice.

## 9. Risks and open questions

- **Acknowledge is ~1 s slower on the button.** A silent tap now waits for
  `close_silence_ms` before it counts as `ack`; saying "copy" is faster than that. If the
  response window is shorter than the silence timeout for some call, the window must be
  extended by the channel-open time (the tap opened before it closed), otherwise the
  fallback can miss — spec: response windows pause while the channel is open.
- **Accent and vocabulary.** Small English models are US-trained; "tyres", "box",
  "Norris" may be weak. Grammar constraint helps a lot (the model only has to choose
  among 40 phrases), but the spike decides. Fallback: respell phrasings to lexicon words,
  or Rhino.
- **Microphone while driving.** A wheel-rig headset mic picks up breathing and the rig;
  the closed channel plus the short pre-roll is the main defence. If the driver uses
  speakers, the engineer's reply must never overlap an open channel — which the
  auto-close on recognition guarantees (the channel is closed before the reply starts).
- **Silence close mis-fires.** A pause mid-sentence ("gap… ahead?") longer than 1 s
  closes the channel early and the recogniser sees half a phrase, usually a miss. The
  driver can re-tap; `close_silence_ms` is tunable; the spike measures natural pause
  lengths while driving.
- **Early close on a false partial.** The streaming partial could match a short phrase
  ("gap") before the driver finishes ("gap behind"). Early close therefore requires the
  partial to match a *complete* phrase that is not a prefix of another, plus 300 ms of
  silence; prefix phrases wait for the normal silence close.
- **Two-way talk-over.** The driver asks while the engineer is mid-sentence: the hold
  stops the engineer at the end of the sentence and re-queues it; on the capture side
  the pit wall's own audio is in the headphones, not the mic, so it is not heard. With
  speakers it would be — see above.
- **Digest quality.** A three-item "Also:" can sound like a list. The cap is 3, briefs
  are 3–6 words, recommendations are never digested, and the grader will tell us if the
  digest is noise; if so, lower `digest_max_items` to 1.
- **Hold duration.** A question plus reply plus digest can hold P2/P3 for 5–8 s. A P2
  recommendation (box this lap) is spoken first at release, and P1 never waits, so the
  cost is only ever informational calls arriving late — which stale-drop then handles.
- **Discord / league voice.** A talk key shared with Discord would send the question to
  the league too. Use a separate button.
- **Windows only.** Capture (WASAPI) and the SAPI fallback are Windows; Vosk is
  cross-platform, so the voice process could run on a Linux/Pi box beside a LAN backend.
- **Not in the recording:** keyboard and Stream Deck routes go through the
  dashboard, as the Space route does today; the intent record is what makes replay
  complete, the press edge itself is not needed.
- **Dependency:** `vosk` (and its model download via a `pitwall voice get`-style command)
  and `sounddevice` (PortAudio) are new; both ship Windows wheels. Neither is needed when
  `voice.enabled` is false; import lazily in the voice process only.
