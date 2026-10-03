# ADR 0010: Jev call arbitration

- **Status:** proposed (2026-10). Adds `radio.arbitrate` as a decision type under
  [ADR 0009](0009-llm-chooser-with-rule-veto.md). Phrasing stays as decided in
  [ADR 0008](0008-natural-phrasing-without-an-llm.md).
- **Context:** the dispatcher speaks queued P2/P3 calls in `(priority, t)` order. When two
  or three calls queue on the same tick, that order is whichever rule fired first.

## Context

The dispatcher already decides which calls exist and whether each may be spoken
(`_suppression_reason`: priority gate, quiet modes, cooldowns, budget, dedup). What it
does not decide well is order. Two P2 calls that queue together go out in submit order,
so a pit-window reminder can wait behind a gap update that could have waited a lap.

Jev (TypeSafe, served through the Vercel AI Gateway `POST /v1/evaluate` API) is a
schema-typed decision model. It answers `choice` questions with one of the option names
it was given and a probability for each option. It does not return free text. That makes
it fit ADR 0009's shape: the rules build the options, the model picks one, and a pick
outside the options is rejected by the schema before pitwall sees it.

## Decision

1. **Rules stay the only source of calls, facts and words.** Jev sees only calls that
   already passed `_suppression_reason` and were queued. It cannot add, drop or reword a
   call. The spoken text is the rule's `say` pool output (ADR 0008), fixed before Jev is
   asked.
2. **Scope.** Only queued P2/P3 rule calls are candidates. P1 calls, menu prompts, replies
   and say-agains never reach Jev and keep their heap position. Jev is asked only when at
   least two candidates are queued, and sees at most `jev.max_candidates` (default 5), the
   first ones in heap order.
3. **One question.** The request carries a `best_call` choice question. The options are
   the candidate call ids plus `none`; each option's description is the rule id, priority
   and the already-rendered text. The shared `state` is a digest of facts the engine has
   already computed: lap, stint phase, tyre state, gaps and their trends, track evolution
   (lap-time trend, weather), the active plan, `last_pick` and `last_pick_age_s`, the
   heap order, and a short fixed doctrine. Confidence is the probability Jev returns for
   its own choice.
4. **Prefetch at submit, apply at drain.** When `submit` queues a call and two or more
   candidates are waiting, the dispatcher starts the request on a worker thread and
   returns at once. `drain` holds the queued P2/P3 calls for up to `jev.timeout_ms`
   (default 300 ms) while the answer is outstanding; P1 calls are never held. A late
   answer counts as `jev_timeout`. If a candidate left the queue before the answer came
   back, the question is stale and the heap order stands.
5. **Gates.** Jev's pick moves to the head of the queue only when all of these hold:
   - the choice is not `none` (`jev_abstain`);
   - its probability is at least `jev.confidence_threshold` (0.6) and at least the heap
     head's probability plus `jev.margin` (0.15) (`jev_margin`);
   - it does not contradict a different pick made less than `jev.sticky_s` (5 s) ago. The
     first contradiction inside that window keeps the earlier pick. A second one inside
     the window is flapping, and Jev abstains for that decision (`jev_flap`).

   Any failure (timeout, HTTP error, malformed answer, unknown option) falls back to the
   heap order (`jev_timeout`, `jev_error`). The promoted call takes the earliest min-gap
   slot among the candidates and the others keep their order in the later slots, so
   arbitration never speaks sooner or more often than the heap order would.
6. **`still_true` is unchanged.** The reordered call is revalidated at speak time exactly
   as before. Deadlines, straight-only P3, preemption, quiet modes and the lap budget all
   apply after the reorder.
7. **Record, then replay.** Every applied ranking is a decision-log record with outcome
   `arbitrated`: a replay-stable key (session uid plus each candidate's rule id, lap and
   session time), the candidates, the digest, the order, `arbitrated_by`
   (`heap | jev | jev_timeout | jev_error | jev_abstain | jev_margin | jev_flap`),
   confidence, latency and the model string. SQLite keeps them in `arbitrations`, and
   each `calls` row carries the same fields. `pitwall replay` repeats the recorded order
   by key, so a replay says what live said. A decision point with no record replays in
   heap order. `--recompute` asks Jev again and is experimental; its results are never
   fixtures.
8. **Off by default, live last.** `jev.enabled` and `jev.arbitrate` default to false. With
   either off, no request is sent and the dispatcher behaves as before this ADR. The
   path to live use is:
   - `pitwall replay FILE --arbitrate shadow` asks Jev at every decision point of a
     recording, keeps the heap order and prints a heap-vs-Jev diff;
   - `pitwall tune --judge jev` grades recorded decision points and writes
     `(digest, candidates, pick, verdict)` JSONL to `recordings/jev_training/`;
   - `jev.arbitrate: true` is a reviewed config change, made only after shadow replays
     over the corpus show Jev's order beats the heap order and `pitwall diff --corpus`
     loses no must-fire calls. ADR 0009 §6 demotion applies. A model change sends the
     type back to shadow.

## Risks

- **Hosted dependency.** Jev runs behind a hosted gateway. A slow or failed request costs
  at most `timeout_ms` of P2/P3 delay and falls back to the heap order. P1 never waits.
- **League telemetry privacy.** Requests leave the machine. Driver names on the snapshot
  are replaced with neutral labels ("the car ahead") in candidate text and inputs before
  the request is built. The digest carries no names. Lap times, gaps and positions still
  leave the machine; leagues that forbid that keep `jev.enabled: false`.
- **Model pinning.** The model string is in config and recorded on every decision. A
  gateway-side model update can change answers under the same name, which is why the
  recorded order, not a re-query, is what replays use.

## Consequences

- With Jev off, nothing changes: no request, no held calls, no new log records.
- The decision log gains `arbitrated` records, and the `calls` table gains six nullable
  columns (migration 10). Old databases migrate in place.
- Replays depend on the decision log for arbitrated sessions, as ADR 0009 §8 already
  requires for other decision types.
