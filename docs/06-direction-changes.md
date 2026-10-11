# The five changes of direction, in detail

The refined plan differs from plan v1 in five structural ways. Each section covers the
change, its reason and cost, how to build it, how to verify it, and how to reverse it.

This document proposes no code changes. Agree on all five changes before implementation
because each would be costly to reverse later.

---

## DC-1: Record/replay becomes the foundation, not a debugging afterthought

**v1:** live UDP only. Every threshold change is validated by driving.

**Refined:** every datagram is written to disk with its receive timestamp before it is
parsed. A replay tool feeds recordings back through the identical ingest path. Tests,
tuning, the debrief and demos all run on replays.

**Why.** The expensive resource is *your driving time*, not CPU. Tuning a tyre-temperature
threshold takes six races if done live, or six minutes on a recording. Replay also makes
behaviour reproducible: "given
`silverstone-race-01`, the engine emits exactly these calls at these lap distances" is a
test; "it felt chatty last night" is not.

**Cost.** ~0.5 session of work before anything speaks. Recordings use ~0.85 GB per race
hour at 30 Hz before compression. The ingest path must not process live and replayed
packets differently.

**How it is built.** See `07-replay-and-debug.md`. The load-bearing constraint is that
`ingest` takes a source abstraction (`LiveSocket | ReplayFile`) and cannot tell them
apart, and that everything time-dependent reads a `Clock` object rather than
`time.monotonic()` directly.

**Acceptance.** A recorded practice session replays at 10× and produces the same packet
census as the live session. A rule test suite runs in CI without the game.

**If it is wrong.** The change is not reversible, but it is not harmful. At worst, we
under-use the tool. The debrief and calibration tool both use the recordings.

---

## DC-2: Persistence replaces the memory budget

**v1:** hard cap of ~1 MB of state, five laps of history, in-memory only.

**Refined:** hot path keeps small ring buffers; every lap summary, stint, pit event and
call decision is written to SQLite, and a 10 Hz downsample of the session is kept.

**Why.** The memory cap offered no benefit (see DC-3) and removed the product's main
long-term advantage. Degradation priors per track and compound, measured pit loss,
thermal windows fitted to *your* driving, and post-session debriefs all need session
history. Without data from your last race at a track, the assistant is a rulebook, not
an engineer.

**Cost.** A schema and migrations. Write I/O on the game PC, mitigated with WAL mode,
lap-boundary batching, and a background thread (`09-performance.md`). The database
contains session history, but it never leaves the LAN.

**How it is built.** Tables in `03-strategy.md`. Writes happen on a background thread from
a bounded queue; if the queue fills, we drop persistence rows and keep racing, and the UI
shows a degraded-persistence badge. Persistence failure must never affect a call.

**Acceptance.** The second race at a track starts with a degradation prior and measured
pit loss from the first race. Its first pit recommendation is better-founded than a
cold start.

**If it is wrong.** Persistence is easy to reverse. Nothing in the hot path reads from
it synchronously. Delete the file and the assistant still works, with no prior data.

---

## DC-3: A measured latency budget replaces "zero GC"

**v1:** zero-allocation hot path, justified as avoiding in-game micro-stutter.

**Refined:** instrument and display a latency budget: packet in → WebSocket out p99 <
50 ms; trigger true → speech starts p99 < 300 ms for priority 1. Add a separate,
*measured* frame-time impact budget on the game (`09-performance.md`).

**Why.** The justification was wrong. The backend is a separate OS process, so its
garbage collector cannot stall the game's render loop. It can add latency to our calls
and consume CPU the game needs. These require separate measurements. Neither is measured
by "allocations per packet". Optimising against a theory produces hard-to-change code
that remains slow where it matters.

This does *not* mean being careless. Keep the cheap hygiene (`unpack_from` into
preallocated slotted objects, no per-packet dicts, `gc.freeze()` after warm-up).
Justify it with a benchmark, and stop when the benchmark shows no benefit.

**Cost.** Instrumentation throughout the system and numbers that may look bad.

**How it is built.** Timestamp at `datagram_received` and carry the value through the
snapshot into the emitted call. Record each stage's delta in a histogram exposed on
`/metrics` and in the dashboard corner. The phone reports speech-start over the WebSocket
so the last hop is measured too. Use a one-time clock-offset handshake.

**Acceptance.** Meet the budget on target hardware with the game running. Show benchmark
results from replay in CI and from a live session.

**If it is wrong.** If Python cannot meet the budget, use a small native ingest process
(Rust/Go) to parse and forward decoded state over a local socket. Keep the strategy
engine in Python. Schema-driven layout tables (DC-4) make the port cheap, another reason
to build DC-4 first.

---

## DC-4: Declarative packet layouts replace hand-written struct strings

**v1:** hand-written `struct` format strings per packet.

**Refined:** define one layout table per (`m_packetId`, `m_packetVersion`) for format
2026 and compile it at import into `struct.Struct` objects and field maps. Dispatch on
the header triple. Count and drop unknown triples instead of using a nearest match.
Reject size mismatches with an error.

**Why.** We support format 2026 only, but EA still revises layouts within a season through
`m_packetVersion`. Format strings require manually re-deriving offsets across a dozen
packets after a patch. This caused the six field-level errors in v1. Tables make each
change a data edit. A size assertion identifies the changed packet before fields shift
silently downstream.

**Cost.** One indirection layer and careful transcription of the tables.

**How it is built.** See `02-ingestion.md`. The tables also generate the enum module and
the golden-fixture tests, so there is exactly one source of truth for the spec.

**Acceptance.** Reject a deliberately corrupted-length packet with a named error. Add a
new `m_packetVersion` in a data-only pull request.

**If it is wrong.** This change is reversible at any time. Generated parsers have the
same interface as hand-written parsers.

---

## DC-5: Rules as data, and a pace model behind them

**v1:** `quali_engine.py` and `race_engine.py` with imperative thresholds.

**Refined:** two parts, which belong together.

Every call declares a trigger predicate, a clear predicate, priority, cooldown,
max-per-stint, required-fresh packets, phrasing template, and speak-time revalidation
predicate. The dispatcher evaluates the predicates against an immutable 10 Hz snapshot.

*A model under them:* `lap_time = base(track, compound) + fuel_coeff · fuel_kg +
deg(compound, age, wear) + traffic`, fitted online and persisted, with pit loss measured
rather than assumed.

**Why.** Thresholds answer "is the front left hot?" but not "box this lap or next?".
Undercut value, free-stop availability, and the SC cheap stop are *projections*, not
comparisons. They need a model of your pace, tyre, and traffic. Keeping rules as data
also makes tuning a config edit with a replay test instead of a code change that needs a
race for validation.

**Cost.** This is the largest change. It needs a safe expression evaluator, a rule schema,
a fitting routine that tolerates outlier laps, and a phrasing layer. A confidently wrong
model is worse than an obviously crude threshold. Give each model output a confidence and
return "not enough data" instead of guessing.

**How it is built.** See `03-strategy.md`. Ship one rule and the minimal engine in M1.
Ship the quali set without the model in M2. Ship the model and race set in M3. Rules only
*consult* the model, so config can disable a bad fit without removing the rules.

**Acceptance.** Change thresholds without touching Python. A pit recommendation on a
recorded race is defensible against what happened. For the first three laps of a stint,
the model says "insufficient data" instead of inventing a degradation rate.

**If it is wrong.** The rule engine stays regardless; if the model proves unreliable, the
strategy rules fall back to threshold predicates over the same snapshot, which is v1's
behaviour, reached by config.

---

## Sequencing and dependencies

```
DC-1 record/replay ──▶ DC-4 layout tables ──▶ DC-5 rules ──▶ DC-5 model
       │                      │                   │
       └──────────────▶ DC-3 latency budget       │
                              │                   │
                       DC-2 persistence ──────────┘
```

Build DC-1 first. It is cheap and makes later acceptance criteria testable.
Add DC-3 instrumentation with the first end-to-end call so budget violations are visible.
Build DC-2 before the DC-5 model because its priors live in persistence.
