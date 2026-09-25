# The five changes of direction, in detail

These are the five places where the refined plan does something structurally different
from plan v1. Each one is written as: what changes, why, what it costs, how it is built,
how we know it worked, and how we back out if it was wrong.

Nothing here is a code change yet. The point of this document is that we agree on the
five before any of them get built, because each one is expensive to reverse later.

---

## DC-1 — Record/replay becomes the foundation, not a debugging afterthought

**v1:** live UDP only. Every threshold change is validated by driving.

**Refined:** every datagram is written to disk with its receive timestamp before it is
parsed. A replay tool feeds recordings back through the identical ingest path. Tests,
tuning, the debrief and demos all run on replays.

**Why.** The expensive resource in this project is *your driving time*, not CPU. A
tyre-temperature threshold that needs six iterations costs six races if tuned live, and
six minutes if tuned on a recording. It also makes the behaviour reproducible: "given
`silverstone-race-01`, the engine emits exactly these calls at these lap distances" is a
test; "it felt chatty last night" is not.

**Cost.** ~0.5 session of work up front, before anything speaks. ~0.85 GB per race hour at 30 Hz before compression
of disk. A discipline cost: the ingest path must never have a side door that live packets
take and replayed packets do not.

**How it is built.** See `07-replay-and-debug.md`. The load-bearing constraint is that
`ingest` takes a source abstraction (`LiveSocket | ReplayFile`) and cannot tell them
apart, and that everything time-dependent reads a `Clock` object rather than
`time.monotonic()` directly.

**Acceptance.** A recorded practice session replays at 10× and produces a packet census
identical to the live one, and a rule test suite runs in CI with no game present.

**If it is wrong.** It is not reversible, but it is also not harmful — the worst case is
that we built a tool we under-use. Unlikely: the debrief and the calibration tool both
consume the same recordings.

---

## DC-2 — Persistence replaces the memory budget

**v1:** hard cap of ~1 MB of state, five laps of history, in-memory only.

**Refined:** hot path keeps small ring buffers; every lap summary, stint, pit event and
call decision is written to SQLite, and a 10 Hz downsample of the session is kept.

**Why.** The memory cap was buying a benefit that does not exist (see DC-3) at the price
of the product's main long-term advantage. Degradation priors per track and compound,
measured pit loss, thermal windows fitted to *your* driving, and any post-session debrief
all require history to survive the session. A pit-wall assistant that knows nothing about
your last race at this track is a rulebook, not an engineer.

**Cost.** A schema and its migrations. Write I/O on the game PC (mitigated in
`09-performance.md`: WAL mode, batched at lap boundaries, off the hot thread). Some
privacy surface — the database contains your session history; it never leaves the LAN.

**How it is built.** Tables in `03-strategy.md`. Writes happen on a background thread from
a bounded queue; if the queue fills, we drop persistence rows and keep racing, and the UI
shows a degraded-persistence badge. Persistence failure must never affect a call.

**Acceptance.** Second race at a track starts with a degradation prior and a measured pit
loss from the first, and the first pit recommendation is visibly better-founded than a
cold start.

**If it is wrong.** Trivially reversible: persistence is a sink, nothing in the hot path
reads from it synchronously. Delete the file and the assistant still works, just colder.

---

## DC-3 — A measured latency budget replaces "zero GC"

**v1:** zero-allocation hot path, justified as avoiding in-game micro-stutter.

**Refined:** an explicit latency budget, instrumented and displayed —
packet in → WebSocket out p99 < 50 ms; trigger true → speech starts p99 < 300 ms for
priority 1 — plus a separate, *measured* frame-time impact budget on the game
(`09-performance.md`).

**Why.** The stated justification was wrong: the backend is a separate OS process, so its
garbage collector cannot stall the game's render loop. It can only (a) add latency to our
own calls and (b) consume CPU the game wanted. Those are two different problems with two
different measurements, and neither is "allocations per packet". Optimising against a
theory produces code that is hard to change and still slow in the way that matters.

Note this does *not* mean being careless: the cheap hygiene (`unpack_from` into
preallocated slotted objects, no per-packet dicts, `gc.freeze()` after warm-up) stays.
It is now justified by a benchmark instead of a belief, and it stops where the benchmark
stops paying.

**Cost.** Instrumentation everywhere, and the honesty cost of having numbers that can be
bad.

**How it is built.** Timestamp at `datagram_received`, carry it through the snapshot into
the emitted call, and record the delta at each stage into a histogram exposed on
`/metrics` and in the dashboard's corner. The phone reports speech-start back over the
WebSocket so the last hop is measured too, using a one-time clock offset handshake.

**Acceptance.** The budget is met on the target hardware with the game running, shown in
CI-published benchmark output from a replay and in a live session.

**If it is wrong.** If Python cannot hold the budget, the escape hatch is a small native
ingest process (Rust/Go) that parses and forwards decoded state over a local socket,
leaving the strategy engine in Python. The schema-driven layout tables (DC-4) are exactly
what makes that port cheap, which is another reason to do DC-4 first.

---

## DC-4 — Declarative packet layouts replace hand-written struct strings

**v1:** hand-written `struct` format strings per packet.

**Refined:** one layout table per (`m_packetId`, `m_packetVersion`) for format 2026,
compiled at import into `struct.Struct` objects plus field maps; dispatch on the header
triple; unknown triples counted and dropped, never parsed with a nearest match; size
mismatch rejects the packet loudly.

**Why.** We support only format 2026, but EA still revises layouts
inside a season via `m_packetVersion`. With format strings, a patch means re-deriving
offsets by hand across a dozen packets — the exact kind of work that produced the six
field-level errors in v1. With tables, it is a data edit, and a size assertion tells you
immediately which packet moved instead of letting every field downstream shift silently.

**Cost.** One indirection layer, and the tables must be transcribed carefully once.

**How it is built.** See `02-ingestion.md`. The tables also generate the enum module and
the golden-fixture tests, so there is exactly one source of truth for the spec.

**Acceptance.** A deliberately corrupted-length packet is rejected with a named error; a
new `m_packetVersion` can be added in a data-only pull request.

**If it is wrong.** Reversible at any time — the generated parsers have the same interface
as hand-written ones.

---

## DC-5 — Rules as data, and a pace model behind them

**v1:** `quali_engine.py` and `race_engine.py` with imperative thresholds.

**Refined:** two parts, which belong together.

*Rules as data:* every call is a declaration — trigger predicate, clear predicate,
priority, cooldown, max-per-stint, required-fresh packets, phrasing template, speak-time
revalidation predicate — evaluated against an immutable 10 Hz snapshot.

*A model under them:* `lap_time = base(track, compound) + fuel_coeff · fuel_kg +
deg(compound, age, wear) + traffic`, fitted online and persisted, with pit loss measured
rather than assumed.

**Why.** Thresholds answer "is the front left hot?". They cannot answer "box this lap or
next?", which is the only question a pit wall exists to answer. Undercut value, free-stop
availability and the SC cheap stop are all *projections*, not comparisons — they need a
model of how fast you will be, on what tyre, in how much traffic. Separately, keeping the
rules as data is what makes tuning a config edit with a replay test rather than a code
change with a race to validate it.

**Cost.** The largest of the five. A safe expression evaluator, a rule schema, a fitting
routine that is robust to outlier laps, and a phrasing layer. Also a failure mode to
design against: a model that is confidently wrong is worse than a threshold that is
obviously crude — so every model output carries a confidence and degrades to "not enough
data" rather than guessing.

**How it is built.** `03-strategy.md`. Rules land incrementally: M1 ships one rule and the
minimal engine; M2 ships the quali set without the model; M3 ships the model and the race
set. The model is only ever *consulted* by rules, so a bad fit can be disabled by config
without removing the rules.

**Acceptance.** Changing a threshold requires touching no Python. A pit recommendation on
a recorded race is defensible in review against what actually happened, and the model
says "insufficient data" for the first three laps of a stint rather than inventing a
degradation rate.

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

DC-1 first: it is cheap, and it makes every later acceptance criterion testable.
DC-3's instrumentation lands with the first end-to-end call so that the budget has never
been violated silently. DC-2 must exist before the model in DC-5, because the model's
priors live in it.
