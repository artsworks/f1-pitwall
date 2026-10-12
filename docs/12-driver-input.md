# Driver input: acknowledge and negative

Earlier plans missed a way for the driver to answer the radio. One control for "got it" or
"no" fixes more of the call-policy problem than threshold tuning.

## Controls

| Input | Primary | Secondary | Meaning |
|---|---|---|---|
| Single press | Fanatec wheel **button 2** → UDP Action 1 | **Spacebar** | Acknowledge a target or confirm the menu. With no target, add a contextual tap bookmark after quiet-off and say-again checks |
| Double press (second press within 350 ms) | button 2 ×2 | Spacebar ×2 | Negative: "no / not now". Closes the menu while open |
| Long press (held ≥ 800 ms) | button 2 held | Spacebar held | Toggle radio silence. Set `input.long_press: bookmark` to add a contextual hold bookmark |
| Menu up / down | UDP Action 2 / 3 (stick up / down) | `↑` / `↓` | Open the driver menu, then scroll it (see [Driver menu](#driver-menu-driver--pit-wall)) |
| Dashboard page reset | UDP Action 4 (stick right) | `P` / click the page pill | resets to the race layout; all clients follow. **Confirm** while the menu is open. It is not a menu item. |
| Mindset toggle | UDP Action 5 (stick left) | `M` / click the mindset pill | balanced ⇄ aggressive (live override), confirmed by voice; also a menu item. **Close** while the menu is open |
| Shortcuts | UDP Action 6 to 12 (Stream Deck) | — | Ask "Pit now?", "Push or save?", "Fight", "Understeer", "Oversteer", "Radio calls", or "Mindset" directly |
| Menu close | — | `Esc` | close the menu without answering |

Both inputs use the same press detector and behave identically. Mid-race, the driver does
not need to click the dashboard and take focus from the fullscreen game. The dashboard
shows the last press ("ACK L24 ▸ box lap 26") to expose mis-presses.

A tap with no target records the current scalar snapshot as a bookmark. When
`input.spoken_replies` is on, the backend replies "Marked." A negative press with no
target still quiets the radio for `input.quiet_minutes`.

## How each input reaches the backend

**Fanatec button 2, through the game's UDP output (preferred).** F1 26 exposes bindable
*UDP Action 1–12* controls. A bound press appears in the Event packet as `BUTN` with
`buttonStatus` bit flags (UDP Action 1 = `0x00100000` … UDP Action 12 = `0x80000000`).
Bind button 2 to *UDP Action 1* in the game's controls menu.

- No extra driver, HID library, or permission is needed. It works on a second LAN machine.
- The same recorder captures the event, so presses are **in the replay** for free.
- `BUTN` is sent on change, so the detector sees press and release edges. Timestamps use
  `m_sessionTime` and receive time.
- Do not bind button 2 to a game function, or it will do both. In the controls menu,
  verify that UDP Action bindings exist for your wheel profile and button 2 is free. If
  the profile does not expose those bindings, use the fallback below.

If the binding is unavailable, read the wheel as a DirectInput device through SDL2 with
background events enabled. This fallback works only on the game PC and adds a dependency.

**Spacebar, through a global keyboard hook.** The game has focus, so the backend needs a
system-wide low-level keyboard hook, the same mechanism used by push-to-talk apps. Use a
small Windows-only module.

- Leave Spacebar **unbound in the game**, or the press does both.
- The hook only observes; it never consumes or injects keys.
- Keyboard presses do not appear in UDP. The backend writes them to the recording as
  synthetic records (a reserved packet id) so replay stays complete.
- This works only on the keyboard's machine, the game PC. If the backend moves elsewhere,
  a forwarder on the game PC can send presses over the LAN, or use only the wheel path.
- Anti-cheat: passive keyboard hooks are standard for voice/overlay tools. Check that
  anti-cheat tolerates the hook before using it in online races.

## Press detection

```
down ─┬─ held ≥ 800 ms ─────────────▶ BOOKMARK
      └─ up ─▶ wait 350 ms ─┬─ down ─▶ NEGATIVE
                            └─ timeout ▶ ACKNOWLEDGE
```

- Classify **press and release edges**. `BUTN` reports the full button bitmask on every
  change, so releases are visible. The keyboard hook reports key-down and key-up.
- Ignore keyboard **auto-repeat** (repeated key-downs while Spacebar is held). Count only
  the first down after an up.
- A lost `BUTN` datagram could make a double press appear single. This is rare on loopback.
  The radio log shows the outcome, so a misread is visible and the driver can press again.

- Acknowledge takes 350 ms to register. Nothing hangs on it.
- Presses closer than 60 ms are treated as contact bounce; three presses count as negative.
- Window length is configurable (`input.double_press_ms`, 250–500).

## Radio silent

Long press toggles radio silence by default. Set `input.silent_toggle_bit` to use another
control.

A double press is always negative. It never toggles radio silence.

- Speech stops, but every call still reaches the dashboard banner and radio log.
- P1 (urgent) calls still speak (`input.silent_keeps_p1`, default on).
- Confirm the toggle by voice with rotating variants. On: "Radio silent. Leave you
  to it." / "Copy, I'll leave you to it." Off: "Back with you. Feeding you info again."
- The status bar shows **RADIO SILENT** while it's on; `silent_on` / `silent_off` go in the
  decision log and on the review timeline.
- Radio silence stays on until toggled off, including across sessions.

Settings: `input.long_press` (`silent` | `bookmark`, default `silent` in `settings.yaml`),
`input.silent_toggle_bit`
(default `0`: a dedicated toggle button), `input.silent_on_replies`,
`input.silent_off_replies`.

The repo owner recorded a hold of about 1.1 s on UDP Action 1. The wheel reported down
then up, and radio silent toggled.

## Mindset and page buttons (M3)

UDP Action 5 (`input.mindset_toggle_bit`, default `0x01000000`; previously Action 2 before
the driver menu took it) cycles through `input.mindset_cycle` (default
`[balanced, aggressive]`). The choice overrides `mindset.active` live and changes rule
thresholds at once. Voice confirms the choice (`input.mindset_replies`). The decision log
stores a `mindset` record, and each later decision record carries the active mindset.

UDP Action 4 (`input.page_cycle_bit`, default `0x00800000`) resets the dashboard page to
the first entry in `ui.pages`, the race layout. A `page` client message or menu item with
a page name still selects that page directly. The backend owns the current page and sends
it in every state frame, so `/` and `/radio` stay in sync. Optional auto-paging
(`ui.auto_page`, default off) selects the track page in formation/SC/VSC and the battle
page when a rival is within `ui.auto_page_battle_gap_s`. Auto-paging does not swap pages
within `ui.auto_page_call_hold_s` of a call.

Set either bit to `0` to disable its button. When config loads, it checks every
`input.*_bit` for collisions. Two actions on one bit cause a config error.

## Driver menu (driver → pit wall)

The rolling menu lets the driver ask a preset question or state an opinion. While driving,
it must use three buttons and a glance.

### Remap (M3 + menu)

Twelve UDP Actions exist (`buttonStatus` bits, UDP Action *n* = `0x00100000 << (n-1)`).
Action 1 handles ack, negative, bookmarks, and radio silence. Actions 2 and 3 scroll the
menu, Action 4 resets the page, and Action 5 toggles mindset.

Use the wheel button for Action 1 and the Fanatec F1 V2.5 thumb stick for Actions 2 to 5.
The driver can also use seven Stream Deck keys for Actions 6 to 12. Each key sends a tap
only. Holding a key may not hold the game key, so assign one action to each key.

| UDP Action | Bit | Setting | Input | Menu closed | Menu open |
|---|---|---|---|---|---|
| 1 | `0x00100000` | `input.udp_action_bit` | wheel button; Spacebar | tap ack or bookmark · double neg · hold radio silent | tap **confirm** · double **close** · hold radio silent |
| 2 | `0x00200000` | `input.menu_up_bit` | stick up; key `↑` | **open** on the last item | previous item |
| 3 | `0x00400000` | `input.menu_down_bit` | stick down; key `↓` | **open** on the first item | next item |
| 4 | `0x00800000` | `input.page_cycle_bit` + `menu_open_actions.page: confirm` | stick right | reset to the race layout | **confirm** |
| 5 | `0x01000000` | `input.mindset_toggle_bit` + `menu_open_actions.mindset: close` | stick left | balanced ⇄ aggressive | **close**, no answer |
| 6 | `0x02000000` | `input.shortcuts: {item: pit}` | Stream Deck tap | asks "Pit now?" | same, closes the menu first |
| 7 | `0x04000000` | `input.shortcuts: {item: push}` | Stream Deck tap | asks "Push or save?" | same, closes the menu first |
| 8 | `0x08000000` | `input.shortcuts: {item: fight}` | Stream Deck tap | asks "Fight" | same, closes the menu first |
| 9 | `0x10000000` | `input.shortcuts: {item: understeer}` | Stream Deck tap | records understeer | same, closes the menu first |
| 10 | `0x20000000` | `input.shortcuts: {item: oversteer}` | Stream Deck tap | records oversteer | same, closes the menu first |
| 11 | `0x40000000` | `input.shortcuts: {item: budget}` | Stream Deck tap | cycles radio calls per lap | same, closes the menu first |
| 12 | `0x80000000` | `input.shortcuts: {item: mindset}` | Stream Deck tap | changes mindset | same, closes the menu first |

- The stick works like a d-pad: up/down scroll, right selects, and left backs out. The
  menu uses one thumb; Action 1 also confirms.
- Hold Action 1 to toggle radio silence. Set `input.silent_toggle_bit` to an unused UDP
  Action for a separate toggle. `input.silent_toggle_bit` and `input.menu_close_bit` still
  exist (default `0`).
- `input.menu_open_actions` (`page`, `mindset`: `confirm` | `close` | `""`) sets what
  those buttons do while the menu is open. `""` keeps their usual action.
- `input.shortcuts` binds a bit to any menu item id. A shortcut answers the item without
  opening or scrolling the menu. Shortcut-only items stay out of the scroll list.
  `pitwall rules check` rejects unknown item ids and warns if a shortcut-only item lacks a
  binding.
- A target call can receive a press grade: ACK means good and NEG means noise. Human
  grades can replace these grades.
- Configure all bits in YAML. `0` disables a binding. A duplicate bit, including a
  shortcut, fails to load.
- Use `↑` `↓` `Enter` or `Esc` on the focused dashboard (`/` or `/radio`). Space still
  mirrors Action 1.

### Behaviour

- **Open:** `Down` opens on the first item; `Up` opens on the last. The highlighted
  item's name is spoken briefly (`menu.speak_on_scroll`). Each new highlight replaces
  the previous prompt, so fast scrolling does not build a queue. Prompts are audio only.
  They do not enter either log or repeat with "say again".
- **Scroll:** up/down wrap around (`menu.wrap`).
- **Confirm:** A single Action 1 press or `Enter` answers the highlighted item and closes
  the menu. The P1 reply bypasses the call budget and radio silence, and appears in the
  radio log.
- **Close:** Stick left, `Esc`, or an Action 1 double press closes the menu without
  answering. It also closes after `menu.timeout_s` (default 6 s) without a press.
- **Owned by the backend.** The state frame carries `menu: {open, index, items, left_s,
  timeout_s}`. `/` and `/radio` draw the same overlay below the call banner and over the
  upper-left zone, without reflowing anything.
- **Replayable.** Wheel presses are recorded as `BUTN` events. The menu runs inside
  `Engine.tick` using press timestamps, so replay makes the same picks and answers.
  Keyboard/Stream Deck presses via the dashboard are not recorded, like the current
  Space route.

### Items and answers

Items are YAML (`menu.items` in `config/defaults/menu.yaml`, overridable per profile).
Each has an `id`, a 2–3 word `label`, a `kind`, and `replies`, a map of **case → template
variants**. A deterministic handler (`pitwall.input.menu.ANSWERS`, keyed by `answer` or
`id`) selects the case from the current snapshot and fills placeholders. Variants rotate
per item and case. No language model is involved (ADR 0008). `pitwall rules check`
validates ids, handlers, and placeholders.

The scroll list order is Tyres gone?, Pit now?, Fight, Rain coming?, and Push or save?.
Radio silent is shortcut-only. Items with `shortcut_only: true` stay out of the list but
remain available through a shortcut or voice intent with the same item id.

Items may also carry snapshot-based `show_when` / `rank_when` expressions. `show_when`
hides an item; `rank_when` moves it to the top. The list stays frozen while the menu is
open. Wheel-right (UDP Action 4) resets the page to the race layout.

| Item | Kind | Cases (from the snapshot) | Example reply |
|---|---|---|---|
| Tyres gone? | question | switch / gone / fading / ok / unknown | "Fading. Worst one 55 percent, 3 laps left." |
| Pit now? | question | box_now / soon / stay_out / no_stop / unknown | "Stay out, box lap 26. Window 24 to 28." |
| Fight | question | race: both / ahead / behind / none; other sessions: times / no_time | "P4, 12 to go. Clear air." |
| Rain coming? | question | switch / right_tyre / crossover / coming / chance / dry | "Rain coming. 60 percent in ten." |
| Push or save? | question | save_fuel / save_energy / save_tyres / fuel_tight / attack / push | "Push, fuel's tight. Plus 0.2 laps." |
| Radio silent | shortcut-only action | — | Toggles radio silence |
| Understeer | shortcut-only opinion (`balance`) | default / no_bias (`front_brake_bias`) | "Copy, understeer. Bias back one, to 56." |
| Oversteer | shortcut-only opinion (`balance`) | default / no_bias | "Copy, oversteer. Bias forward one, to 58." |
| Radio calls | shortcut-only action | default (`budget`) | Cycles the P2/P3 calls-per-lap limit through `menu.budget_steps` |
| Mindset | shortcut-only action | — | Changes mindset and confirms the choice |

### Opinions and records

Every confirmed item creates a `driver_input` record in the decision log (item, kind,
topic, case, template values, reply text) and a row in the SQLite `driver_inputs` table
(migration 3). Review and debrief can align these records with calls; the review timeline
shows them as ticks. An opinion with a `topic` holds for `menu.opinion_hold_laps` laps
(default 5). The balance topic sets `snapshot.driver_balance` (`"understeer"` /
`"oversteer"` / `""`). Rules can use it to suggest brake bias or differential changes
after the driver reports that the car is out of balance.

## What a press applies to

A press targets the **most recent spoken call still inside its response window**, which
defaults to 8 s after the call finishes. With no such call:

- single press → nothing (`input.say_again: true` re-speaks the last call instead);
- double press → quiet for five minutes (P1 still speaks, and calls are not shown).

The packaged response window is 3 s after the call finishes. Strategy calls can set their
own `response_window_s`.

| Call type | Acknowledge | Negative |
|---|---|---|
| Informational (P2/P3) | stop repeating; mark it landed | suppress this rule for 3 laps; ×2 cooldown for the session after a second negative |
| Recommendation ("box lap 26") | accepted — the plan assumes it; the dashboard banner changes to PLANNED | rejected — plan the alternative; don't recommend again unless the projection moves by ≥ `pit_gain_min_s` |
| Mindset suggestion | switch mindset | dismiss for 5 laps |
| Priority 1 | acknowledged; stop repeating | acknowledged, stop repeating — a negative never mutes P1 |
| Unacknowledged P1 | repeated once at the next safe moment | — |

## Adaptivity without a language model

The assistant *learns during the race* through deterministic updates:

- Each rule keeps an acknowledge/negative count for the session. Repeated negatives
  lengthen its cooldown and drop it one priority step; acknowledgements restore it.
- Across sessions, SQLite stores counts per rule and track. The counts set future default
  cooldowns: "you always dismiss sector-delta calls at Monaco."
- The decision log stores every press, so review mode shows accepted, rejected, and
  ignored calls. Presses also supply some grading data (good / noise).

All of it is replayable and testable, which an LLM-driven policy would not be.

## Dashboard (second screen)

The second-monitor dashboard includes the **radio log**: the last ~12 calls as text,
newest on top, with lap, priority colour and word, and outcome (ACK, NEG, or no outcome).
This makes missed or misheard calls recoverable at a glance and provides the visual
counterpart of "say again". A dedicated `/radio` page shows only the log in large type
for a narrow second screen.

> **Status note (M2):** the spacebar route currently reaches the backend via the
> dashboard WebSocket (`{"type":"press","down":…}` sent on keydown/keyup while
> the page has focus); the Windows global keyboard hook described above is not
> yet implemented. The wheel path via `BUTN` UDP actions is implemented.
