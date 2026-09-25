# Review of plan v1

Every spec claim below was checked against the published F1 26 / packet-format-2026
UDP structures (see `reference/f1-26-udp-notes.md`). Items are ordered by how much
damage they would do if built as written.

## A. Spec errors that would produce wrong calls

| # | v1 says | Reality (format 2026) | Impact |
|---|---------|----------------------|--------|
| A1 | Race mode when `m_sessionType == 10..13` | 10–14 are **Sprint Shootout**; Race is **15, 16, 17**. Quali is 5–9. | Race engine never runs in a race; runs during sprint shootout instead. Fatal. |
| A2 | `m_tyresWear[4]` in Car Status (7) | Tyre wear, tyre damage and **tyre blisters** are in **Car Damage (10), 10 Hz**. Car Status has compound, age, fuel, ERS, DRS. | Wear cliff logic reads a field that does not exist. |
| A3 | `m_raceDistance` (3 = 25%, 5 = 50%, 7 = 100%) | No such field. Session packet has `m_sessionLength` (0 = none, 2 = very short … 7 = full) and `m_totalLaps`. | Distance scaling misfires. Prefer `m_totalLaps` — it is what the strategy actually needs. |
| A4 | `m_deltaToCarInFrontInMS` | Split fields: `m_deltaToCarInFrontMSPart` + `m_deltaToCarInFrontMinutesPart` (same pattern for delta-to-leader and sector times). | Gaps over 60 s silently wrap; sector times wrong for any lap over a minute (i.e. all of them). |
| A5 | `m_weatherForecastSamples[56]` | Array is **64**, with `m_numWeatherForecastSamples`; each sample is tagged with the session type it applies to. | Struct size mismatch → the whole Session packet mis-parses. Also must filter samples to the current session. |
| A6 | Pit-loss dict keyed 0–25 contiguously | Track IDs are **sparse**: no 1 (Paul Ricard), no 8, 18, 21–25; Zandvoort 26, Imola 27, Jeddah 29, Miami 30, Las Vegas 31, Losail 32, reverse layouts 39–41, Madrid 42. | Almost every track gets another track's pit loss → undercut/overcut math wrong everywhere. |
| A7 | Packet 16 = "MGU-K harvest rate + active aero (0 = high downforce, 1 = low drag)" | Car Telemetry 2 (16) holds `m_activeAeroMode` (**0 = corner, 1 = straight**), `m_activeAeroAvailable`, `m_activeAeroActivationDistance`, **Overtake Mode** (available / active / activation distance), `m_2026Regulations`, `m_drivingWrongWay`. Harvest data lives in Car Status. | Wrong fields, and the 2026 Overtake Mode — arguably the highest-value new call — is missed entirely. |
| A8 | Per-packet frequency column | Session 2 Hz; Car Damage 10 Hz; Participants every 5 s; **Session History and Tyre Sets 20 Hz round-robin, one car per packet**; Motion / Lap Data / Car Telemetry / Car Status / Car Telemetry 2 at the menu rate. | Lap Data is not 20 Hz and Car Status is not 2 Hz; the round-robin packets need per-car staleness tracking, not a simple "latest" slot. |
| A9 | Compounds implicitly soft/medium/hard | Actual compounds are **C0–C6** (16 = C5, 17 = C4, 18 = C3, 19 = C2, 20 = C1, 21 = C0, 22 = C6), plus 7 = inter, 8 = wet. Visual compound is separate. | Thermal windows and deg models must key on *actual* compound, not visual. |
| A10 | Flashback via `sessionTime <= previous sessionTime` | The header carries `m_overallFrameIdentifier`, which **does not rewind on flashback**, and there is an explicit **`FLBK` event** with the target frame identifier. | Use the authoritative signals; keep the sessionTime regression only as a pause backstop. Heuristic alone both false-positives (out-of-order UDP) and false-negatives. |

## B. Packets the plan ignores that it needs

- **Event (3)** — `SCAR` (safety car), `DRSE`/`DRSD`, `FLBK`, `RDFL` (red flag), `PENA`, `RTMT`, `COLL`, `SPTP`, `STLG`/`LGOT`, `CHQF`. The plan polls `m_safetyCarStatus` at 2 Hz for something the game pushes instantly.
- **Participants (4)** — driver names and teams (radio calls say "Verstappen", not "car index 4"), and whether a rival is AI or human.
- **Session History (11)** — every rival's lap times, sector times and stint compounds. Without it there is no real undercut math, only guesswork about rival pace.
- **Tyre Sets (12)** — which sets you actually still have, their wear, `m_lifeSpan`, `m_usableLife`, `m_lapDeltaTime`. A strategy engine that recommends a compound you no longer own is worse than no engine.
- **Final Classification (8)** — clean end-of-session trigger for the debrief.

Also unused but already available: `m_fuelRemainingLaps` / `m_fuelInTank` / `m_fuelMix`,
`m_ersHarvestLimitPerLap` / `m_ersDeployedThisLap`, `m_drsAllowed` + `m_drsActivationDistance`
+ the Session packet's DRS and active-aero zone lists, `m_cornerCuttingWarnings`,
`m_pitLaneTimeInLaneInMS` / `m_pitStopTimerInMS`, `m_vehicleFiaFlags`.

## C. Architecture

**C1. The "zero-GC to avoid in-game micro-stutter" premise is wrong.** The backend is a
separate OS process (often a separate machine). Its garbage collector cannot stall the
game's render loop; it can only add latency to your own calls. Replace the goal with an
explicit, measured latency budget:

- packet received → WebSocket frame out: p99 < 50 ms
- trigger condition true → speech starts on the phone: p99 < 300 ms for priority 1

Keep the cheap hygiene (`struct.unpack_from` into preallocated slotted objects, no
per-frame dicts, `gc.freeze()` after warm-up), but justify it with a benchmark, not a
theory. At 60 Hz × ~9 packet types this is roughly 500–900 packets/s, which is not a hard
problem for Python — the risk is jitter, not throughput.

**C2. The "< 1 MB total memory / keep 5 laps" constraint throws away the product.** The
history the plan discards is exactly what makes calls good: per-track, per-compound
degradation, self-calibrated pit loss, and a post-session debrief. Memory is not scarce —
a two-hour race downsampled to 10 Hz is tens of MB. Keep the hot-path ring buffer, and
*persist* every lap summary plus a 10 Hz downsample to SQLite.

**C3. There is no record/replay harness — this is the single biggest omission.** Dump raw
UDP with receive timestamps to disk; replay at 1×, 10× or instant. Without it, every
threshold tweak costs a full race. With it you get deterministic tests ("given replay
`silverstone-race-01`, the engine emits exactly these calls at these lap distances"), and
strategy tuning becomes an offline loop. Build it first, before anything else.

**C4. Hand-written struct format strings will be the main maintenance cost.** The game
emits formats 2024, 2025 and 2026, and EA revises layouts mid-year via `m_packetVersion`.
Define packet layouts declaratively (one YAML/table per format+packet+version), generate
the parsers, dispatch on `m_packetFormat` / `m_packetId` / `m_packetVersion`, and fail
loudly on an unknown combination rather than reading garbage.

**C5. `quali_engine.py` / `race_engine.py` as imperative code will rot.** Model every call
as declarative rule data: id, trigger predicate, priority, hysteresis band, cooldown,
max-per-stint, required packets, phrasing template, and a "still true?" revalidation
predicate. The engine evaluates rules against an immutable per-tick snapshot. Tuning
becomes a config edit, and each rule gets a one-line replay test.

## D. Audio and message policy

**D1. Spam is the failure mode that kills these tools, and v1 has no defence beyond a
queue.** Add per-rule cooldowns, hysteresis (e.g. enter at 104 °C, clear at 99 °C),
deduplication, a global budget (≤ N calls per lap), suppression of anything already
legible on screen, and — for priority 3 only — a "speak on a straight" gate using
`m_lapDistance` against the track's braking zones.

**D2. TTL of 1500 ms is too aggressive and measures the wrong thing.** A spoken call takes
2–4 s; queueing plus speech start can exceed 1.5 s on its own, so nearly everything would
be dropped. Use per-priority deadlines (P1 5 s, P2 3 s, P3 1.5 s) *and* revalidate the
triggering condition at speak time. "Is this still true?" is the real question; TTL is a
crude proxy for it.

**D3. Merging P1 and P2 into one sentence delays the critical half.** Speak P1 immediately
(cancelling current speech), queue P2 behind it.

**D4. Web Speech API constraints the plan does not account for:** it needs a user gesture
to unlock (add an explicit "arm radio" tap screen), it stops when the tab is backgrounded
or the screen sleeps, and **Screen Wake Lock requires a secure context** — plain
`http://192.168.x.x` will not get it. Either serve HTTPS with a locally trusted cert
(mkcert), which also enables PWA install, or accept manually keeping the screen on.
Android voice quality and latency vary by device, so keep a server-side fallback in
scope: local Piper TTS, with the ~50 most common phrases pre-rendered for near-zero
latency and a consistent voice.

## E. Frontend and networking

- **Do not use the Tailwind CDN.** The race PC may be offline and the phone should never
  depend on the internet mid-race. Vendor the CSS into the repo.
- **Do not push 60 Hz to the UI.** Send a state snapshot at 5–10 Hz plus a separate event
  channel. The eye cannot read faster, and the phone stays cool and connected.
- Define a versioned WebSocket envelope (`{v, type, seq, t, payload}`) with reconnect and
  resync, so a phone that drops Wi-Fi for 3 s recovers cleanly instead of showing stale
  numbers. Add a visible packet-age / connection indicator — a frozen dashboard that
  looks alive is dangerous.
- Document the gotchas: Windows Firewall inbound rules for UDP 20777 and TCP 8000;
  in-game broadcast mode vs. explicit IP; and **"Your Telemetry: Restricted"** (the
  default) zeroes other players' fuel, ERS and brake bias in multiplayer, so rival fuel
  and ERS modelling is realistically single-player/AI only.

## F. Strategy substance

**F1. v1 is all thresholds and no model, so it cannot answer the only question that
matters: "box this lap or next?"** Add a minimal lap-time model —
`lap_time = base(track, compound) + fuel_effect · fuel_kg + deg(compound, age) + traffic` —
fitted online from your own laps and persisted per track and compound. Undercut/overcut
then becomes an evaluation over candidate pit laps using measured pit loss, rival pace
from Session History, and projected pit-exit traffic.

**F2. Measure pit loss, do not hardcode it.** `m_pitLaneTimeInLaneInMS` and
`m_pitStopTimerInMS` let you compute the real loss the first time you stop at a track, and
store it. Ship the hardcoded table only as a cold-start prior, and fix its keys (A6).

**F3. Linear wear extrapolation to a "cliff lap" is the wrong abstraction.** Wear percentage
is roughly linear but pace loss is not. Fit lap-time degradation from the current stint
with wear as a covariate, and phrase the output in the driver's currency: "about three
more laps at this pace", not "cliff on lap 31".

**F4. Thermal thresholds must be configuration, not constants.** Optimal windows vary by
actual compound, track, and air/track temperature. Put them in YAML keyed by
(compound, track) with defaults, and add a calibration script that fits them from recorded
sessions.

**F5. Quali abort thresholds of 0.5/0.35/0.2 s are arbitrary.** Better: compare the
projected lap time against the time needed to advance (available from the field's Session
History), weighted by remaining ERS and by whether you can afford another set (Tyre Sets).
Aborting costs a set — the engine should know that.

**F6. The clean-air slot finder needs geometry, not just a gap in the classification.**
Project all 24 cars' `m_lapDistance` forward to your pit-exit point over your expected
out-lap, and check the exit window. A 5 s gap in the timing screen is not the same as
clear track when you rejoin.

**F7. Missing calls that are cheap and high value:** fuel delta and lift-and-coast,
ERS/Overtake-mode deployment budget per lap, DRS-in-range warnings, track-limit warning
count before a penalty, blue flags, and red-flag handling.

## G. Process

- **Slice vertically, not by layer.** v1's four steps only produce something usable at the
  very end. Instead: M0 capture/replay, M1 one call end to end, M2 quali, M3 race, M4 model
  and debrief — each milestone independently usable. See `05-roadmap.md`.
- **Tests:** pytest with golden binary fixtures per packet type and format, plus
  replay-driven snapshot tests over the emitted call stream. Ruff and mypy in CI.
- **Observability:** structured JSONL decision log (rule id, inputs, fired or suppressed
  and why) and a post-session HTML debrief. Without it, tuning is vibes.
- **Windows is the target host** if the backend runs on the gaming PC — keep to stdlib
  asyncio (`DatagramProtocol`), avoid uvloop, and test the Windows path.
