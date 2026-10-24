# Roadmap

Where f1-pitwall goes next. For what works today and the open gaps, see
[status](status.md). The original M0 to M4 plan is complete and kept as
[history](05-roadmap.md#history-the-m0-to-m4-plan) at the end of this page.

Effort is given in working sessions, not calendar time.

## Next

These items come from the [status](status.md) "Next" list and the open exit criteria of the original plan.

- Measure the game's frame time with and without pitwall, and commit the numbers
  (`09-performance.md`). The target is no measurable effect on the 1 % lows.
- Build the tray app.
- Record more races until calibration reports `converged: true`.
- Record calls-off sessions so `pitwall evaluate` can compare calls on and off.
- Clear the tuning backlog from the qualifying review (listed in [status](status.md)).
- Follow up the reverted 25% race fixes: mixed-weather calls, drive-through tyre calls,
  stop summaries and early-race pace.
- Finish the voice-command dispatcher hold (`21-voice-command.md`).
- Build the guest timing tower (`19-guest-tower.md`).

## Later, if wanted

Later options include defend and survive mindsets, a Streamdeck, a spotter, free-form
voice questions beyond the fixed menu phrases, and an LLM phrasing layer over deterministic
rule outputs. An optional LLM-assisted debrief could provide coaching analysis and Q&A over
debrief artifacts and SQLite history, behind an optional API key (`10-angles-not-yet-considered.md`, item 21).
Keep decisions deterministic and delivery natural, never the reverse.

## Working agreements

- Every rule ships with a replay test. A rule without a fixture does not merge.
- Every session driven gets recorded and the interesting ones get committed as fixtures.
- Thresholds change in YAML, never in code.
- Latency and calls-per-lap are tracked per release; regressions in either are bugs.

## History: the M0 to M4 plan

The original plan sequenced work as vertical slices. Each milestone was usable on its
own and ended with a recording that became a regression fixture. All five are done.
Design docs still tag behaviour with the milestone that introduced it, such as "M3 race".
The text below is the plan as written, so some details, such as SAPI speech, are out of date.

Plan v1 sequenced work by layer (parser → state → engines → UI). That sequence produces
nothing usable until the end and leaves strategy thresholds untested until the last day.

### M0: Capture and replay (~0.5 session)

The foundation that makes every later milestone cheap.

- UDP listener on 20777, raw `.f1bin` recorder with receive timestamps, `.f1idx` sidecar.
- `pitwall replay`: stream a recording through the ingest path at 1×/N×/max and seek by
  lap or event. Use a virtual clock so a 10× replay decides identically to 1×.
- `pitwall trim` for local replay slices; keep recordings out of git (ADR 0005);
  `--stats` packet census.
- Repo scaffolding: uv, ruff, mypy, pytest, GitHub Actions.

**Exit:** Drive one practice session and record it. Replay it at 10× and max speed.
Confirm that both runs produce a correct, identical packet census.
**Deliverable to keep:** that recording.

### M1: One call, end to end (~1 session)

Proves the whole pipe before any strategy complexity.

- Layout tables and generated parsers for Session, Lap Data, Car Telemetry, Car Status,
  Car Damage, Event.
- Session state, snapshot at 10 Hz, fast/slow EMAs, lap accumulator, validity tagging.
- Minimal rule engine with hysteresis and cooldown; one rule: out-lap tyre temperature.
- FastAPI WebSocket + the dashboard page with radio log; backend speech via Windows SAPI. Speech later moved to Piper, with OmniVoice as an option.
- Measure trigger-to-speech-start latency in-process and add a packet-age indicator.
- Layered config loading with validation and hot reload; config hash in the recording.
- `pitwall doctor` (port, firewall, observed format/rate, UDP Action binding) and the tray app.
- First frame-time A/B against a no-backend baseline (`09-performance.md`), numbers
  committed.

**Exit:** On an out-lap, the radio says "front left is cold, drag the brakes through
sector three" within 300 ms of the condition. It stays silent otherwise and has no
measurable effect on the game's 1 % lows.

### M2: Qualifying (~1 session)

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
- Feedback loop: bookmark, post-session feedback screen, `pitwall report` bundle, and
  GitHub issue templates.
- Driver input: Fanatec button 2 (bound to UDP Action 1) and spacebar; single press = acknowledge, double = negative, long press = radio silent on/off (UDP Action 3 also toggles); per-rule backoff from negatives.

**Exit:** a full Q1–Q3 driven with the assistant, with the decision log reviewed
afterwards for false positives. Tune from the recording, not from another run.

### M3: Race (~1 session)

- Lap-time and degradation model fitted online; rival pace from Session History.
- Measured pit loss; track priors as cold start only.
- Pit window optimiser, undercut/overcut, free stop, SC/VSC cheap stop.
- Fuel, ERS and Overtake-mode calls, penalties, blue flags and weather crossover.
- Rival scope filter with the pit-exit rival projection.
- `pitwall replay --mask-restricted`: replay single-player recordings with rival fuel/ERS/wear zeroed, to prove the model degrades gracefully for league races.
- Wire balanced and aggressive mindsets through the planners, setup advisor, and rules.
  Make them switchable from the dashboard and record them per call. Keep defend and survive for later.
- 2026 energy management as a modelled per-lap budget rather than a threshold rule.
- Watchdog and mid-race crash recovery from SQLite plus the tail of the recording.
- First friends-league race (3 humans + AI grid) recorded and reviewed.
- Backlog before merge: driver cheatsheet (UDP action map, and what pitwall does for the
  driver in practice, qualifying and race).

**Exit:** a 25% and a 100% race where the pit recommendation is defensible in review and
the call count per lap stays inside budget.

### M4: Make it better over time (~1 session)

- Post-session HTML debrief: stint plots, deg curves, every call with its inputs and
  whether it proved right. Layout and phases in `16-debrief-design.md`.
- Calibration tool fitting thermal windows and fuel coefficients from recorded sessions.
- Piper TTS fallback with pre-rendered common phrases.
- Optional phone client: Web Speech, HTTPS via mkcert, PWA install, wake lock.
- Threshold tuning pass driven by the accumulated recordings and the corpus diff.
- Per-track energy-deployment map learned from your own laps.
- Honest self-evaluation: lap-time and mistake-rate comparison with calls on versus off.
