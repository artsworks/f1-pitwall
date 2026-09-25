# Debrief screen: the post-session review page

Status: **design only** (M4 target). Mockup with static sample data:
`docs/mockups/debrief.html` — vanilla HTML/CSS/JS, self-contained, no build step. All
numbers in it are illustrative; the source line under each panel names the real table or
artifact it would be built from.

This doc does for the debrief what `15-dashboard-design.md` does for the live screen. The
two are different pages with different jobs and share only the visual tokens. It also
gives item 21 of `10-angles-not-yet-considered.md` (LLM-assisted debrief) its UI home.

## 1. Why a separate page

The live dashboard is glanced at for under a second while driving; it deliberately
excludes lap tables, traces, sector deltas and call grading (`15-dashboard-design.md`,
non-goals). A debrief is the opposite activity: the session is over, the driver is
reading, scrolling, comparing, and answering "was that right?" The page is therefore a
**report** — long, vertical, dense, readable at desk distance — not a glanceable panel.
Forcing it into the live layout would break both.

**Goals**

1. Answer, in order: where did the result come from, where did the time go, were the
   engine's calls right, and what do I do differently next session.
2. Every number traceable to a deterministic source (`laps`, `stints`, `pit_events`,
   `calls`, the `.jsonl` decision log, the `.f1bin` index). Charts are built from those
   tables, never from prose.
3. Every call in the session reviewable with the exact inputs its predicate read, and
   gradable (right / noise / late / wrong) — the grade feeds rule tuning and the
   replay-test corpus (`07-replay-and-debug.md`).
4. Works as a static HTML file (openable years later without the backend) *and* served
   by the backend at `/debrief/<session>` so grading and "open in review mode" work.
5. Same tokens as the live screen: dark, high contrast, monospace numerics, colour always
   paired with a word, no framework, no CDN.
6. Optional generated prose (item 21) is visually unmistakable, hideable, and anchored
   to the deterministic sections it talks about.

**Non-goals**

- Not a telemetry viewer. No scrubbing raw channels; per-corner traces belong to review
  mode and need a full (not lite) recording.
- Not live. Nothing on the page updates during a session.
- Not the decision-maker. Grades and generated text never change a rule; they inform
  the human who edits the rules.

## 2. What a debrief covers (research)

Real F1 debriefs are structured performance reviews, not open conversation: engineers
lead with data, the driver gives feedback, then strategy, tyres, pit stops, traffic and
energy are reviewed in turn, ending with actions for the next session or event. The
attendees are the driver, race engineer, performance engineer, strategy, tyre and data
engineers. Public team write-ups (McLaren's per-race "Strategy Debrief", Mercedes'
"Race Debrief" videos) frame strategy as *decision → alternatives considered → outcome*,
and post-race analysis pieces state findings as **time deltas at specific laps** ("out of
undercut range by ~3.5 s", "damage cost >0.5 s/lap").

Engineering tools converge on a small chart vocabulary: MoTeC i2 and McLaren Applied
ATLAS organise work into workbooks/worksheets of traces on a shared distance axis with a
running time-difference trace and sector stripes; sim tools (VRS, Garage61, Popometer,
Coach Dave Delta, Z1 Analyzer) put a delta-vs-reference chart on top, a lap list with
per-sector gain/loss, and — in the last three — plain-English findings beside the charts,
each linking back to the corner it describes. Strategy analysis uses the **race trace**
(cumulative time vs a reference lap; slope = pace, vertical drop = pit stop), the
**compound-coloured stint bar** per driver, and the **lap-time-vs-lap scatter coloured by
compound** with SC/pit laps filtered so degradation shows as within-stint slope.

Translated to a solo sim driver with Pitwall's data, the debrief agenda is:

| # | Section | Question it answers | Primary source |
|---|---------|---------------------|----------------|
| 00 | Summary | What happened, in one paragraph and four numbers | derived from all below |
| 01 | Pace & stints | How fast, how consistent, how did tyres fall off, what did rivals do | `laps`, `stints`, `pit_events`, Session History |
| 02 | Sectors | Where on the lap the time went, and is it the same every session | `laps.sector*_ms` across the weekend |
| 03 | Tyres | Deg per stint vs prior sessions, thermal window hit rate, wear at stop | `stints`, 10 Hz downsample (tyre temps) |
| 04 | Strategy calls | Each pit/strategy call: inputs, projection, alternative, outcome, verdict | `calls`, decision log, `pit_events` |
| 05 | Radio & decisions | Every call in order, exact text, audio fate, ACK/NEG, grade | decision log, `calls` |
| 06 | Incidents & energy | Lock-ups, SC, damage, off-tracks with time cost; fuel and ERS use | event packets, `.f1idx`, 10 Hz downsample |
| 07 | Actions | Deterministic "next session" list derived from 01–06 | rules over the above |
| 08 | Engineer's notes | *Optional* generated prose and Q&A (item 21) | sections 00–07 only |

## 3. Layout

```
┌──────────────┬────────────────────────────────────────────────────────┐
│ pitwall      │ BAHRAIN · RACE 50% · 28 LAPS                            │
│ · DEBRIEF    │ date · P4 from P6 · MED 1–12 → HARD 13–28 · league     │
│              │ [tag: keep] [open in review mode] [export HTML]         │
│ 00 Summary   ├────────────────────────────────────────────────────────┤
│ 01 Pace      │ 00 SUMMARY  [DETERMINISTIC]                             │
│ 02 Sectors   │ one paragraph · 4 headline cards                        │
│ 03 Tyres     ├────────────────────────────────────────────────────────┤
│ 04 Strategy  │ 01 PACE & STINTS                                        │
│ 05 Radio     │ lap-time scatter (full width) · stint bars | race trace │
│ 06 Incidents ├────────────────────────────────────────────────────────┤
│ 07 Actions   │ ...one section per agenda row, in order...              │
│ 08 Notes GEN ├────────────────────────────────────────────────────────┤
│              │ 08 ENGINEER'S NOTES  [GENERATED · model]  (dashed box)  │
│ recording,   │ claims → §01 §02 … · ask box · hide toggle              │
│ config hash  │                                                         │
└──────────────┴────────────────────────────────────────────────────────┘
```

- **Sticky left agenda** (16 rem) with the section numbers; the active section
  highlights on scroll. The footer of the nav carries provenance: recording file, lite /
  full, size, config hash, rules version, mindset — the same identity fields the decision
  log records, so a debrief can always be matched to its inputs.
- **Main column** capped at ~76 rem for reading; panels use two or three columns where
  a chart and its table belong side by side.
- **Section header** = number, title, and a `DETERMINISTIC` or `GENERATED` badge where
  the distinction matters.
- Below 900 px the nav becomes a static block above the content and multi-column panels
  collapse. Print hides the nav and interactive controls so "export HTML → print to PDF"
  produces a readable document.

## 4. Visual grammar

Every chart obeys the same rules so the page reads as one document:

| Element | Convention |
|---------|-----------|
| Compound | Filled marker / bar in compound colour (soft red, medium yellow, hard white, inter green, wet blue) |
| Invalid lap | Hollow marker, never in fits; the reason (`pit`, `SC`, `flashback`, `after_in_lap`) in the tooltip / legend |
| Degradation fit | Dashed line per stint; slope printed as s/lap beside it |
| Safety car | Amber vertical band spanning the SC laps |
| Pit stop | Dashed vertical line at the in-lap plus the called window as a bracket |
| Rivals | Neutral greys; the driver is always the accent colour |
| Alternative the engine projected but didn't take | Same shape, dimmed |
| Deltas | Signed, in seconds, colour = good/bad **and** a word or arrow ("you lose ←") |
| Verdicts | `right` / `noise` / `late` / `wrong` — the grading vocabulary from review mode |
| Source line | Every panel ends with the table / column / artifact it was built from |

Findings are stated in driver currency (seconds per lap, laps of fuel, seconds of undercut
margin), matching `03-strategy.md`'s rule that raw coefficients never reach the UI.

## 5. Section detail

**00 Summary.** One deterministic paragraph assembled from templates (the same
machinery as ADR 0008 phrasing): result vs grid, biggest sector delta, biggest strategy
delta, call tally. Four cards: race pace (mean valid lap, rank in field), consistency
(σ of valid laps, best in field), pit stop (lap, loss vs green pace, called window),
calls (total / right, with noise / late / wrong counts).

**01 Pace & stints.** Lap-time scatter by lap (§4 grammar) with a hover row showing
lap, sector split, compound, age, fuel and validity. Under it, stint bars for the driver
and the two cars finishing either side, from Session History; the engine's untaken
alternative shown dimmed. Beside them the race trace against the field median valid lap.
SC laps are drawn neutral (the field bunches) so a stop's vertical drop stays legible.

**02 Sectors.** Mean sector delta to the car ahead on valid laps, as a signed bar chart,
and the same three numbers per session across the weekend so a recurring loss stands
out. S3 is derived (lap − S1 − S2) until `LapSummary` grows a third sector. Corner-level
splits are out of scope here (need a full recording; belong to review mode).

**03 Tyres.** Per stint: fitted deg (s/lap per lap of age), the prior-session fit for the
same compound, laps used and R². Thermal: % of green laps with inner temperature cold /
in window / hot per corner, from the 10 Hz downsample. Wear at stop and finish.

**04 Strategy calls.** One card per strategy-class call (`pit_window`, `undercut`,
`free_stop`, `extend`…) with five fixed rows: *called* (exact text, lap, confidence),
*inputs* (gaps, deg, pit loss — what the predicate read), *alternative* (what the engine
also projected), *outcome* (what actually happened, in seconds), *verdict*. This is the
McLaren "decision → alternatives → impact" shape, computed deterministically from the
decision log plus post-hoc pit-loss and gap measurements.

**05 Radio & decision log.** A timeline strip of every call by lap (coloured by class),
then the list: lap, rule id, exact spoken text, audio fate (spoken / interrupted /
dropped), driver ACK / NEG, and grade buttons. Each row expands to the predicate inputs
and — for suppressed calls — which suppression layer fired. Grades post to the backend
and land in `calls.grade`; in the static export they are read-only.

**06 Incidents & energy.** Event table (lock-up, off-track, slide, contact, damage,
SC/VSC) with lap, corner where the recording has it, and estimated time cost against the
stint fit. Fuel: laps remaining at start / finish vs target delta. ERS: Overtake Mode
activations by lap and where deployment strategy changed.

**07 Actions.** Short deterministic list generated by rules over sections 01–06 (e.g.
"S2 −0.3 s in every session → investigate in review mode"; "FL hot 23 % of stint 1 →
manage FL from lap 3"; "rule X fired 4× and graded noise 3× → raise threshold"). Each
action links to the section that produced it. This is the debrief's real output.

**08 Engineer's notes (optional, generated).** See §6.

## 6. The generated section

Item 21 sits inside the ADR 0008 boundary: a model may phrase or analyse, never decide.
On this page that becomes a set of presentation rules, following the disclosure pattern
that tools like Z1 Analyzer and Coach Dave Delta already use (plain-English findings
beside the charts, each linking to its evidence) and the general convention that
AI-generated content is labelled in a clear, distinguishable way:

1. **One place.** Generated prose lives only in §08 (and, if built, the answers to the
   ask box). It never appears inside a deterministic panel, card or action.
2. **Unmistakable container.** Dashed border, distinct tint, a `GENERATED · <model>`
   badge, and a header line listing the provider, the input sections, the token count and
   cost. The nav entry carries the same `GEN` badge.
3. **Anchored claims.** Each paragraph ends with the sections it relied on (`§01 §04`),
   rendered as links that scroll to them. A claim with no anchor is a prompt bug.
4. **Grounded inputs.** The model receives the structured stats behind §00–§07 and the
   decision log — never raw packets, never the `.f1bin`. Small context, low hallucination
   surface, and the page's own numbers are the only numbers it can quote.
5. **Advisory footer.** A fixed line: generated text never changes a decision, a grade or
   an action; the deterministic sections above are the record.
6. **Hideable.** A "show generated text" toggle (persisted in `localStorage`) hides §08
   and its nav entry entirely. Static exports honour the state at export time.
7. **Absent by default.** With no API key configured the section is simply not rendered;
   nothing else on the page changes.

**Ask box.** Freeform Q&A ("why did I lose time in S2 all weekend?") is the same
pipeline with the question appended; answers render as further anchored paragraphs. It
is served-page only (needs the backend); the export shows any answers already given.
Whether a `pitwall debrief --ask` CLI is also wanted stays an open point in item 21.

**Model tier.** The job is phrasing and correlation over pre-computed stats, not
reasoning over raw data, so the fast, cheap tier is preferred over frontier models:
Gemini Flash, OpenAI's mini/nano class, DeepSeek-V3, Zhipu GLM-Flash, or a local model
where league privacy (items 13 / 15) rules out sending rivals' names off-machine. The
provider sits behind a small interface (`complete(system, user) -> text`) so the model is
one line in the layered config (`08-configuration.md`) next to the optional API key.

## 7. Data the page needs that doesn't exist yet

- `calls.grade` and `calls.audio_fate` columns (grading is currently review-mode only).
- `stints` fit fields: `deg_s_per_lap`, `fit_laps`, `r2`; and the *prior-session* lookup
  for the same track/compound.
- Thermal-window aggregates per stint from the 10 Hz downsample (or computed at debrief
  time from it).
- Rival stints and pit laps persisted from Session History packets.
- Event rows (lock-up, off-track, damage) with lap and, where known, corner — the `.f1idx`
  already indexes event packets; the debrief needs them in SQLite.
- A `debrief_meta` row per session: recording path, lite/full, config hash, rules
  version, mindset — for the nav footer and for pairing with the decision log.

These are additive schema changes; item 16's migration rule applies.

## 8. Phased plan

| Phase | Scope | Output |
|-------|-------|--------|
| D1 | `tools/debrief.py` renders §00, §01 (scatter, own stints), §05 (list, no grading) from SQLite + decision log to a static HTML file | first useful debrief from an M2 session |
| D2 | §02, §03, §04, §06 once the schema additions in §7 land; rival stints and race trace from Session History | full deterministic report |
| D3 | Served at `/debrief/<session>` with grading, "open in review mode" deep-link, tag/keep, export | grading loop closed |
| D4 | §07 action rules | debrief produces a to-do list |
| D5 | §08 behind an API key: provider interface, prompt over §00–§07 stats, anchored rendering, hide toggle; ask box | item 21 delivered |

Each phase is testable by rendering a recorded session (`07-replay-and-debug.md`) and
diffing the produced HTML against a golden file, the same way replay tests pin decisions.
