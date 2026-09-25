# Dashboard redesign: the second-monitor race-engineer screen

Status: **proposal, not implemented.** Mockups with static sample data live in
`docs/mockups/` (`dashboard-1080p.html`, `dashboard-m3-stale.html`, `radio.html`). They
are vanilla HTML/CSS, self-contained, no build step — open them in a browser.

This doc supersedes the "Dashboard" section of `04-audio-ui.md` for layout; the
priorities, suppression rules, speech and WebSocket protocol in that doc are unchanged.

## 1. Goals and non-goals

**Goals**

1. The driver glances at the screen for well under a second while driving F1 26. Every
   glance must answer one question: *what did the engineer just say, and is it still
   true?* The **call banner** is therefore the largest thing on screen.
2. The radio log is the second thing: what was said over the last few laps, and whether
   it was actually spoken (or dropped).
3. Connection freshness is load-bearing. LIVE / STALE and packet age are always visible;
   above 1 s of staleness the whole page greys out so a frozen screen never looks alive.
4. The few numbers that *justify* a call are visible without reading: tyres per corner,
   fuel laps and delta, pit window / gaps (M3), damage, lap / position / phase, mindset.
5. Readable at 2 m on a 1080p or 1440p landscape monitor. Dark, high contrast, big type,
   colour always paired with a word.
6. Offline. Vanilla HTML/CSS/JS served by the backend; no CDN, no framework, no build.

**Non-goals**

- Not a telemetry gauge cluster. No speed, RPM, gear, throttle/brake traces, g-forces,
  minimap, sector deltas, per-lap tables. If the game HUD already shows it, we don't.
- Not a control surface during a session. Every mid-race control (ACK / NEG, mindset,
  quiet) has a wheel or keyboard binding (`12-driver-input.md`). The screen only
  *displays* their state.
- Not a debrief tool. Stint plots, deg curves and call grading live in the post-session
  HTML debrief (M4) and review mode (`07-replay-and-debug.md`).
- No animation beyond a single opacity fade on the banner and the stale overlay.
  Nothing moves continuously; a screen that flickers pulls the eye off the track.

## 2. Information hierarchy

Ordered by how often a glance must resolve it. Type sizes follow the order.

| Rank | Element | Why it earns the space | Min size @1080p |
|---|---|---|---|
| 1 | **Call banner** — current/last call, priority colour, age ("12 s ago"), lap | The spoken word is the product; the screen confirms and re-reads it | 64 px text, 120 px tall |
| 2 | **Freshness** — LIVE/STALE, packet age, WS rate | A stale screen must be recognisable instantly | 28 px, top-left corner, always the same place |
| 3 | **Radio log** — last 8–12 calls, lap, priority, spoken ✓ / dropped ✗ / ACK / NEG | "What did it say two laps ago?" | 26 px |
| 4 | **Tyres** — 2×2 car plan view; inner EMA temp, surface, wear %, status word | Justifies thermal / wear calls; the most common M1–M2 call family | 40 px temp |
| 5 | **Fuel** — laps remaining and delta to target | Justifies lift-and-coast / fuel calls | 40 px |
| 6 | **Lap / position / session phase** | Context for every call | 32 px |
| 7 | **Damage** — front wing L/R, rear wing, floor, diffuser, sidepod, gearbox, engine | Justifies "box for a wing"; hidden when all zero | 24 px, list |
| 8 | **Mindset** BALANCED / AGGRESSIVE + verbosity + quiet | One-press control; must be visible to trust the calls | 24 px pill |
| 9 (M3) | **Pit window**, undercut/overcut threats, gaps ahead/behind, stint plan | Race strategy; the tactical block | 32 px |
| — | Brakes, ERS, SC status, latency p99 | Footer, small, dim. Diagnostics, not driving information | 18 px |

## 3. Layout grid

Landscape, 16:9. Fixed 12-column grid with `rem` sizing driven by a root font size set
from viewport width (`font-size: clamp(14px, 0.9vw, 24px)`), so 1080p and 1440p render
the same composition with proportionally larger type. Zones never reflow between
sessions; empty zones keep their box so nothing jumps when a value appears.

```
┌────────────────────────────────────────────────────────────────────────────┐
│ A  STATUS BAR   ● LIVE  age 42 ms  10 Hz   │ RACE · Bahrain  LAP 24/57  P5 │ 48 px
│                 phase: ON TRACK            │ MED  age 14 laps   [BALANCED] │
├────────────────────────────────────────────────────────────────────────────┤
│ B  CALL BANNER                                                             │
│    ▮ P2  "Box this lap, box box. Undercut on Norris."          L24 · 8 s   │ 160 px
├─────────────────────────────────┬──────────────────────────────────────────┤
│ C  TYRES (2×2 plan view)        │ E  RADIO LOG                             │
│    FL 106 HOT   │  FR  98 OK    │   L24 ✓ Box this lap, box box…          │
│    surf 104 · 38% surf 101 · 35%│   L23 ✓ Front left one-oh-four, ease…   │
│    ─────────────┼─────────────  │   L22 ✓ Norris 1.4 ahead, losing…       │
│    RL  94 OK    │  RR  95 OK    │   L21 ✗ Gap behind 2.1 (dropped)        │
│    surf 92 · 22%  surf 93 · 21% │   L20 ✓ ACK  Fuel is good, push.        │  fills
├─────────────────────────────────┤   …                                      │
│ D  FUEL  +0.4 laps   14.2 laps  │                                          │
│    DAMAGE FW L 12% · Floor 4%   │                                          │
├─────────────────────────────────┤                                          │
│ F  STRATEGY (M3; placeholder    │                                          │
│    until then)  PIT WINDOW 26–28│                                          │
│    AHEAD P4 NORRIS +1.4  UC +1.2│                                          │
│    BEHIND P6 LECLERC −2.1  DRS  │                                          │
├─────────────────────────────────┴──────────────────────────────────────────┤
│ G  FOOTER  brakes 412/405/388/390 · ERS 62% · SC 0 · call p99 210 ms · ws 12 ms │ 32 px
└────────────────────────────────────────────────────────────────────────────┘
        ◄────────── 5 cols ──────────►◄──────────── 7 cols ─────────────►
```

Zone rules:

- **A status bar**: freshness lives at the far left, first thing the eye lands on in
  Western reading order. Session/lap/position right-aligned. Mindset pill far right.
- **B banner**: full width, fixed height, never collapses. Empty state shows a dim
  "— radio quiet —" so the zone is still recognisable.
- **C tyres**: a 2×2 grid drawn as the car from above — **FL top-left, FR top-right, RL
  bottom-left, RR bottom-right** — with a thin centre-line so left/right is unambiguous
  at a glance. Compound chip and tyre age sit above the grid. The **whole tile** takes the
  status colour (border + faint background tint, not just the number) so a corner
  heating up is visible in peripheral vision. Brake temp sits small in each tile's
  corner: it is the *cause* of inner-tyre heat and a lock-up warning, not a race-fight
  number, so it never competes with the tyre temp for size.
- **D fuel + damage**: fuel delta is the big number (signed, coloured), laps remaining
  smaller. Damage renders as a list of non-zero components only; the zone header stays.
- **E radio log**: newest at the top (the banner is directly above it, so the top entry
  is the banner's own call and reads as "history continues downward"). 8 rows at 1080p,
  10 at 1440p.
- **F strategy**: reserved for M3. Until then it shows the pit-window placeholder line
  from the roadmap ("pit window: M3") in the dim colour, so the grid does not change
  when strategy lands.
- **G footer**: diagnostics only. Everything here is deliberately small.

### 1440p

Same grid; root font scales ~1.33×. The radio log gains two rows. No other change.

### `/radio` compact view

Log only: status bar (freshness, lap), banner, then the log in ~1.6× type. For a narrow
portrait screen or phone. Same `app.js`, `body.radio` class hides zones C/D/F/G.

## 4. Colour and typography tokens

CSS custom properties in `web/style.css`; every colour is paired with a word in the DOM.

```css
:root {
  --bg: #0a0a0c;        --panel: #121216;   --line: #2a2a30;
  --fg: #ececec;        --dim: #8a8a92;     --faint: #4a4a52;
  --ok: #37e07a;        /* green  — in window, spoken, LIVE */
  --warn: #ffc233;      /* amber  — P2, approaching threshold, delta small */
  --crit: #ff4d4d;      /* red    — P1, HOT, act now, STALE banner */
  --cold: #5cc8ff;      /* blue   — COLD tyres (distinct from red/green for CVD) */
  --info: #b8b8ff;      /* lilac  — P3 informational */
  --aggr: #ff8a3d;      /* orange — AGGRESSIVE mindset pill */
  --font: "Cascadia Mono", "Consolas", "JetBrains Mono", ui-monospace, monospace;
  font-size: clamp(14px, 0.9vw, 24px);   /* 17.3 px @1920, 23 px @2560 */
}
```

Type scale (rem): banner 3.6, tyre temp 2.4, fuel delta 2.4, status bar 1.5, log 1.4,
tile labels 1.0, footer 0.9. Tabular figures (`font-variant-numeric: tabular-nums`) so
numbers do not jitter. Monospace throughout: values line up and the driver learns
positions, not shapes.

Priority colour is used on the **banner left bar and the log row marker**, never on the
whole text — red text on black at 2 m is less legible than white text next to a red bar.

## 5. State → component mapping

All fields are from `state_payload` in `src/pitwall/server/app.py` (WS `state` /
`snapshot` frames) unless noted. `call` / `spoken` / `cancel` frames are per
`04-audio-ui.md`.

| Element | Field(s) | Rendering rule |
|---|---|---|
| LIVE/STALE word | `live`, `packet_age_ms` | `LIVE` green when `live`; `STALE` red otherwise. Client also applies its own >1 s guard against `t` of the last frame (see §6) |
| Packet age | `packet_age_ms` | `NNN ms`; amber ≥ 500 ms, red ≥ 1000 ms |
| WS rate | *derived client-side* from frame arrival (`rate_hz` is `null` today) | `N Hz`, dim |
| Session line | `session_kind`, `session_type`, `track` | `RACE · bahrain` upper-cased; `session_type` numeric is not shown |
| Lap | `lap_num`, `total_laps` | `LAP 24/57`; `total_laps==0` → `LAP 24` |
| Position | `position` | `P5`; `0` → `P--` |
| Phase | `phase` | `garage / out_lap / flying / in_lap / on_track` → upper-case words; `out_lap` amber (thermal calls likely) |
| Compound + age | `tyre_visual`, `tyre_compound`, `tyre_age_laps` | Chip `MED · 14 laps`; visual compound preferred, actual as tooltip |
| Tyre tile temp | `tyres.{fl,fr,rl,rr}.inner` | Integer °C, coloured by `status` |
| Tyre tile status | `tyres.*.status` | `COLD` blue / `OK` green / `HOT` red word under the temp |
| Tyre tile detail | `tyres.*.surface`, `tyres.*.wear` | `surf 104 · wear 38%`; wear amber ≥ 50 %, red ≥ 70 % (thresholds from `/api/config` later) |
| Fuel delta | `fuel_remaining_laps` − laps left (`total_laps − lap_num + 1`) *derived client-side until the server sends `fuel_delta_laps`* | Signed `+0.4 laps`; green ≥ 0, amber −0.5..0, red < −0.5 |
| Fuel laps | `fuel_remaining_laps` | `14.2 laps`, one decimal |
| Damage list | `damage.{front_left_wing, front_right_wing, rear_wing, floor, diffuser, sidepod, gearbox, engine}` (**being added**; percentages 0–100) | Rows for non-zero values only: `FW L 12%`; amber ≥ 10, red ≥ 30. Header reads `DAMAGE none` when all zero |
| Mindset pill | `mindset` | `BALANCED` dim outline; `AGGRESSIVE` orange filled |
| Verbosity / quiet | `verbosity`, `quiet` | Small text right of the pill; `QUIET` in amber when true |
| Banner | latest `call` frame with `priority ≤ 2` (P3 goes to the log only) + `spoken`/`cancel` | See §7 |
| Banner age | client clock − `t` of the `call` frame | `8 s`, updated once per second, not per frame |
| Log rows | `calls[]` from `snapshot`, then `call`/`spoken`/`cancel` frames | `L24 ✓ text`; ✓ once `spoken`, ✗ if `cancel` before spoken (row dims, stays); ACK/NEG chips when `12-driver-input.md` lands |
| Brakes | `brakes.{fl,fr,rl,rr}` | Small `brk 412` in each tyre tile's top-right; faint colour, amber above a brake threshold |
| ERS | `ers_pct` | Footer `ERS 62%` |
| SC | `safety_car` | Footer `SC 0`; non-zero also paints the status bar background amber with the word `SAFETY CAR` / `VSC` |
| Latency | `latency.trigger_to_speak_p99_ms`, `latency.packet_to_ws_p99_ms` | Footer, dim |
| Protocol mismatch | close code 4001 / `v !== 1` | Banner replaced by red `PROTOCOL MISMATCH — RELOAD`; no reconnect |

Fields **not** rendered on purpose: `session_type` (raw enum), `latency` beyond p99.

**Requested payload additions** (for the owner; none are required for phase 1):
`damage` object as above; `fuel_delta_laps` (server owns the target); `rate_hz` populated;
M3: `strategy: {pit_window: [26, 28], ahead: {pos, name, gap_s, compound}, behind: {...},
undercut_s, overcut_s, stint_plan: [...]}`.

## 6. Stale and reconnect behaviour

Two independent staleness sources, either one greys the page:

1. **Server-reported**: `live === false` or `packet_age_ms > 1000` in the latest frame.
   Meaning: the game stopped sending UDP (menu, pause, crash).
2. **Client-observed**: no WS frame for > 1 s (a client `setInterval` checks the last
   frame time). Meaning: the backend or socket is gone. This is the case a
   server-only check can never catch.

Greyed state: `body.stale` sets `filter: grayscale(1) brightness(0.55)` on everything
except the status bar, which turns **red** with `STALE · last packet 4.2 s ago` counting
up. The banner keeps its last text so the driver can still re-read the call, but its
priority colour is removed. Log stays readable. Numbers are not blanked — a 4 s old tyre
temp is still information, as long as it is unmistakably old.

Reconnect: exponential back-off 0.5 → 5 s (as today). On `open` send `hello` with
`last_seq`; the `snapshot` frame replaces `calls[]` wholesale and clears stale. If the
snapshot's `seq` jumped, the log is trusted over local memory (server ring buffer is the
source of truth). A `hello` with a different `config_hash` than last seen shows a one-row
"config reloaded" entry in the log (dim, no banner).

Startup with no frames yet: status bar shows `CONNECTING…` (dim, not red) for the first
3 s, then `STALE`.

## 7. Call banner lifecycle

```
            call frame (P1/P2)          spoken frame            +20 s (P2) / +30 s (P1)
   idle ───────────────────► NEW ───────────────────► SPOKEN ───────────────────► FADED
    ▲                        │  bright bg tint,        │ tint removed,             │ text at --dim,
    │                        │  bold, "speaking…"      │ ✓ + age counter           │ left bar stays coloured
    │   cancel frame ────────┘                         │                           │
    └──────────────── log-only, banner shows "— radio quiet —" if nothing else ◄────┘ (new call at any time → NEW)
```

- **NEW**: banner text swaps on the same frame, left bar and a 15 % background tint in
  the priority colour, right side reads `L24 · speaking…`. No slide-in.
- **SPOKEN**: tint drops to 0 on one 200 ms opacity transition (the only animation),
  `speaking…` becomes `✓ 3 s` and counts up per second.
- **FADED**: after 20 s (P2) or 30 s (P1) without a newer call, text colour drops to
  `--dim`; the coloured left bar remains so "the last thing said was a P1" is still
  visible. Never cleared automatically — the empty state only appears on a fresh
  session or after a `snapshot` with no calls.
- **Cancelled before spoken**: banner reverts to the previous P1/P2 call (or the empty
  state); the log row keeps the text with ✗ and dim colour so revalidation drops are
  auditable.
- **Preemption** (P1 arrives while a P2 is NEW): P1 replaces the banner immediately; the
  P2 stays in the log and receives ✓ or ✗ when the dispatcher reports.
- **P3** never enters the banner. It goes to the log with a lilac marker.
- ACK / NEG (from `12-driver-input.md`, M2): a chip on the banner's right side and on
  the log row; NEG additionally strikes the row through.

## 8. What M2–M4 add, and what stays off the screen

| Milestone | Screen change | Zone |
|---|---|---|
| M2 quali | Phase words `OUT LAP / FLYING / IN LAP`; release-window countdown `release in 12 s` in zone F; abort advisory as a P2 banner; ACK/NEG chips; QUIET state; verbosity name | A, F, B, E |
| M3 race | Zone F fills: `PIT WINDOW L26–28`, `AHEAD P4 NORRIS +1.4 HARD · UC +1.2`, `BEHIND P6 LECLERC −2.1 DRS`, one-line stint plan `M 1–26 → H 27–57`. SC/VSC paints the status bar. `fuel_delta_laps` from server. Damage `box for wing` rows turn red | F, A, D |
| M4 | Nothing new on the live screen. Debrief is a separate page; Piper changes the voice, not the UI. Phone client reuses `/radio` | — |

Reserved but empty until M3: zone F, `~12 %` of the height. It is cheaper to carry an
empty box for two milestones than to reflow the grid the driver has learned.

**Never on this screen**: speed/RPM/gear, throttle/brake/steering traces, sector times,
full lap table, minimap, weather forecast table, all 22 cars, config editor, replay
controls, latency histograms, per-rule cooldown state. Anything the owner wants to *look
at* rather than *glance at* goes to review mode or the debrief.

## 9. Phased implementation plan (small PRs)

Each PR keeps `/` and `/radio` working against the current `state_payload`; no protocol
change until PR 5.

1. **Tokens + grid** — `style.css` rewrite: custom properties, root font clamp, the 12-col
   grid with zones A–G as empty boxes; `index.html` restructured to the zones. No JS
   changes. Verify with mockup screenshots side by side at 1920×1080 and 2560×1440.
2. **Tyres 2×2 + fuel + status bar** — `app.js` render functions per zone; plan-view
   tyre tiles; client-derived fuel delta and WS rate. Unit-testable pure formatters split
   into `web/format.js`.
3. **Banner lifecycle + log** — state machine from §7, age counter, ✓/✗, newest-first
   log, protocol-mismatch state. Add a tiny `web/fixtures/*.json` replay of frames for
   manual testing via a `?fixture=` query param (dev only).
4. **Stale/reconnect** — client-observed staleness timer, `CONNECTING…`, red status bar,
   grayscale filter, config-hash change row.
5. **Damage** — render `damage` object once the server ships it (owner's current work);
   zone D list. Server PR adds the field; this PR only reads it.
6. **`/radio` compact** — `body.radio` rules on the new grid; test on a phone in portrait.
7. **M3 strategy zone** — when `strategy` exists in the payload. Separate PR series.

Definition of done for each: mockup parity screenshot, `ruff`/`mypy` untouched (no
Python), manual check that `pitwall replay --serve` renders the recording.

## 10. Open questions for the owner

1. **Fuel delta target** — compute client-side from `total_laps − lap_num + 1` (wrong in
   quali/practice, where the target is "enough for the run") or wait for the server to
   send `fuel_delta_laps`? Proposal: server field, client falls back to laps remaining
   in race sessions only.
2. **Damage thresholds** — amber 10 % / red 30 % are guesses. Should these come from
   `thresholds` in config like the tyre temps, and should the server emit a
   `status` word per component the way it does for tyres?
3. **Log order** — newest-first (proposed, keeps the top row adjacent to the banner) vs.
   the current newest-last chat style?
4. **Banner fade times** — 20 s / 30 s, or tie to the rule's `deadline_ms`?
5. **Tyre tile colour source** — keep server `status` (COLD/OK/HOT from config) or also
   let wear drive a second colour band? Proposal: temp colours the number, wear colours
   only its own `38%` text.
6. **Brake threshold** — brakes are shown small inside each tyre tile; what temperature
   should turn them amber, and should it come from `thresholds` like the tyre temps?
7. **SC/VSC** — status-bar paint only, or should a `safety_car != 0` also force a
   synthetic banner if the dispatcher has not sent a P1 yet?
8. **Should `rate_hz` be populated** by the server (it is `null` today) or is a client
   derived number acceptable?
9. **1440p only?** If the second monitor is known, the `clamp()` scaling can be dropped
   for fixed pixel sizes.
