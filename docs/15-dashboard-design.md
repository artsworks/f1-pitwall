# Dashboard redesign: the second-monitor race-engineer screen

Status: **proposal, not implemented.** Mockups with static sample data live in
`docs/mockups/` (`dashboard-1080p.html`, `dashboard-m3-stale.html`, `radio.html`). They
are vanilla HTML/CSS, self-contained, no build step — open them in a browser.
Their call evidence, fuel target and damage values illustrate planned backend fields.

This doc supersedes the "Dashboard" section of `04-audio-ui.md` for layout. The
current runtime is unchanged; future call-history additions are identified below.

## 1. Goals and non-goals

**Goals**

1. The driver glances at the screen for well under a second while driving F1 26. Every
   glance must answer one question: *what is the engineer saying, and can I act on it?*
   The **call banner** is therefore the largest thing on screen.
2. A small previous-call subtitle provides continuity; the radio log holds older calls
   and distinguishes dispatched, audio started, interrupted and dropped.
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
- No continuous animation. A new call makes a brief, one-time transition into the
  fixed banner while the old call settles into a smaller previous-call line. Urgent
  calls and stale/error states appear immediately; reduced-motion users get no motion.

## 2. Information hierarchy

Ordered by how often a glance must resolve it. Type sizes follow the order.

| Rank | Element | Why it earns the space | Min size @1080p |
|---|---|---|---|
| 1 | **Call banner** — current call, priority, age, lap, one evidence line | The spoken word is the product; the screen confirms and explains it | ~60 px text, 160–190 px tall |
| 2 | **Freshness** — LIVE/STALE, packet age, WS rate | A stale screen must be recognisable instantly | 28 px, top-left corner, always the same place |
| 3 | **Previous radio** — one line below the current call | Preserves the last call as the next one arrives, without competing for attention | 18–24 px, dim |
| 4 | **Radio log** — older calls, lap, priority, audio state / ACK / NEG | "What did it say two laps ago?" | 26 px |
| 5 | **Tyres** — 2×2 car plan view; inner EMA temp, surface, wear %, status word | Justifies thermal / wear calls; the most common M1–M2 call family | 40 px temp |
| 6 | **Fuel** — laps remaining, then delta to a real target when available | Justifies lift-and-coast / fuel calls | 40 px |
| 7 | **Lap / position / session phase** | Context for every call | 32 px |
| 8 | **Damage** — front wing L/R, rear wing, floor, diffuser, sidepod, gearbox, engine | Justifies "box for a wing"; hidden when all zero | 24 px, list |
| 9 | **Mindset** BALANCED / AGGRESSIVE + verbosity + quiet | One-press control; must be visible to trust the calls | 24 px pill |
| 10 (M3) | **Pit window**, undercut/overcut threats, gaps ahead/behind, stint plan | Race strategy; the tactical block | 32 px |
| — | ERS, SC status, latency p99 | Footer, small, dim. Diagnostics, not driving information | 18 px |

## 3. Layout grid

Landscape, 16:9. Fixed 12-column grid with `rem` sizing driven by a root font size set
from viewport width (`font-size: clamp(14px, 0.9vw, 24px)`), so 1080p and 1440p render
the same composition with proportionally larger type. Zones never reflow between
sessions; empty zones keep their box so nothing jumps when a value appears.

```
┌────────────────────────────────────────────────────────────────────────────┐
│ A  STATUS BAR   ● LIVE  age 42 ms  10 WS/s │ RACE · Bahrain  LAP 24/57  P5 │ 48 px
│                 phase: ON TRACK            │ MED  age 14 laps   [BALANCED] │
├────────────────────────────────────────────────────────────────────────────┤
│ B  CURRENT  ▮ P2  "Front left is hot. Ease the trail braking." L24 · 8 s   │
│    WHY  FL inner 112° HOT · wear 38%                                       │ 190 px
│    PREVIOUS  L23 "Fuel is good. Stay on target."                            │
├─────────────────────────────────┬──────────────────────────────────────────┤
│ C  TYRES (2×2 plan view)        │ E  RADIO LOG                             │
│    FL 112 HOT   │  FR  98 OK    │   L22 ▶ Fronts are getting warm…        │
│    surf 110 · 38% surf 101 · 35%│   L21 ✗ ERS at ninety (dropped)          │
│    ─────────────┼─────────────  │   L20 ▶ Fuel is good, push.             │
│    RL  94 OK    │  RR  95 OK    │   …                                      │
│    surf 92 · 22%  surf 93 · 21% │                                          │  fills
├─────────────────────────────────┤   …                                      │
│ D  FUEL  +0.4 laps   34.4 laps  │                                          │
│    DAMAGE FW L 12% · Floor 4%   │                                          │
├─────────────────────────────────┤                                          │
│ F  STRATEGY (M3; placeholder    │                                          │
│    until then)                  │                                          │
│    pit window · gaps · undercut │                                          │
├─────────────────────────────────┴──────────────────────────────────────────┤
│ G  FOOTER  ERS 62% · SC 0 · call p99 210 ms · ws 12 ms                       │ 32 px
└────────────────────────────────────────────────────────────────────────────┘
        ◄────────── 5 cols ──────────►◄──────────── 7 cols ─────────────►
```

Zone rules:

- **A status bar**: freshness lives at the far left, first thing the eye lands on in
  Western reading order. Session/lap/position right-aligned. Mindset pill far right.
- **B banner**: full width, fixed height, never collapses. A large current line, a
  single evidence line and a dim, one-line **previous radio** subtitle share this
  fixed space. The subtitle shows the immediately preceding call whose audio started;
  the next call replaces it. The log shows older calls, omitting those already in the
  banner, but retains all calls in its underlying history. Long calls use at most two
  banner lines with the complete text retained in call history for review. When there
  is no call, the banner says "— radio quiet —".
- **C tyres**: a 2×2 grid drawn as the car from above — **FL top-left, FR top-right, RL
  bottom-left, RR bottom-right** — with a thin centre-line so left/right is unambiguous
  at a glance. Compound chip and tyre age sit above the grid. The **whole tile** takes the
  status colour (border + faint background tint, not just the number) so a corner
  heating up is visible in peripheral vision. Brake temp sits small in each tile's
  corner: it can help explain heat or lock-ups, but it never competes with the tyre
  temp for size. Brake colours remain neutral until a calibrated threshold exists.
- **D fuel + damage**: fuel laps remaining is the large number until the backend
  supplies a target delta; only then does the signed delta take prominence. Damage
  renders as a list of non-zero components only; the zone header stays.
- **E radio log**: newest older call at the top; the current and previous calls are
  already visible in B. 8 older rows at 1080p, 10 at 1440p.
- **F strategy**: reserved for M3. Until then it shows the pit-window placeholder line
  from the roadmap ("pit window: M3") in the dim colour, so the grid does not change
  when strategy lands.
- **G footer**: diagnostics only. Everything here is deliberately small.

### 1440p

Same grid; root font scales ~1.33×. The radio log gains two rows. No other change.

### `/radio` compact view

Log only: status bar (freshness, lap), banner with previous-call subtitle, then the log
in ~1.6× type. For a narrow
portrait screen or phone. Same `app.js`, `body.radio` class hides zones C/D/F/G.

## 4. Colour and typography tokens

CSS custom properties in `web/style.css`; every colour is paired with a word in the DOM.

```css
:root {
  --bg: #0a0a0c;        --panel: #121216;   --line: #2a2a30;
  --fg: #ececec;        --dim: #8a8a92;     --faint: #4a4a52;
  --ok: #37e07a;        /* green  — in window, LIVE */
  --warn: #ffc233;      /* amber  — P2, approaching threshold, delta small */
  --crit: #ff4d4d;      /* red    — P1, HOT, act now, STALE banner */
  --cold: #5cc8ff;      /* blue   — COLD tyres (distinct from red/green for CVD) */
  --info: #b8b8ff;      /* lilac  — P3 informational */
  --aggr: #ff8a3d;      /* orange — AGGRESSIVE mindset pill */
  --font: "Cascadia Mono", "Consolas", "JetBrains Mono", ui-monospace, monospace;
  font-size: clamp(14px, 0.9vw, 24px);   /* 17.3 px @1920, 23 px @2560 */
}
```

Type scale (rem): banner 3.4, previous call 1.1, evidence 1.2, tyre temp 2.4,
fuel 2.4, status bar 1.5, log 1.4,
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
| LIVE/STALE word | `live`, `packet_age_ms` | `LIVE` green when `live`; `STALE` red otherwise. Client also applies its own >1 s guard against local time of the last frame (see §6) |
| Packet age | `packet_age_ms` | `NNN ms`; amber ≥ 500 ms, red ≥ 1000 ms |
| WS update rate | *derived client-side* from frame arrival (`rate_hz` is `null` today) | `N WS/s`, dim; do not label it UDP packet rate |
| Session line | `session_kind`, `session_type`, `track` | `RACE · bahrain` upper-cased; `session_type` numeric is not shown |
| Lap | `lap_num`, `total_laps` | `LAP 24/57`; `total_laps==0` → `LAP 24` |
| Position | `position` | `P5`; `0` → `P--` |
| Phase | `phase` | `garage / out_lap / flying / in_lap / on_track` → upper-case words; `out_lap` amber (thermal calls likely) |
| Compound + age | `tyre_visual`, `tyre_compound`, `tyre_age_laps` | Chip `MED · 14 laps`; visual compound preferred, actual as tooltip |
| Tyre tile temp | `tyres.{fl,fr,rl,rr}.inner` | Integer °C, coloured by `status` |
| Tyre tile status | `tyres.*.status` | `COLD` blue / `OK` green / `HOT` red word under the temp |
| Tyre tile detail | `tyres.*.surface`, `tyres.*.wear` | `surf 110 · wear 38%`; wear amber ≥ 50 %, red ≥ 70 % (thresholds from `/api/config` later) |
| Fuel delta | **planned** backend `fuel_delta_laps` relative to a session-specific target | Signed `+0.4 laps`; green ≥ 0, amber −0.5..0, red < −0.5. Absent/null → show laps only; never infer a target in the client |
| Fuel laps | `fuel_remaining_laps` | `34.4 laps`, one decimal, prominent when delta is unavailable |
| Damage list | `damage.{front_left_wing, front_right_wing, rear_wing, floor, diffuser, sidepod, gearbox, engine}` (**being added**; percentages 0–100) | Rows for non-zero values only: `FW L 12%`; rear wing shows `RW` when supplied as one value, or `RW L` / `RW R` if split values become available. Amber ≥ 10, red ≥ 30. Header reads `DAMAGE none` when all zero |
| Mindset pill | `mindset` | `BALANCED` dim outline; `AGGRESSIVE` orange filled |
| Verbosity / quiet | `verbosity`, `quiet` | Small text right of the pill; `QUIET` in amber when true |
| Current / previous radio | latest `call` frames (P1/P2/P3), keyed by `id`; `spoken`/`cancel` events | Show most recent dispatched call large; after the next one arrives, the last audio-started call becomes the dim previous line. Cancel before audio start → remove current and retain last audio-started call |
| Banner age | envelope `t` of the `call` frame relative to a server timestamp + local monotonic elapsed time | `8 s ago`, updated once per second; resumed calls need their original timestamp from the server without assuming clocks are synchronized |
| Call evidence | **planned** optional evidence captured with the call (e.g. `FL inner 112° · HOT`) | One line beneath current call; hidden if no call-specific evidence. Do not present changed live telemetry as if it justified an older call |
| Log rows | `calls[]` from a **planned** complete snapshot, then `call`/`spoken`/`cancel` frames | Earlier calls only; ▶ for audio started, ✗ for cancellation before start, ✓ only after a future audio-finished event. ACK/NEG chips when `12-driver-input.md` lands |
| Brakes | `brakes.{fl,fr,rl,rr}` | Small `brk 412` in each tyre tile's top-right; neutral until a calibrated brake threshold exists |
| ERS | `ers_pct` | Footer `ERS 62%` |
| SC | `safety_car` | Footer `SC 0`; non-zero also paints the status bar background amber with the word `SAFETY CAR` / `VSC` |
| Latency | `latency.trigger_to_speak_p99_ms`, `latency.packet_to_ws_p99_ms` | Footer, dim |
| Protocol mismatch | close code 4001 / `v !== 1` | Banner replaced by red `PROTOCOL MISMATCH — RELOAD`; no reconnect |

Fields **not** rendered on purpose: `session_type` (raw enum), `latency` beyond p99.

**Requested payload additions** (for the owner; none are required for phase 1):
`damage` object as above (optional `rear_left_wing` / `rear_right_wing` if available);
`fuel_delta_laps` (server owns the target); optional
call-specific evidence; a call-history snapshot containing event timestamps and
audio status; an audio-finished event if "heard in full" is needed; `rate_hz` populated;
M3: `strategy: {pit_window: [26, 28], ahead: {pos, name, gap_s, compound}, behind: {...},
undercut_s, overcut_s, stint_plan: [...]}`.

## 6. Stale and reconnect behaviour

Two independent staleness sources, either one greys the page:

1. **Server-reported**: `live === false` or `packet_age_ms > 1000` in the latest frame.
   Meaning: the game stopped sending UDP (menu, pause, crash).
2. **Client-observed**: no WS frame for > 1 s (a client `setInterval` checks the last
   frame time). Meaning: the backend or socket is gone. This is the case a
   server-only check can never catch.

Greyed state: `body.stale` desaturates and dims telemetry, strategy and log, while the
status bar turns **red** with `STALE · last packet 4.2 s ago` counting up. The main
banner immediately reads `TELEMETRY STALE · ENGINEER ADVICE PAUSED`. The latest
audio-started call moves to the dim subtitle labelled `LAST RADIO · STARTED · L31 · 5 s ago`;
it never remains a large imperative like "Box now". There is no transition into
stale/error states. Numbers stay visible as historical data. Refresh the status bar's
age using locally elapsed monotonic time when no new packet or frame arrives.

Reconnect: exponential back-off 0.5 → 5 s (as today). On `open` send `hello` with
`last_seq`; the **planned** always-on `snapshot` supplies recent calls with their
original times and statuses, including to a first-time client (currently the server
only sends a snapshot if `last_seq` is non-null, and its call buffer stores no
`spoken`/`cancel` outcomes). Reconcile by call ID/sequence without duplicating rows.
Only return to the active banner after a fresh state frame reports `live` and
`packet_age_ms ≤ 1000`; a socket reconnection alone does not prove live telemetry.
If `config_hash` changes on `hello`, show one dim "config reloaded" log row.

Startup with no frames yet: status bar shows `CONNECTING…` (dim, not red) for the first
3 s, then `STALE`.

## 7. Call banner lifecycle

At present `call` means **dispatched to the audio sink**, not necessarily audible.
`spoken` fires when the speech engine *starts* output; it does not mean audio has
finished. `cancel` before start means dropped; after start it can mean interrupted.
Do not use ✓ or say "heard" based on `spoken` alone. If confirmed completion matters,
add a separate audio-finished/interrupted event in a subsequent protocol change.

1. **DISPATCHED**: on `call`, show the new P1/P2/P3 text immediately in the fixed,
   large current area with priority bar, lap and `awaiting audio`. Only P1 is urgent
   enough to skip all transition effects. New lower-priority calls also replace
   current when dispatched; the old call moves to the dim previous line *only if its
   audio started*. A never-audible call remains in the log as dropped.
2. **AUDIO STARTED**: on `spoken`, change the label to `▶ started · 8 s ago`. This can
   remain visible after the sound finishes; no check mark is implied. A later call
   replaces the current one, moving the prior started call into the one-line
   `PREVIOUS · L23` slot; the next call replaces that subtitle again. The underlying
   history retains recent calls, but the visible log starts after the two banner calls.
3. **IDLE/FADED**: after 20 s (P2/P3) or 30 s (P1) without a newer call, dim the
   current text but keep the last call readable. Fade is only visual, not proof that
   an instruction is still valid; urgent instructions need expiry/revalidation rules.
4. **CANCELLED/PREEMPTED**: cancelled before audio started → ✗ in the log and restore
   the last audio-started call or quiet state. Cancelled after audio started → label
   `interrupted` in history; never treat the entire instruction as heard. A P1
   preempts the visual banner immediately, without waiting for an animation.
5. **STALE**: replace current advice with the stale warning as in §6. The previous
   line becomes `LAST RADIO · STARTED` and shows age. Resume only on a fresh live state.

For normal call-to-call handoff, keep the banner and subtitle rows fixed: animate a
*brief ghost* of the old current text up to 220 ms toward the smaller, lighter previous
line (transform + opacity), and crossfade the new text into the primary row in
180–220 ms. The new words are readable **immediately**; transitions must never queue
or delay later frames. Clip the subtitle to one line with an ellipsis; the log retains
the full text. `prefers-reduced-motion: reduce` disables motion; P1 and stale/error
states also switch instantly. No pulsing, scrolling ticker or shifting telemetry.

ACK / NEG (from `12-driver-input.md`, M2) appears as a chip on the current or
previous line when it refers to that call, as well as in history. NEG does not imply
that audio was interrupted.

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

Each PR keeps `/` and `/radio` working against the current `state_payload`; the first
three use the existing protocol, with absent/planned fields hidden. PR 4 handles
the call-history contract.

1. **Tokens + grid + freshness** — restructure `index.html`/`style.css`; bring the
   client-side 1 s timeout, `CONNECTING…` and the red stale warning to `app.js` in
   the same PR. Avoid shipping a new banner that could appear live when frozen.
   Verify at 1920×1080 and 2560×1440, including long calls and lost WS/UDP.
2. **Tyres 2×2 + fuel + evidence** — `app.js` render functions per zone; plan-view
   tyre tiles, remaining fuel laps, WS update rate. Hide the target delta and
   call-evidence strip until the backend supplies them. Unit-testable pure
   formatters can live in `web/format.js`.
3. **Current + previous radio + log** — state machine from §7, age counter,
   ≤220 ms handoff, reduced motion, next older call at the top of the log,
   and protocol-mismatch state. Test rapid consecutive calls, P1 preemption,
   cancellation, stale during transition and long-text clipping.
4. **Call history contract** — in a separate server/protocol PR, send a complete
   recent-call snapshot on initial connection and reconnect with original
   timestamps, status and stable IDs; add confirmed finish/interruption events
   only if the product needs them. Keep current clients compatible or version
   the protocol. Verify replay and reconnect recovery.
5. **Damage + server-computed fuel delta** — render `damage` once the owner's
   server change lands in zone D; add `fuel_delta_laps` after its target has a
   defined session-aware source. Do not compute a race-only guess for quali/practice;
   absent/unknown delta leaves fuel laps remaining prominent.
6. **`/radio` compact** — `body.radio` rules on the new grid; test on a phone in portrait.
7. **M3 strategy zone** — when `strategy` exists in the payload. Separate PR series.

Definition of done: screenshots at both landscape sizes and portrait `/radio`,
lint/type checks for any changed Python, and an offline `pitwall replay --serve`
render check against a test recording. No running game is needed.

## 10. Open questions for the owner

1. **Fuel delta target** — which backend strategy target owns `fuel_delta_laps` in
   race, quali and practice? Until defined, display only laps remaining. In the
   illustrative live mockup, lap 24/57 has 34 laps including the current lap;
   34.4 remaining gives a future server delta of +0.4. In the M3 mockup,
   lap 31/52 has 22 laps including the current lap; 21.7 gives −0.3.
2. **Damage thresholds** — amber 10 % / red 30 % are guesses. Should these come from
   `thresholds` in config like the tyre temps, and should the server emit a
   `status` word per component the way it does for tyres?
3. **Log order** — newest-first (proposed, keeps the top row adjacent to the banner) vs.
   the current newest-last chat style?
4. **Banner fade times** — 20 s / 30 s, or tie to the rule's `deadline_ms`?
   Expiration of an actionable P1 needs a distinct rule from its visual fade.
5. **Tyre tile colour source** — keep server `status` (COLD/OK/HOT from config) or also
   let wear drive a second colour band? Proposal: temp colours the number, wear colours
   only its own `38%` text.
6. **Brake threshold** — brakes are shown small inside each tyre tile and remain
   neutral for now; what calibrated threshold should turn them amber?
7. **SC/VSC** — status-bar paint only, or should a `safety_car != 0` also force a
   synthetic banner if the dispatcher has not sent a P1 yet?
8. **Should `rate_hz` be populated** by the server (it is `null` today) or is a client
   derived number acceptable?
9. **1440p only?** If the second monitor is known, the `clamp()` scaling can be dropped
   for fixed pixel sizes.
