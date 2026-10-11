# Angles neither plan has covered yet

These gaps are absent from plan v1 and the refined documents. They are ordered by impact
and include recommendations. Most are not for M1, but identify them before other choices
rule them out.

## High value, cheap, should be scheduled

**1. An acknowledgement channel from the driver.**
Communication is one-way, which makes spam control elaborate. A button on the wheel,
Streamdeck, or a large phone-screen area can send *copy* / *say again* / *quiet for five
minutes*.
Knowing whether a call was heard lets the engine speak more freely. This is cheap to
build and has a large effect. **Resolved:** Fanatec button 2 (UDP Action 1) and spacebar,
M2 (`12-driver-input.md`).

**2. 2026 energy management deserves first-class modelling, not a rule.**
Under the 2026 regulations, the electrical side is roughly half the power unit. It has
per-lap harvest limits and Overtake Mode as a distance-gated resource
(`m_overtakeAvailable`, `m_overtakeActivationDistance`). "Where do I spend my energy on
this lap?" is a per-track optimisation problem: which straights, and under what
circumstances. It may be the highest-value new call an F1 26 pit wall can make compared
with F1 23. Model it alongside tyre behaviour, with a per-track deployment map learned
from your laps. Recommend designing it in M3 and learning in M4.

**3. Self-evaluation: is the assistant actually helping?**
The assistant may feel useful while distraction costs two tenths. Add two low-cost
measures: the *grade this call* control in review mode (`07-replay-and-debug.md`), and a
coarse A/B comparison of lap-time distributions and mistake rates with calls on and off
over a few sessions. Without these, there is no honest answer to "is it good?".
Recommend grading in M2 and A/B analysis in M4.

**4. A `doctor` / onboarding path.**
The most likely reason this never gets used is a firewall dialog at 9 pm. A one-command
diagnostic should bind the port, check the rule, report the observed format and rate, and
name the wrong in-game setting. Add a tray QR code that opens the dashboard on a phone or
tablet without typing an IP. Recommend M1.

**5. Crash resilience mid-race.**
If the backend dies on lap 30 of a 50-lap race, the outcome is undefined. It should restart
under a watchdog, reload session state from SQLite and the recording's last minutes, then
rejoin with a spoken "back with you". The recorder should be the last process to die and
the first to start. Recommend M3, when races are long enough to matter. **Resolved:** M3.
`pitwall start` runs the recorder and watchdog. The watchdog restarts the engine child
process if it crashes or hangs (`18-race-engine.md`, Recovery).

## Real, but later

**6. A spotter is a different product from an engineer, and you may want both.**
Proximity awareness ("car alongside, left"), blue flags, and incidents ahead come from
Motion / Lap Positions. These are *reactive*, sub-second, and high-frequency, while
strategy is deliberative. They need a separate latency budget and voice. Keep the spotter
separate so strategy calls cannot delay it.

**7. Practice programmes and weekend continuity.**
The game's practice programmes (fuel run, tyre-management run, qualifying sim) are
precisely the data the model needs. A weekend entity linking P1/P2/P3/Q/R at one track lets a practice
long-run seed the race degradation prior. Real teams do this, and persistence makes it
nearly free.

**8. Career / season state.**
Engine-component allocation, grid penalties, weekend tyre allocation, and championship
position affect the right call. They answer "do I need to win this race or finish it".
Most are absent from telemetry, so configure them. This takes little effort and improves
race-level advice.

**9. Setup advice.**
Car Setups (5) is read-only. Stored history could correlate setup parameters with tyre
temperatures, wear, and pace at a track, then suggest changes between sessions. This is a
second product, so keep the schema open to it.

**10. Voice input.**
"How's the gap?", "what's my fuel?", "quiet". Web Speech recognition on the phone is the
easy option. A local Vosk/whisper.cpp model works offline. Pair voice input with (1).

**11. More than one client at once.**
Phone, second monitor, and a spectating friend are plausible. Each client needs its own
mute and layout. Design WebSocket fan-out now, then implement it later.

**12. Stream/OBS output.**
Consider a browser-source dashboard overlay and a Discord bot that posts the debrief to a
league channel. League is now the target (`13-league-multiplayer.md`). Add the Discord
debrief after M4. No streaming is planned.

## Risks and constraints not yet written down

**13. League and competition rules.**
The UDP feed is an official, documented game feature, so it raises no game-integrity issue
in single-player. Some leagues restrict external coaching or driving aids. Check the rule
before competitive races. Verbosity presets make a "league-legal mode" (facts only, no
coaching) trivial to add.

**14. Spec drift is a *when*, not an *if*.**
A mid-season patch that bumps `m_packetVersion` will break parsing. The plan handles this
with size assertions and version dispatch, while keeping recordings valid. Document the
operational response too: a canary that alerts on unknown header triples, and a rule never
to discard recordings. Re-parse an old recording to verify a fix.

**15. Data growth and hygiene.**
Recordings use ~0.85 GB/hour raw (30 Hz), and the database will grow. Set a retention
policy and an export path for learned track parameters so they survive a reinstall. If
multiplayer enters scope, decide whether to store other players' names.

**16. Database migrations.**
The schema will change every milestone. Pick a migration approach at M2, before data is
worth keeping, rather than at M4.

**17. Clock discipline between phone and backend.**
With speech in the backend, this is in-process for the pilot. A phone client needs a
clock-offset handshake to measure latency accurately. Skipping it would invalidate a
stated goal.

**18. Security posture on the LAN.**
The dashboard has no authentication by default, and the backend binds a port on the game
machine. Bind only to the LAN interface, put a token in the QR-code URL, and never expose
the dashboard beyond the LAN. These steps avoid a later choice between a five-minute
change and one that never happens.

**19. Accessibility of the display.**
Colour carries meaning in the mock-up. Pair each colour with a word, as the refined UI
doc requires, and use a colour-blind-safe palette. Make visual-only mode a configuration
option rather than relying on "turn the volume down". This mode also suits streaming.

**20. The LLM question, answered once.**
_Superseded in part by ADR 0009 (`adr/0009-llm-chooser-with-rule-veto.md`): a model may
now choose among rule-generated candidates once it passes the promotion gate. Phrasing
stays deterministic._

Use a language model for *phrasing*, to turn a deterministic decision into natural
speech, or for *post-race Q&A over the stored database*. It is not suitable for decisions
because of latency, non-determinism, and the lack of replay tests. Record this boundary
before someone adds a model casually.

**Decision for the pilot: no LLM, no API key.** Decisions, phrasing (templates with
variants), adaptivity (acknowledge/negative counts, `12-driver-input.md`) and the
debrief are all deterministic. An LLM would add 300 ms–2 s of latency, a network
dependency mid-race, per-token cost and untestable behaviour, for the marginal gain of
more varied wording. Revisit only for post-race Q&A over SQLite, behind an optional key.

**21. LLM-assisted debrief.**
This is the agreed shape of the "post-race Q&A" from (20). After M4 builds the
deterministic HTML debrief (`05-roadmap.md`, M4), add an *optional* LLM pass over its
artifacts. These include stint-plot data, deg curves, the decision log with each call's
inputs and verdict, `LapSummary` rows, the 10 Hz SQLite downsample, and the `.f1bin`
recording index. The model produces natural-language coaching analysis and freeform Q&A
("why did I lose time in S2 all weekend?", "was the lap-30 call correct?"). Add setup
correlation (9) as an input once available.

This can wait and is safe to build. It runs offline after the session, with no live-latency
constraint or replay-determinism requirement. Follow the ADR 0008 boundary
(`adr/0008-natural-phrasing-without-an-llm.md`). The model may phrase or analyse, but
never decide. Deterministic decisions remain replay-testable. The LLM provides advisory
prose about them.

This depends on the M4 debrief artifacts, the SQLite history schema, and M3's recorded
sessions. The LLM adds a SQLite consumer, so migrations (16) and data growth (15) also
apply.

Open points to settle before building:
- Cloud or local model. League sessions contain other players' names and pace, so the
  privacy questions in (13) and (15) determine what may leave the machine.
- Model tier. Prefer a fast, cheap tier over frontier models: Gemini Flash, OpenAI's
  mini/nano class, DeepSeek-V3, or Zhipu GLM-Flash. Inputs are pre-computed structured
  stats; the task is phrasing and correlation, not reasoning from raw data. Put each
  provider behind a small interface so the model is a config line.
- Per-token cost and an optional API key in the layered config (`08-configuration.md`).
  The key is absent by default.
- Surface: embed the analysis in the HTML debrief or use a separate debrief chat.
- Grounding: use structured stats and the decision log, never raw packets, to limit
  hallucination risk and context size.
- Mark LLM-generated text so readers can distinguish it from the reviewed deterministic
  analysis. The debrief page design (`16-debrief-design.md`, §6) proposes one badged,
  hideable section. Anchor every claim to a deterministic section and leave the LLM
  section absent by default.
