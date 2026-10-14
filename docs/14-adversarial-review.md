# Adversarial review of the plan (before the first commit)

This review tests each decision against the spec, related documents, and the target setup:
a Windows gaming PC, second monitor, Fanatec wheel, and friends league with three humans
and an AI grid.

## Errors found and fixed

| # | Finding | Severity | Resolution |
|---|---|---|---|
| A1 | **Recording size was wrong by ~30×.** Docs said 25–40 MB per race hour. From the format-2026 packet sizes, menu-rate packets alone are ~6.2 KB per frame: ≈ 0.85 GB/h at 30 Hz, ≈ 1.5 GB/h at 60 Hz. | High | Corrected everywhere; recordings compressed (zstd) at low priority after the session; retention policy defaults sized to GB, not MB. Still well inside the 2 MB/s disk budget (~0.25 MB/s). |
| A2 | **Car Setups was listed at menu rate**; the spec sends it at 2 Hz. | Low | Corrected in ingestion and reference notes; frequencies for Lobby Info, Time Trial and Lap Positions filled in. |
| A3 | **Restricted telemetry was under-stated.** Docs listed fuel, ERS, brake bias and wing damage. The spec also zeroes tyre wear, tyre/brake damage, all engine wear, and the whole Tyre Sets packet. | High for league | Reference list completed. League design assumes friends stay Restricted: their stints are modelled from compound, tyre age and lap-time trend; fuel/energy calls only about AI cars. |
| A4 | **Speech on the second monitor would steal focus.** Web Speech needs a click to arm; clicking a second monitor while the game is exclusive-fullscreen minimises the game. The same applied to the one-tap mindset toggle. | High | Speech moves into the backend (Windows SAPI for the pilot, Piper later). The dashboard is display-only during a session; every mid-race control has a wheel/keyboard binding. Borderless windowed recommended if clicking between sessions. |
| A5 | **"No ducking needed" assumed a phone speaker.** Radio and game now share the headset. | Medium | Separate radio volume and output device setting; short radio click before each call. |
| A6 | **"Box this lap" was priority 1 and also a rejectable recommendation**, while the input doc said a negative never mutes P1. | Medium | Split: *forced* box (fuel, puncture, damage) is P1; strategic box (window, undercut, SC stop) is P2 and can be rejected. |
| A7 | **Parser docs still described multi-format dispatch** (2024/2025/2026) after the F1 26-only decision. | Low | Tables keyed by (`m_packetId`, `m_packetVersion`) for format 2026 only, everywhere. |
| A8 | **Strategy doc used old mindset parameter names** (`pit_gamble_margin_s`, `risk_of_position_loss`). | Low | Renamed to the new vector (`pit_gain_min_s`, `pit_confidence_min`, `position_loss_risk_max`). |
| A9 | **Architecture and performance docs still said "dashboard on the phone"**, contradicting the second-monitor decision. | Medium | Second monitor is primary; the page budget (5 Hz, static text, no charts) is measured in the frame-time A/B. Phone becomes an optional client. |
| A10 | **"No Windows-only dependency"** claim broken by SAPI and the keyboard hook. | Low | Both behind interfaces; the move-to-second-machine escape hatch uses Piper and a small key forwarder. |
| A11 | **Long-press bookmark overlapped the single/double-press detector**, and a held spacebar auto-repeats. | Medium | Detector classifies on press/release edges; auto-repeat ignored; long press ≥ 2 s is a bookmark. |
| A12 | **Roadmap "later" still listed league mode and a Streamdeck ack channel** as future work. | Low | League is the target (first league race in M3); acknowledgement is M2. |
| A13 | **Public repository.** Risks: committing EA's spec, and committing recordings with friends' online IDs. | Medium | Spec is not committed (notes are re-expressed facts); `pitwall trim` redacts participant names; full recordings never in git. |

## Challenged and kept

- **Python on the game PC.** At 30 Hz, ingest is ~230 packets/s (~0.24 MB/s), within
  Python's range. Keep native ingest documented, not planned.
- **10 Hz engine tick.** This is fast enough for strategy. Serve sub-second calls
  (Overtake Mode distance) from the latest state at speak time, not the tick.
- **No LLM.** Acknowledge/negative counts provide deterministic, replay-testable
  adaptivity. Keep this decision.
- **Balanced and aggressive only.** Two tuned vectors are better than four guessed ones.
- **Frame-time A/B as the stutter test.** Include the second-monitor browser, the more
  likely cost. Keep the wider scope.

## Assumptions to verify early (M0/M1)

1. F1 26 menu offers 30 Hz (else use 20 Hz; nothing breaks).
2. Fanatec button 2 can be bound to UDP Action 1, and `BUTN` is emitted in online
   sessions as well as offline.
3. AI cars report `m_yourTelemetry = public` in a league lobby.
4. Spacebar is unbound in your F1 26 control profile, and a passive keyboard hook is
   tolerated by the game's anti-cheat online.
5. Measure real recording size and compression ratio in the first M0 session.
6. SAPI voice quality and start latency are acceptable on your PC.

## Nothing blocking

No finding requires a decision; all were resolved within the existing direction. Verify
the assumptions above in the first recorded session.
