# Glossary

Terms with a precise meaning in this repo. Each entry names the code that owns the
behaviour; read that code for the details.

**Call.** One thing the race engineer says or shows. A rule in
`config/defaults/rules/*.yaml` produces a candidate call (text, priority, urgency class,
tags) from a snapshot. The dispatcher (`audio/dispatcher.py`) queues, rate-limits,
revalidates (`still_true`) and then speaks or drops it. A `screen_only` call is shown and
never spoken.

**Priority.** A rule's number from 1 to 3: 1 critical, 2 tactical, 3 informational.
Priority controls budgets, deadlines, quiet mode and exemptions. P1 calls never count
against `call_budget_per_lap`.

**Urgency class.** The queue order, separate from priority: `safety` (0), `execution` (1),
`reply` (2, answers to driver-menu requests), `tactical` (3), `info` (4), `coaching` (5).
A rule sets it with `urgency` or `urgency_by_priority`; otherwise priority 1, 2 and 3 map
to execution, tactical and info (`RuleDefModel.urgency_class`). Near a decision point a
promoted execution call ranks -1.

**Snapshot.** The frozen per-tick (10 Hz) view of `SessionState` that rules read. Rules
never see raw packets.

**Mindset.** A named set of numeric biases that rules read as `mode.*` (for example
`mode.pit_gain_min_s`), not a separate rule set. `balanced` is the default and
`aggressive` is the other tuned set. A mindset changes appetite and call frequency, never
a priority-1 fact. Only the driver changes it; the assistant may suggest a change.

**Thresholds (`th.*`).** Tuned numeric limits from the packaged defaults, the user profile
and the track overlay, last wins. Rules read them as `th.*`.

**Session phase.** Where the player is within a session. Qualifying and practice follow
the game's driver status (`state/session.py`): `garage`, `out_lap`, `flying`, `in_lap`,
`on_track`. A race follows `state/race.py`: `formation → racing → sc | vsc → racing`,
`racing → in_lap → out_lap → racing` around a stop, then `finished`.

**Out-lap / in-lap.** The lap leaving the pits and the lap entering them. Neither lap,
nor the lap after an in-lap, enters the pace model.

**Hot lap / cool lap.** In qualifying, a push lap and a slow lap between pushes. The game
keeps reporting `flying` on a cool lap, so `RunTracker` (`state/runplan.py`) tells them
apart by pace.

**Stint.** A continuous run on one tyre set, from pit exit (or the start) to the next
stop or the session end. A stint is the unit of the degradation fit
(`model/deg.py` `fit_stint`).

**Degradation fit (`DegFit`).** Lap time modelled from `base_ms`, `deg_ms_per_lap` per lap
of tyre age, an optional quadratic `deg2_ms_per_lap2`, and fuel burned. Short fits blend
toward a prior.

**Cliff age (`cliff_age`).** The tyre age at which modelled degradation reaches
`tyre_cliff_ms` (`model/deg.py`). `laps_of_pace` is the cliff age minus the current tyre
age, capped by corner wear limits.

**Prior.** A learned or packaged starting value for a model parameter (pit loss,
degradation, fuel, thermal, energy). Learned priors live in the `model_params` table and
are folded as weighted running means (`fold_param`). Packaged priors come from the
per-track overlay in `config/defaults/tracks/`. Race priors are scoped by race distance,
for example `deg_ms_per_lap@52L`; practice priors are unscoped.

**Quarantine.** Isolation of a learned value that fails the plausibility check, with a
reason. Refits never bring a quarantined value back.

**Upkeep.** The `maintain()` step that `pitwall start` runs before rules start, in one
database transaction: rebuild stint values, quarantine bad values, grade ungraded
sessions, refit priors and refold rule cooldowns. It then writes refit values to
`~/.pitwall/tracks/<id>.yaml`. Upkeep is idempotent.

**Grading.** The hindsight verdict on each call (`learning/hindsight.py`), such as `good`
or `wrong`. Upkeep and session end grade automatically; a human grade replaces the
automatic one.

**Debrief.** The post-session review page (`/debrief/<session>`, `pitwall debrief`):
pace, stints, calls and their grades, read after the session rather than at a glance.

**Plan A/B/C.** The named race strategies: the primary plan, the alternative, and the
safety-car reactive plan (`strategy/plans.py`).

**Pit loss.** The time a stop costs versus staying out, measured per track and per
neutralisation state (green, VSC, SC). The packaged table is only a cold-start prior.

**Recording.** The raw UDP datagrams of a session with receive timestamps, in `.f1bin` or
`.f1bin.zst` with an `.f1idx` index. Recordings stay on the game PC and never enter git
(ADR 0005).

**Scenario.** A YAML file in `scenarios/` that `pitwall bench` replays and scores: a source
recording, optional mutations, and the calls that must or must not fire.

**Restricted telemetry.** Rival data the game zeroes when another human sets *Your
Telemetry: Restricted*. Models fall back to compound, tyre age and lap times.
