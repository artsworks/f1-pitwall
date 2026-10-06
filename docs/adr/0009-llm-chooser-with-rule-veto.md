# ADR 0009: LLM as a gated chooser with rule veto

- **Status:** accepted (2026-09). Supersedes the decision half of ADR 0008's "Why not an
  LLM" and of `10-angles-not-yet-considered.md` item 20. ADR 0008's phrasing decision
  (variant pools, escalation, seeded shuffle) stands unchanged.
- **Context:** setup advisor proposal (`22-setup-advisor.md`) and a review of the LLM
  boundary with the project owner

## Context

ADR 0008 and item 20 kept every model out of the live path. It gave four reasons: latency,
a network dependency, cost, and non-reproducible replays. They were written for *phrasing*,
where the gain was only more varied wording. Two things have changed:

- Fast, cheap models (Flash-tier) make cost negligible and bring latency down to under
  a second for short structured replies. Decisions made at pit-window or debrief speed
  can afford that.
- The decisions pitwall now makes have real judgment in them that the rules only
  approximate. Examples: choosing between pit laps inside the window against rival
  projections, and picking the one setup change to try next from symptoms the research
  rates low or medium confidence (`22-setup-advisor.md` §1.2). A model that sees the whole
  evidence set may choose better than a fixed candidate order. Nobody knows yet, and the
  learning loop (`20-learning-loop.md`) can measure it.

The ban's real justification was never cost. It was that a decision nobody can reproduce
or grade cannot be improved or trusted, and that a free-form model can invent an action
("−3 rear wing", "box this lap" under a red flag). This ADR keeps both protections and
drops the ban.

## Decision

An LLM may **choose among options the deterministic engine has already produced**. It
never creates an option, and it only gets authority for a decision type after beating the
rules on graded outcomes.

1. **Optional, and deterministic without a key.** Every decision type works without a
   model. When no API key is configured (the default), no provider is constructed, no
   request is sent, and every decision is the rules' top candidate. Pitwall then behaves
   exactly as it did before this ADR, including the setup advisor. Tests, fixtures and
   `pitwall replay` run keyless by default. Adding a key never changes what the driver
   hears until a decision type has been promoted (§6).
2. **The rules generate the options and hold the veto.** For each decision type (listed
   below), the engine computes the complete legal candidate set as it does today: pit laps
   from the window optimiser, compound sequences from the plan search, setup deltas from
   `setup_rules.yaml` after parc-fermé, range and contraindication filters, and MFD steps.
   Each candidate carries an id, its evidence and the rules' own ranking. The model
   receives the snapshot summary, the candidates and the evidence. It returns
   `{"choice": "<candidate id>", "reason": "..."}`, and nothing else is accepted.
3. **Fallback is the rules' top choice.** A timeout, schema failure, unknown id, a
   candidate the rules vetoed after the request was sent (`still_true` fails, state
   moved on), a missing key or no network all use the rules' top candidate. The call
   goes out as it would have without the model, and the failure reason is logged.
4. **Phrasing stays deterministic.** The chosen candidate is spoken through the rule's
   YAML `say` pools (ADR 0008). The model's `reason` is never spoken; it appears only on
   the dashboard and in the debrief, labelled.
5. **Decision types and budgets.**

   | Decision type | Where | Timeout (config) | Initial authority |
   |---------------|-------|------------------|-------------------|
   | `setup.debrief`: primary change for the next run | practice debrief | 20 s | shadow |
   | `setup.garage`: single change in the pits | garage card | 3 s | shadow |
   | `strategy.plan`: stop lap and compound sequence among window candidates | re-plan events, ≥ 2 laps before the window opens | 1.5 s | shadow |
   | `strategy.react`: SC/VSC cheap stop, undercut/overcut choice | on the event | 1.0 s | shadow |
   | `mfd.balance`: brake bias / on-throttle step among rule options | race, P3 | 1.0 s | shadow |

   Rules-only, with no model involved: flags, penalties, damage, lock-ups, spins, boost,
   fuel-critical and any P1 call. They are safety- or time-critical and have one right
   answer.
6. **Authority is earned per decision type, and can be taken away.**
   `off → shadow → chooser`. In **shadow**, the model's choice is requested, logged and
   graded, but the rules' choice is used. Promotion to **chooser** is a reviewed config change
   (`llm_authority.<type>: chooser`). Automatic promotion isn't built
   (`20-learning-loop.md`). Two things must be true before the change is made. First, over ≥ `llm_promote_min_graded`
   graded decisions, the model's picks do at least as well as the rules' by
   `llm_promote_margin`. Second, `pitwall diff --corpus` using the logged model responses
   loses no must-fire calls. The decision type drops back to shadow automatically if its rolling
   hindsight grade falls below the rules' over the last `llm_demote_window` graded
   decisions. A change of model version also sends it back to shadow.
7. **Grading uses the existing loop.** The hindsight grader grades whichever choice was
   used. A shadow pick is graded where the grader can price the alternative. `stop_cost_s`
   already searches every legal in-lap, so it can price a different stop lap. In setup
   debriefs every candidate is listed, so a shadow pick is graded when the driver happened
   to run it. Picks the grader can't price are `censored`, never assumed good. Outcomes go
   in `outcomes` with `rule_id = "llm:<decision type>"` next to the rule's own row. That
   way `pitwall digest` and `pitwall tune` compare the model and the rules with no new
   plumbing.
8. **Record, then replay.** Every request is logged to the `.jsonl` decision log and to
   SQLite (`llm_choices`). The entry holds: the request hash, the model id with its
   version, the prompt, the reply, the latency, the parsed choice and the fallback reason.
   `pitwall replay` reads the logged reply by request hash, so a replay reproduces the
   live session exactly, and tests use the log or a stub provider. `--llm live` re-queries
   the model for A/B runs, and those results are reported but never used as fixtures. A
   recording with no log entry replays with the rules' choice, so every existing recording
   and test keeps its current result.
9. **Second opinion on the dashboard.** For any decision type, including rules-only
   ones, the dashboard may show the model's pick and reason in a panel labelled
   `GENERATED · <model>`, even while the type is in shadow. It is never spoken and never
   acted on. The panel is hidden if the reason contains a number or name that isn't in the
   request's inputs (the same check as the §08 generated section in
   `16-debrief-design.md`).
10. **Config and privacy.** The provider sits behind `complete(system, user) -> text`
   (`16-debrief-design.md` §6). The key is optional and the exact model version is set in
   config. With no key, pitwall behaves exactly as before. In league sessions, rival names
   are replaced with slot labels before a request leaves the machine, unless a local model
   is configured (items 13 and 15).

## Consequences

- The model can't produce an illegal or invented action, because every candidate passed
  the same filters the rules use. The worst it can do is pick a worse legal option, and
  the grader measures exactly that.
- Replays and fixtures stay exact, but only together with the decision log. A recording
  without its log replays rules-only. The decision log now has to be kept alongside the
  `.f1bin` (still local-only, ADR 0005).
- Every decision type starts in shadow, so on day one nothing the driver hears changes.
  Authority arrives one decision type at a time, from graded evidence.
- The network is a soft dependency: losing it mid-race leaves the rules deciding, which
  is how every call works today.
- Cost is bounded by the number of decisions, not packets: a handful per stint, a few
  dozen per debrief.
- `18-race-engine.md`'s "every call is a YAML rule" still holds. Rules still produce every
  call and every option; the model only chooses between options.
- Voice input (`21-voice-command.md`) is unchanged. This ADR covers choices, not
  hearing or speaking.
