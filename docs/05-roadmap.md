# Roadmap

Plan v1 sequenced by layer (parser → state → engines → UI), which only produces something
usable at the very end and leaves every strategy threshold untested until the last day.
These milestones are vertical slices: each one is independently usable and each one ends
with a recording that becomes a regression fixture.

Effort is given in working sessions, not calendar time.

## M0 — Capture and replay (~0.5 session)

The foundation that makes every later milestone cheap.

- UDP listener on 20777, raw `.f1bin` recorder with receive timestamps, `.f1idx` sidecar.
- `pitwall replay`: stream a recording through the ingest path at 1×/N×/max, with seek by
  lap and by event; virtual clock so a 10× replay decides identically to 1×.
- `pitwall trim` for committable fixtures; header parsing and packet-ID counters;
  `--stats` packet census.
- Repo scaffolding: uv, ruff, mypy, pytest, GitHub Actions.

**Exit:** drive one practice session, record it, replay it at 10× and see a correct
packet census, and at max speed with an identical census. **Deliverable to keep:** that
recording.

## M1 — One call, end to end (~1 session)

Proves the whole pipe before any strategy complexity.

- Layout tables and generated parsers for Session, Lap Data, Car Telemetry, Car Status,
  Car Damage, Event.
- Session state, snapshot at 10 Hz, fast/slow EMAs, lap accumulator, validity tagging.
- Minimal rule engine with hysteresis and cooldown; one rule: out-lap tyre temperature.
- FastAPI WebSocket + the dashboard page with radio log; backend speech via Windows SAPI.
- Latency instrumentation end to end, trigger → speech start measured in-process; packet-
  age indicator.
- Layered config loading with validation and hot reload; config hash in the recording.
- `pitwall doctor` (port, firewall, observed format/rate, UDP Action binding seen) and the tray app.
- First frame-time A/B against a no-backend baseline (`09-performance.md`), numbers
  committed.

**Exit:** on an out-lap, the radio says "front left is cold, drag the brakes through
sector three" within 300 ms of the condition, and stays silent otherwise — with no
measurable effect on the game's 1 % lows.

## M2 — Qualifying (~1 session)

- Remaining packets: Participants, Session History, Tyre Sets, Car Setups, Car
  Telemetry 2.
- Flashback/pause/red-flag handling against the `FLBK` and `RDFL` events.
- Clean-air release window using projected car positions at pit exit.
- Abort advisory based on projected lap time versus the cut-off from the field's Session
  History, weighted by remaining sets and ERS.
- Full dispatcher: priorities, preemption, dedupe, global budget, verbosity presets,
  quiet mode.
- SQLite persistence (with a migration approach chosen now, not later) and the JSONL
  decision log.
- Review mode: replay with a scrubbable timeline of calls and suppressions, plus the
  "grade this call" control.
- `pitwall diff` for A/B-ing two rule configs over one recording or a corpus.
- Feedback loop: long-press bookmark, post-session feedback screen, `pitwall report` bundle, GitHub issue templates.
- Driver input: Fanatec button 2 (bound to UDP Action 1) and spacebar; single press = acknowledge, double = negative; per-rule backoff from negatives.

**Exit:** a full Q1–Q3 driven with the assistant, with the decision log reviewed
afterwards for false positives. Tune from the recording, not from another run.

## M3 — Race (~1 session)

- Lap-time and degradation model fitted online; rival pace from Session History.
- Measured pit loss; track priors as cold start only.
- Pit window optimiser, undercut/overcut, free stop, SC/VSC cheap stop.
- Fuel and ERS/Overtake-mode calls; DRS, penalties, blue flags, weather crossover.
- Rival scope filter with the pit-exit rival projection.
- `pitwall replay --mask-restricted`: replay single-player recordings with rival fuel/ERS/wear zeroed, to prove the model degrades gracefully for league races.
- Mindsets wired through the rules: balanced / aggressive (defend and survive after M3), switchable from
  the dashboard, recorded per call.
- 2026 energy management as a modelled per-lap budget rather than a threshold rule.
- Watchdog and mid-race crash recovery from SQLite plus the tail of the recording.
- First friends-league race (3 humans + AI grid) recorded and reviewed.

**Exit:** a 25% and a 100% race where the pit recommendation is defensible in review and
the call count per lap stays inside budget.

## M4 — Make it better over time (~1 session)

- Post-session HTML debrief: stint plots, deg curves, every call with its inputs and
  whether it proved right.
- Calibration tool fitting thermal windows and fuel coefficients from recorded sessions.
- Piper TTS fallback with pre-rendered common phrases.
- Optional phone client: Web Speech, HTTPS via mkcert, PWA install, wake lock.
- Threshold tuning pass driven by the accumulated recordings and the corpus diff.
- Per-track energy-deployment map learned from your own laps.
- Honest self-evaluation: lap-time and mistake-rate comparison with calls on versus off.

## Later, if wanted

Defend and survive mindsets, a Streamdeck, a spotter, voice input ("how's the gap?"), an LLM phrasing layer over the deterministic rule
outputs, and an optional LLM-assisted debrief — coaching analysis and Q&A over the M4
debrief artifacts and SQLite history, behind an optional API key
(`10-angles-not-yet-considered.md`, item 21) — deterministic decisions, natural delivery,
never the reverse.

## Working agreements

- Every rule ships with a replay test. A rule without a fixture does not merge.
- Every session driven gets recorded and the interesting ones get committed as fixtures.
- Thresholds change in YAML, never in code.
- Latency and calls-per-lap are tracked per release; regressions in either are bugs.
