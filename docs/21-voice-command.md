# Voice command: driver → pit wall by speech

Feasibility, design and impact analysis. Design only — nothing here is implemented.

The driver menu (doc 12, M3) already lets the driver ask the pit wall a preset question
with the thumb stick: open, scroll, confirm, and a deterministic handler answers from the
snapshot. That works, but it costs a hand off the wheel and a glance at the second screen,
and in a battle neither is free. Voice removes the navigation: the driver holds a button,
says "gap ahead?", and the same handler answers.

The short version:

- **Feasible, with a small footprint,** if it is *push-to-talk* with a *closed grammar* of
  roughly 40 phrases, recognised offline on the CPU in a **separate process** at
  below-normal priority. Idle cost is an audio callback; the recogniser only runs while
  the button is held and for ~300 ms after release.
- **Voice is a new input route into the existing menu, not a new answer system.** The
  reply side (cases, templates, variants, decision log, `driver_inputs` table) is reused
  untouched. No LLM anywhere (ADR 0008 applies to the ear as much as the mouth).
- **Memory is not the constraint on 32 GB.** The recommended recogniser adds an estimated
  150–300 MB RSS; the game plus browser plus backend leave well over 10 GB free. The risk
  that matters is the same one as in doc 09: a CPU burst landing on the render thread.
  PTT gating, process priority, affinity and a single decode thread bound that burst, and
  the frame-time A/B in doc 09 is how we prove it.
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

### Push-to-talk

Hold a button, speak, release. The recogniser starts on press (with a 400 ms pre-roll
from the ring buffer, so a word that started a fraction before the press is not lost)
and finalises on release. Release is the end-of-utterance signal, so there is no
silence detection to tune and no waiting for a timeout.

Why PTT and not a wake word: it is the single biggest performance and reliability lever.
Nothing runs while the button is up, so idle cost is zero; the game's engine note,
crowd, the engineer's own replies and Discord never reach the recogniser; and "pit wall,
gap ahead?" becomes just "gap ahead?".

Binding: a free UDP Action (**Action 9**, `voice.ptt_bit`, default `0x10000000`) on a
wheel button or a Stream Deck key, or a keyboard key via the dashboard (`V`). The
UDP route is preferred for the same reasons as doc 12: no hook, no anti-cheat question,
and the press is in the recording. A Stream Deck key may not report *held*, so a
Stream Deck PTT falls back to **tap-to-talk**: tap starts listening, end of speech is
detected by 600 ms of silence or a 4 s cap (`voice.tap_mode`).

Feedback so the driver knows the mic is open, without looking:

- **Press:** the existing radio blip (the two-pip open-channel sound), slightly softer.
- **Release, recognised:** the reply itself, within ~0.5 s. No extra "copy".
- **Release, not recognised:** "Say again?" once; a second miss in a row is silent (the
  dashboard shows what was heard). Never guess at a low-confidence action.
- **Dashboard:** a `LISTENING` pill beside the call banner while held; then the
  recognised text and intent for 3 s (or `?` and the raw text on a miss). Same overlay
  slot as the menu, so nothing reflows.
- **P2/P3 speech is held** while the button is down and until the reply is done. P1
  still speaks (safety first, same as radio silent).

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
| `ack` | press ACK | "copy", "understood", "got it" | acts on the open response window, same as a single press |
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
                 └── PTT edge: BUTN Action 9 (via backend ws) or key
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
- PTT edge from `BUTN` Action 9 is forwarded to the voice process as
  `{"type":"ptt","down":true|false,"t":..}`; a `V` key on the dashboard sends the same.
- Dispatcher: `hold_low_priority(until)` while listening/replying; the reply is a P1 reply
  as menu answers already are.
- Recording: the intent message is written as a synthetic record (reserved packet id,
  like the planned keyboard records), so `pitwall replay` re-injects it at the same
  session time and never runs the recogniser. Decision log: a `driver_input` record with
  `source: "voice"`, `heard`, `conf`; SQLite `driver_inputs` gains `source` and `heard`
  columns (migration).
- State frame: `voice: {available, listening, heard, intent, conf, age_s}`.

**Voice process:**

1. Opens the default (or `voice.device`) input in **WASAPI shared mode**, 16 kHz mono
   int16, ~20 ms blocks, into a 1 s ring buffer. Shared mode never changes the device's
   format or touches the game's output stream.
2. Loads the model at startup and warms it with one silent decode, so the first press in
   the race pays nothing.
3. On `ptt down`: takes the last 400 ms from the ring and streams audio into a fresh
   recogniser configured with the grammar. On `ptt up`: feeds the trailing 200 ms,
   finalises, matches, sends the intent (or a `miss`) to the backend. Hard cap 6 s of
   audio per press.
4. Idle: nothing but the audio callback. No VAD, no wake word, no timers.
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
- **While held (1–3 s per question):** streaming decode at RTF ≈ 0.1–0.3 means 10–30 % of
  one core *during the hold only*. Kaldi's BLAS thread count is pinned to 1
  (`OPENBLAS_NUM_THREADS=1`) so it cannot fan out across cores.
- **On release:** finalisation, ≈ 100–300 ms of one core. This is the only burst and it
  is small and bounded.
- **Per race:** at 10–30 questions a race that is a few core-seconds in total.

Mitigations, all default-on and all from doc 09's playbook:

1. The voice process runs at **below-normal priority** (`SetPriorityClass`); the game
   wins every scheduling contest.
2. **Affinity** to the same trailing cores as the backend (`voice.cpu_affinity`,
   inherits `cpu_affinity` when unset). On CPUs with E-cores, pin to E-cores.
3. **One decode thread.** No thread pools, no GPU/DirectML providers.
4. **Model load and warm-up at startup**, in the garage, never mid-lap.
5. **No timer-resolution changes**; PTT edges arrive as events, no polling loops.
6. `voice.enabled: false` removes the process entirely — a one-line A/B.

Where micro-stutter could still come from, and the check for each:

| Suspected cause | Why it is unlikely | How we check |
|---|---|---|
| Decode burst on the render core | below-normal priority + affinity keep it off the busy cores; ≤ 300 ms of one core | frame-time A/B with a scripted PTT burst every 20 s |
| Audio device contention | shared-mode capture is a separate WASAPI stream; the game's output is unaffected; never exclusive mode | verify no format change / no dropouts in the game audio while listening |
| Python GIL / event loop | separate process; the backend only handles a 200-byte WS message per question | tick-overrun histogram unchanged |
| Page faults on first press | model warm-up at startup; steady RSS | RSS log flat across the race |
| Antivirus scanning the model | mmap'd once at startup; add the voices/models dir to the Defender exclusion already recommended for recordings | startup time only |

### The measurement that settles it

Doc 09's procedure, extended: the same hotlap benchmark, three runs each of (a) backend
off, (b) backend on, (c) backend on + voice process idle, (d) backend on + a scripted
PTT question every 20 s. Capture with PresentMon or CapFrameX; compare 1 % and 0.1 %
lows. Acceptance: (c) and (d) inside the run-to-run noise of (b). If (d) fails, the
first knob is affinity to E-cores, the second is the SAPI fallback, the third is moving
the voice process to another machine — the architecture allows all three without code
changes to the backend.

### Latency budget (driver's release → reply starts)

| Step | Est. |
|---|---|
| `BUTN` release reaching the backend and forwarded to the voice process | 10–40 ms on loopback |
| Recogniser finalise (small model, streaming already consumed the audio) | 100–300 ms |
| Intent match + `menu.answer()` | < 5 ms |
| Piper synthesis (cached common replies: 0; else ~40 ms measured) | 0–60 ms |
| Playback start (winsound / device) | ~50 ms |
| **Total** | **~0.2–0.5 s** — feels like a person answering |

## 6. Determinism, replay and review

- The recogniser output is **not** deterministic across machines or model versions, so
  it is treated like a button press: what is recorded is the resolved intent, its slots,
  the heard text and confidence, timestamped in both clock domains (ADR 0002). Replay
  re-injects the intent; the answer is then fully deterministic (same snapshot, same
  templates, same seeded variant rotation).
- Misses are recorded too (`intent: null`), so the review timeline shows "asked, not
  understood" and the accuracy of the recogniser can be graded per race like calls are.
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
  ptt_bit: 0x10000000       # UDP Action 9; 0 disables the wheel PTT
  ptt_key: "V"              # dashboard key, mirrors the wheel button
  tap_mode: false           # tap starts, silence (tap_silence_ms) or tap_max_s ends
  tap_silence_ms: 600
  tap_max_s: 4.0
  preroll_ms: 400
  max_utterance_s: 6.0
  confidence_min: 0.7
  margin_min: 0.15
  say_again_on_miss: true   # "Say again?" once, then silent until a hit
  hold_low_priority: true   # hold P2/P3 speech while listening and replying
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

`ptt_bit` joins the existing collision check for `input.*_bit`.

## 8. Plan

Phase 0 — spike (one session, no backend changes, decides go/no-go)

1. On the game PC: install Vosk + the small English model; record 60 utterances of the
   grammar through the driving headset **while driving** (engine noise, breathing,
   clipped words), with PTT timestamps.
2. Measure: intent accuracy and confusion pairs; process RSS and CPU during idle, hold
   and finalise; finalise latency.
3. Run the four-way frame-time A/B above with the standalone voice process.
4. Repeat 1–2 with the SAPI in-proc recogniser for the fallback numbers.
   Exit: ≥ 90 % intent accuracy on the driver's voice, frame-time lows inside noise, RSS
   under 300 MB. Otherwise try Rhino or stop.

Phase 1 — questions by voice (one to two sessions)

- `pitwall voice` process, PTT via Action 9 and `V`, grammar for the existing menu
  questions and opinions, backend `intent` message → `menu.answer()`, state pill,
  recording + replay, decision log, `pitwall rules check` for `voice.yaml`.
- Tests: intent matcher (unit, incl. thresholds and slots); engine `intent` handling on a
  replay with synthetic intent records; grammar validation.

Phase 2 — actions and statements (one session)

- `ack`, `negative`, `say_again`, `boxing`, `staying_out`, `laps_left`, `position`,
  slot-driven `mindset` / `silent` / `budget` / `page`; dispatcher hold; tap-to-talk;
  `voice eval` over saved audio.

Phase 3 — optional, only if asked for

- Wake word ("pit wall") via a small keyword spotter, always-on. Costs a few percent of
  one core continuously and re-opens the false-trigger problem with game audio; only
  worth it if PTT proves to be a hand-off-the-wheel problem in practice.

## 9. Risks and open questions

- **Accent and vocabulary.** Small English models are US-trained; "tyres", "box",
  "Norris" may be weak. Grammar constraint helps a lot (the model only has to choose
  among 40 phrases), but the spike decides. Fallback: respell phrasings to lexicon words,
  or Rhino.
- **Microphone while driving.** A wheel-rig headset mic picks up breathing and the rig;
  PTT plus the short pre-roll is the main defence. If the driver uses speakers, PTT is
  mandatory (the engineer's own voice would otherwise be recognised).
- **Held UDP Action.** Doc 12 already flags that a *held* UDP Action is untested on the
  wheel. If the game reports only taps, tap-to-talk is the default. Check in the spike.
- **Two-way talk-over.** The driver asks while the engineer is mid-sentence: the P2/P3
  hold covers the reply side; on the capture side the pit wall's own audio is in the
  headphones, not the mic, so it is not heard. With speakers it would be — see above.
- **Discord / league voice.** A PTT key shared with Discord would send the question to
  the league too. Use a separate button.
- **Windows only.** Capture (WASAPI) and the SAPI fallback are Windows; Vosk is
  cross-platform, so the voice process could run on a Linux/Pi box beside a LAN backend.
- **Not in the recording:** keyboard-PTT and Stream Deck routes go through the
  dashboard, as the Space route does today; the intent record is what makes replay
  complete, the press edge itself is not needed.
- **Dependency:** `vosk` (and its model download via `pitwall voices get`-style command)
  and `sounddevice` (PortAudio) are new; both ship Windows wheels. Neither is needed when
  `voice.enabled` is false; import lazily in the voice process only.
