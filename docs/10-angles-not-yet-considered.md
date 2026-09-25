# Angles neither plan has covered yet

You asked what is missing. These are the things that are not in plan v1, not in the
refined documents, and that I think are real — ordered by how much I think they matter,
with a recommendation on each. Most are not for M1; the point is to know they exist
before choices foreclose them.

## High value, cheap, should be scheduled

**1. An acknowledgement channel from the driver.**
Right now communication is one-way, which is why spam control has to be so elaborate. One
button — wheel, Streamdeck, or a big area of the phone screen — carrying *copy* / *say
again* / *quiet for five minutes* changes the call policy fundamentally: the engine can
speak more freely when it knows whether it was heard. Cheap to build, disproportionate
effect. **Resolved:** Fanatec button 2 (UDP Action 1) and spacebar, M2 (`12-driver-input.md`).

**2. 2026 energy management deserves first-class modelling, not a rule.**
Under the 2026 regulations the electrical side is roughly half the power unit, with
per-lap harvest limits and Overtake Mode as a discrete, distance-gated resource
(`m_overtakeAvailable`, `m_overtakeActivationDistance`). "Where do I spend my energy on
this lap?" is a per-track optimisation problem — which straights, under which
circumstances — and it is arguably the highest-value new thing an F1 26 pit wall can do
that an F1 23 one could not. Treat it as a sibling of the tyre model, with a per-track
deployment map learned from your own laps. Recommend: design in M3, learn in M4.

**3. Self-evaluation: is the assistant actually helping?**
The failure mode of this class of tool is that it feels great and costs you two tenths of
distraction. Two mechanisms, both cheap: the *grade this call* control in review mode
(`07-replay-and-debug.md`), and a coarse A/B — compare your lap-time distribution and
mistake rate with calls on versus calls off over a few sessions. Without this there is no
honest answer to "is it good?". Recommend: grading in M2, A/B analysis in M4.

**4. A `doctor` / onboarding path.**
The most likely reason this never gets used is a firewall dialog at 9 pm. A one-command
diagnostic that binds the port, checks the rule, reports the observed format and rate,
and names the wrong in-game setting is a small feature with a large effect on whether the
thing survives contact with a Tuesday evening. Plus a QR code in the tray to open the
dashboard on a phone or tablet without typing an IP. Recommend: M1.

**5. Crash resilience mid-race.**
If the backend dies on lap 30 of a 50-lap race, what happens? Currently: undefined. It
should restart under a watchdog, reload session state from SQLite plus the last minutes
of the recording, and rejoin with a spoken "back with you". The recorder should be the
last thing to die and the first to start. Recommend: M3, when races get long enough to
care.

## Real, but later

**6. A spotter is a different product from an engineer, and you may want both.**
Proximity awareness ("car alongside, left"), blue flags, incidents ahead — these come
from Motion / Lap Positions and are *reactive*, sub-second, and high-frequency, whereas
strategy is deliberative. They need a different latency budget and a different voice.
Worth keeping architecturally separate so the spotter can never be queued behind a
strategy call.

**7. Practice programmes and weekend continuity.**
The game's own practice programmes (fuel run, tyre-management run, qualifying sim) are
exactly the data the model wants. A weekend entity linking P1/P2/P3/Q/R at one track lets
a practice long-run seed the race degradation prior — which is how real teams do it, and
it is nearly free once persistence exists.

**8. Career / season state.**
Engine-component allocation, grid penalties, tyre allocation across a weekend, and
championship position all change what the right call is (there is a correct answer to
"do I need to win this race or finish it"). Mostly not in telemetry, so it would be
configured — small effort, large effect on the quality of race-level advice.

**9. Setup advice.**
Car Setups (5) is read-only, but with stored history you can correlate setup parameters
against tyre temperatures, wear and pace at a track and suggest changes between sessions.
This is a whole second product; noting it so the schema does not accidentally exclude it.

**10. Voice input.**
"How's the gap?", "what's my fuel?", "quiet". Web Speech recognition on the phone is the
easy path, a local Vosk/whisper.cpp model the offline one. Pairs naturally with (1).

**11. More than one client at once.**
Phone plus second monitor plus a friend spectating is a plausible setup; per-client mute
and per-client layout follow. Design the WebSocket fan-out for it now (cheap), implement
later.

**12. Stream/OBS output.**
A browser-source overlay variant of the dashboard, and a Discord bot posting the debrief
to a league channel. League is now the target (`13-league-multiplayer.md`); the Discord debrief is a natural
after-M4 addition. No streaming planned.

## Risks and constraints not yet written down

**13. League and competition rules.**
The UDP feed is an official, documented game feature, so there is no game-integrity issue
in single-player. But some leagues restrict external coaching or driving aids. If you
race competitively, that rule should be checked once, and a "league-legal mode" (facts
only, no coaching) is trivial to add given the verbosity presets.

**14. Spec drift is a *when*, not an *if*.**
A mid-season patch that bumps `m_packetVersion` will break parsing. The plan handles it
(size assertions, version dispatch, recordings stay valid), but the *operational* answer
should also be written: a canary that alerts on unknown header triples, and a rule that
recordings are never discarded — a re-parse of an old recording is how you verify the
fix.

**15. Data growth and hygiene.**
Recordings at ~0.85 GB/hour raw (30 Hz) plus a growing database means a retention policy, an export
path for learned track parameters (so they survive a reinstall), and — if multiplayer is
ever in scope — a decision about storing other players' names.

**16. Database migrations.**
The schema will change every milestone. Pick a migration approach at M2, before there is
data worth keeping, not at M4 when there is.

**17. Clock discipline between phone and backend.**
With speech in the backend this is in-process for the pilot. A phone client would need
a clock-offset handshake, otherwise its latency number is fiction. Small, but it invalidates a stated goal if skipped.

**18. Security posture on the LAN.**
The dashboard has no authentication by default and the backend binds a port on a machine
that is also gaming. Bind to the LAN interface only, put a token in the QR-code URL, and
never expose it beyond the LAN. Not paranoia — just the difference between a five-minute
and a never decision later.

**19. Accessibility of the display.**
Colour is currently carrying meaning in the mock-up. Pair every colour with a word (the
refined UI doc already requires this), ship a colour-blind-safe palette, and make the
visual-only mode a first-class configuration rather than "turn the volume down" — it is
also the right mode for streaming.

**20. The LLM question, answered once.**
There will be a temptation to put a language model in the loop. The defensible place for
one is *phrasing* — turning a deterministic decision into natural speech — and
*post-race Q&A over the stored database*. It is not a good fit for the decision itself:
latency, non-determinism, and the impossibility of a replay test. Worth writing down as a
boundary now, because it is the kind of thing that gets added casually.

**Decision for the pilot: no LLM, no API key.** Decisions, phrasing (templates with
variants), adaptivity (acknowledge/negative counts, `12-driver-input.md`) and the
debrief are all deterministic. An LLM would add 300 ms–2 s of latency, a network
dependency mid-race, per-token cost and untestable behaviour, for the marginal gain of
more varied wording. Revisit only for post-race Q&A over SQLite, behind an optional key.
