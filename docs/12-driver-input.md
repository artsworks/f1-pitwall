# Driver input: acknowledge and negative

The biggest gap in both earlier plans was that the radio only went one way. A single
control that tells the assistant "got it" or "no" fixes more of the call-policy problem
than any threshold tuning.

## Controls

| Input | Primary | Secondary | Meaning |
|---|---|---|---|
| Single press | Fanatec wheel **button 2** | **Spacebar** | **Acknowledge** — "copy" |
| Double press (second press within 350 ms) | button 2 ×2 | Spacebar ×2 | **Negative** — "no / not now" |
| Long press (held ≥ 800 ms) | button 2 held | Spacebar held | **Radio silent** on/off — leave the driver alone in a battle (`input.long_press: bookmark` makes it a silent marker instead) |
| Radio silent toggle | a second wheel button → UDP Action 3 | — | same as long press; fallback if a held button doesn't register |
| Mindset toggle | a wheel button → UDP Action 2 | `M` / click the mindset pill | balanced ⇄ aggressive (live override), confirmed by voice |
| Dashboard page cycle | a wheel button → UDP Action 4 | `P` / click the page pill | race → battle → car → track → setup → race; all clients follow |

Both inputs feed the same press detector, so behaviour is identical whichever is used.
Mid-race nothing requires clicking the dashboard, which would take focus from a
fullscreen game. The dashboard shows the last press ("ACK L24 ▸ box lap 26") so a mis-press is visible.

## How each input reaches the backend

**Fanatec button 2 — through the game's UDP output (preferred).** F1 26 exposes
bindable *UDP Action 1–12* controls; a bound press is reported in the Event packet as
`BUTN` with `buttonStatus` bit flags (UDP Action 1 = `0x00100000` … UDP Action 12 =
`0x80000000`). Bind button 2 to *UDP Action 1* in the game's controls menu.

- No extra driver, HID library or permission; works identically on a second LAN machine.
- The event arrives through the same recorder, so presses are **in the replay** for free.
- `BUTN` is sent on change, so the detector sees press and release edges; timestamps come
  from `m_sessionTime` and receive time.
- Button 2 must not also be bound to a game function, or it will do both. To verify in the
  controls menu: that UDP Action bindings exist for your wheel profile and that button 2 is
  free (if the bindings are not exposed on a wheel profile, fall back below).

Fallback if the binding is unavailable: read the wheel directly as a DirectInput device
through SDL2 with background events enabled. This only works on the game PC and adds a
dependency, so it is second choice.

**Spacebar — global keyboard hook.** The game has focus, so the backend needs a
system-wide low-level keyboard hook (the same mechanism push-to-talk apps use), via a
small Windows-only module. Notes:

- Spacebar must be **unbound in the game**, or the press does both.
- The hook only observes; it never consumes or injects keys.
- Keyboard presses do not appear in UDP, so the backend writes them into the recording
  as synthetic records (a reserved packet id) so replay stays complete.
- Only works on the machine the keyboard is plugged into — the game PC. If the backend
  later moves to another machine, a tiny forwarder on the game PC sends presses over the
  LAN, or the wheel path is used alone.
- Anti-cheat: a passive keyboard hook is standard for voice/overlay tools, but check that
  it is tolerated before relying on it in online races.

## Press detection

```
down ─┬─ held ≥ 800 ms ─────────────▶ BOOKMARK
      └─ up ─▶ wait 350 ms ─┬─ down ─▶ NEGATIVE
                            └─ timeout ▶ ACKNOWLEDGE
```

- Classification runs on **press and release edges**: `BUTN` reports the full button
  bitmask on every change, so release is visible; the keyboard hook gets key-down/up.
- Keyboard **auto-repeat** (a held spacebar sends repeated key-downs) is ignored: only the
  first down after an up counts.
- A lost `BUTN` datagram could turn a double press into a single one. On loopback this is
  rare; the radio log shows the outcome so a misread is visible, and a second press can
  still be added.

- Acknowledge therefore takes 350 ms to register. That's fine — nothing hangs on it.
- Presses closer than 60 ms are treated as contact bounce; three presses count as negative.
- Window length is configurable (`input.double_press_ms`, 250–500).

## Radio silent

For a battle: the driver wants to concentrate, not listen. Long press (or UDP Action 3)
toggles radio silent:

- Speech stops; every call still reaches the dashboard's banner and radio log as normal.
- P1 (urgent) calls still speak (`input.silent_keeps_p1`, default on).
- The toggle is confirmed by voice, with rotating variants — on: "Radio silent. Leave you
  to it." / "Copy, I'll leave you to it."; off: "Back with you. Feeding you info again."
- The status bar shows **RADIO SILENT** while it's on; `silent_on` / `silent_off` go in the
  decision log and on the review timeline.
- It stays on until toggled off, including across sessions.

Settings: `input.long_press` (`silent` | `bookmark`), `input.silent_toggle_bit`
(`0x00400000` = UDP Action 3, `0` disables), `input.silent_on_replies`,
`input.silent_off_replies`.

Unconfirmed on the wheel: recorded sessions so far only contain short taps (≤ 0.25 s), so
whether F1 26 reports a *held* UDP Action as held (down … up after release) is untested.
If a hold doesn't toggle, bind UDP Action 3 instead — no code change needed.

## Mindset and page buttons (M3)

UDP Action 2 (`input.mindset_toggle_bit`, default `0x00200000`) steps through
`input.mindset_cycle` (default `[balanced, aggressive]`). The choice is a live override of
`mindset.active`: it changes rule thresholds at once, is confirmed by voice
(`input.mindset_replies`), is written as a `mindset` record in the decision log, and every
later decision record carries the active mindset.

UDP Action 4 (`input.page_cycle_bit`, default `0x00800000`) steps through `ui.pages`.
The backend owns the current page and sends it in every state frame, so `/` and `/radio`
never diverge. Optional auto-paging (`ui.auto_page`, default off) picks the track page in
formation/SC/VSC and the battle page when a rival is within `ui.auto_page_battle_gap_s`;
a manual choice holds for `ui.auto_page_manual_hold_s` and no auto swap happens within
`ui.auto_page_call_hold_s` of a call.

Either bit set to `0` disables that button. No collision with Action 1 (ack/negative,
`0x00100000`) or Action 3 (radio silent, `0x00400000`).

## What a press applies to

A press targets the **most recent spoken call still inside its response window**
(default 8 s after it finished speaking). With no such call:

- single press → nothing (`input.say_again: true` re-speaks the last call instead);
- double press → quiet for five minutes (P1 still speaks, and calls are not shown).

The packaged response window is 3 s after the call finishes speaking; strategy calls
can set their own `response_window_s`.

| Call type | Acknowledge | Negative |
|---|---|---|
| Informational (P2/P3) | stop repeating; mark it landed | suppress this rule for 3 laps; ×2 cooldown for the session after a second negative |
| Recommendation ("box lap 26") | accepted — the plan assumes it; the dashboard banner changes to PLANNED | rejected — plan the alternative; don't recommend again unless the projection moves by ≥ `pit_gain_min_s` |
| Mindset suggestion | switch mindset | dismiss for 5 laps |
| Priority 1 | acknowledged; stop repeating | acknowledged, stop repeating — a negative never mutes P1 |
| Unacknowledged P1 | repeated once at the next safe moment | — |

## Adaptivity without a language model

This is where the assistant *learns during the race*, deterministically:

- Each rule keeps an acknowledge/negative count for the session. Repeated negatives on
  one rule lengthen its cooldown and drop it a priority step; acknowledgements restore it.
- Across sessions, those counts are stored in SQLite per rule and per track, and feed the
  default cooldowns for next time — "you always dismiss sector-delta calls at Monaco."
- Every press is in the decision log, so review mode shows which calls were accepted,
  rejected or ignored, and the grading data (good / noise) partly writes itself.

All of it is replayable and testable, which an LLM-driven policy would not be.

## Dashboard (second screen)

The dashboard on the second monitor includes the **radio log**: the last ~12 calls in
text, newest on top, each with lap, priority colour plus word, and its outcome (ACK /
NEG / —). That makes a missed or misheard call recoverable at a glance, and it is the
visual counterpart of "say again". A dedicated `/radio` page shows only the log, in large
type, for a narrow second screen.

> **Status note (M2):** the spacebar route currently reaches the backend via the
> dashboard WebSocket (`{"type":"press","down":…}` sent on keydown/keyup while
> the page has focus); the Windows global keyboard hook described above is not
> yet implemented. The wheel path via `BUTN` UDP actions is implemented.
