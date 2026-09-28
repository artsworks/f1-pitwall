# Driver input: acknowledge and negative

The biggest gap in both earlier plans was that the radio only went one way. A single
control that tells the assistant "got it" or "no" fixes more of the call-policy problem
than any threshold tuning.

## Controls

| Input | Primary | Secondary | Meaning |
|---|---|---|---|
| Single press | Fanatec wheel **button 2** → UDP Action 1 | **Spacebar** | **Acknowledge** — "copy"; **confirm** while the driver menu is open |
| Double press (second press within 350 ms) | button 2 ×2 | Spacebar ×2 | **Negative** — "no / not now"; closes the menu while open |
| Long press (held ≥ 800 ms) | button 2 held | Spacebar held | **Radio silent** on/off (`input.long_press: bookmark` makes it a marker instead) |
| Menu up / down | UDP Action 2 / 3 (stick up / down) | `↑` / `↓` | Open the driver menu, then scroll it (see [Driver menu](#driver-menu-driver--pit-wall)) |
| Dashboard page cycle | UDP Action 4 (stick right) | `P` / click the page pill | race → car → track → race; all clients follow. **Confirm** while the menu is open. It is not a menu item. |
| Mindset toggle | UDP Action 5 (stick left) | `M` / click the mindset pill | balanced ⇄ aggressive (live override), confirmed by voice; also a menu item. **Close** while the menu is open |
| Shortcuts | UDP Action 6 / 7 / 8 (Stream Deck) | — | ask "Pit now?" / "Race stat" / "Fight" directly |
| Menu close | — | `Esc` | close the menu without answering |

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

For a battle: the driver wants to concentrate, not listen. Long press (or the "Radio silent" menu item, or `input.silent_toggle_bit` if bound) toggles radio silent:

- Speech stops; every call still reaches the dashboard's banner and radio log as normal.
- P1 (urgent) calls still speak (`input.silent_keeps_p1`, default on).
- The toggle is confirmed by voice, with rotating variants — on: "Radio silent. Leave you
  to it." / "Copy, I'll leave you to it."; off: "Back with you. Feeding you info again."
- The status bar shows **RADIO SILENT** while it's on; `silent_on` / `silent_off` go in the
  decision log and on the review timeline.
- It stays on until toggled off, including across sessions.

Settings: `input.long_press` (`silent` | `bookmark`), `input.silent_toggle_bit`
(default `0`: a dedicated toggle button; Action 6 is a Stream Deck shortcut by default), `input.silent_on_replies`,
`input.silent_off_replies`.

Unconfirmed on the wheel: recorded sessions so far only contain short taps (≤ 0.25 s), so
whether F1 26 reports a *held* UDP Action as held (down … up after release) is untested.
If a hold doesn't toggle, use the "Radio silent" menu item or set `input.silent_toggle_bit`
to a free UDP Action — no code change needed.

## Mindset and page buttons (M3)

UDP Action 5 (`input.mindset_toggle_bit`, default `0x01000000`; was Action 2 before the
driver menu took it) steps through
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

Either bit set to `0` disables that button. Every `input.*_bit` is checked for
collisions when the config loads: two actions on one bit is a config error.

## Driver menu (driver → pit wall)

The radio is no longer one way only: a short rolling menu lets the driver ask a preset
question or state an opinion. Driving, it has to be three buttons and a glance.

### Remap (M3 + menu)

Twelve UDP Actions exist (`buttonStatus` bits, UDP Action *n* = `0x00100000 << (n-1)`).
Before the menu, four were used: 1 ack/neg/silent, 2 mindset, 3 radio silent, 4 page.
The driver has **five wheel inputs** (Action 1 plus the Fanatec F1 V2.5 left-thumb stick
up/down/right/left) and **three Stream Deck keys** (Actions 6–8). A Stream Deck key sends
a tap only (holding it may not hold the game key), so the deck keys have one meaning each.

| UDP Action | Bit | Setting | Input | Menu closed | Menu open |
|---|---|---|---|---|---|
| 1 | `0x00100000` | `input.udp_action_bit` | wheel button; Spacebar | tap ack · double neg · hold radio silent | tap **confirm** · double **close** · hold radio silent |
| 2 | `0x00200000` | `input.menu_up_bit` | stick up; key `↑` | **open** on the last item | previous item |
| 3 | `0x00400000` | `input.menu_down_bit` | stick down; key `↓` | **open** on the first item | next item |
| 4 | `0x00800000` | `input.page_cycle_bit` + `menu_open_actions.page: confirm` | stick right | next dashboard page | **confirm** |
| 5 | `0x01000000` | `input.mindset_toggle_bit` + `menu_open_actions.mindset: close` | stick left | balanced ⇄ aggressive | **close**, no answer |
| 6 | `0x02000000` | `input.shortcuts: {item: pit}` | Stream Deck tap | asks "Pit now?" | same (closes the menu first) |
| 7 | `0x04000000` | `input.shortcuts: {item: race_stat}` | Stream Deck tap | most relevant race stat | same |
| 8 | `0x08000000` | `input.shortcuts: {item: fight}` | Stream Deck tap | fight + pace briefing | same |
| 9–12 | `0x10000000`–`0x80000000` | — | free | — | — |

- The stick works like a d-pad: up/down scroll, right selects, left backs out. The whole
  menu is one thumb; Action 1 also confirms.
- `Cooldown lap` tells qualifying mode that the driver is deliberately cooling. Select it
  again to return to automatic hot-lap coaching. Automatic pace detection remains active
  when the item is not used.
- Radio silent has no dedicated button: hold Action 1, or pick "Radio silent" in the menu.
  `input.silent_toggle_bit` and `input.menu_close_bit` still exist (default `0`).
- `input.menu_open_actions` (`page`, `mindset`: `confirm` | `close` | `""`) decides what
  those two buttons do while the menu is open; `""` keeps their usual action.
- `input.shortcuts` binds a bit to any menu item id; a shortcut answers that item at once
  (no menu, no scrolling). `pitwall rules check` rejects unknown item ids.
- Action 1 semantics are unchanged while the menu is closed.
- All bits are YAML; `0` disables the binding; a duplicate bit (including shortcuts) fails
  to load.
- Keyboard: `↑` `↓` `Enter` `Esc` on the focused dashboard (`/` or `/radio`); Space still
  mirrors Action 1.

### Behaviour

- **Open:** `Down` opens on the first item, `Up` on the last. The highlighted item's
  name is spoken briefly (`menu.speak_on_scroll`); each new highlight replaces the
  previous prompt, so scrolling fast never builds a queue. Prompts are audio only (not in
  the radio log or decision log, and never repeated by "say again").
- **Scroll:** up/down wrap around (`menu.wrap`).
- **Confirm:** Action 1 single press (or `Enter`) answers the highlighted item and
  closes the menu. The answer is a P1 reply: it bypasses the call budget and radio silent,
  and appears in the radio log.
- **Close:** stick left, `Esc`, or an Action 1 double press closes without answering.
  After `menu.timeout_s` (default 6 s) without a press it closes by itself.
- **Owned by the backend.** The state frame carries `menu: {open, index, items, left_s,
  timeout_s}`; `/` and `/radio` draw the same overlay, fixed below the call banner and
  over the upper-left zone, without reflowing anything.
- **Replayable.** Wheel presses are `BUTN` events and so are in the recording; the menu
  runs inside `Engine.tick` on press timestamps, so a replay makes the same picks and the
  same answers. Keyboard/Stream Deck presses via the dashboard are not in the recording
  (same as the Space route today).

### Items and answers

Items are YAML (`menu.items` in `config/defaults/menu.yaml`, overridable per profile).
Each has `id`, a 2–3 word `label`, a `kind` and `replies`: a map of **case → template
variants**. A deterministic handler (`pitwall.input.menu.ANSWERS`, keyed by `answer`
or `id`) picks the case from the current snapshot and fills the placeholders; variants
rotate per item and case. No language model is involved (ADR 0008). `pitwall rules
check` validates ids, handlers and placeholders.

Items may also carry `show_when` / `rank_when` rule expressions evaluated against the
snapshot when the menu opens: items whose `show_when` fails are hidden, items whose
`rank_when` holds move to the top, and the list stays frozen while the menu is open.
There is no "Next page" item: wheel-right (UDP Action 4) cycles dashboard pages.

| Item | Kind | Cases (from the snapshot) | Example reply |
|---|---|---|---|
| Tyres gone? | question | gone / fading / ok / unknown (`laps_of_pace`, `wear_mean_pct`) | "Fading. About 3 laps of pace left." |
| Pit now? | question | box_now / soon / stay_out / no_stop / unknown (`pit_plan`) | "Not yet. Box in 2, lap 26." |
| Gap ahead? | question | closing / opening / steady / none (`gap_ahead_s`, `gap_trend_ahead_s`) | "1.4 to Norris, closing 0.3 a lap." |
| Gap behind? | question | closing / steady / none | "0.9 to Piastri behind, closing 0.2." |
| Fuel OK? | question | short / tight / ok / spare / unknown (`fuel_margin_laps`) | "Short by 0.4 laps. Lift and coast." |
| Plan? | question | box_now / stop / to_end / unknown | "Box lap 26. Window open soon." |
| Push or save? | question | save_fuel / save_energy / save_tyres / attack / push | "Push. 0.8 to the car ahead." |
| Rain coming? | question | crossover / coming / chance / dry (forecast rain *chance*, `rain_pct_in_10/30`, `weather_crossover`) | "Rain coming. 60 percent chance in ten." |
| Race stat | question | fuel_short / tyres_gone / energy / box_now / pit_soon / position / unknown (first that applies) | "P4, 12 to go. Best lap 1:32.4." |
| Fight | question | both / ahead / behind / none (gap, gap trend, laps to catch, model pace) | "Norris 1.2 ahead, closing 0.3 a lap, catch in 4. Pace 1:32.4 to his 1:32.7. Russell 0.9 behind, pulling away 0.2 a lap." |
| Radio calls | action | default (`budget`) | cycles the P2/P3 calls-per-lap limit through `menu.budget_steps` (4 / 8 / 12 / 20), overriding the mindset's `call_budget_per_lap`: "Copy, up to 20 calls a lap." |
| Understeer | opinion (`balance`) | default / no_bias (`front_brake_bias`) | "Copy, understeer. Bias back one, to 56." |
| Oversteer | opinion (`balance`) | default / no_bias | "Copy, oversteer. Bias forward one, to 58." |
| Mindset | action | — | the usual mindset confirmation ("Copy, aggressive. Pushing.") |
| Radio silent | action | — | the usual silent on/off confirmation |
| Cooldown lap | action | — | marks this lap as a cooldown lap (one lap; auto-detection still runs) |

### Opinions and records

Every confirmed item is a `driver_input` record in the decision log (item, kind, topic,
case, template values, reply text) and a row in the SQLite `driver_inputs` table
(migration 3), so review and debrief can line them up with the calls; the review
timeline shows them as ticks. An opinion with a `topic` also holds for
`menu.opinion_hold_laps` laps (default 5): the balance topic sets `snapshot.driver_balance`
(`"understeer"` / `"oversteer"` / `""`), which rules can read, e.g. to suggest brake bias
or differential changes only once the driver has said the car is out of balance.

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
