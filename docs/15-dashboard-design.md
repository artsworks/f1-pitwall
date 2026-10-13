# Dashboard redesign: the second-monitor race-engineer screen

Status: **phases 1–6 implemented** in `web/`. M3 adds the zone F `strategy` block,
backend-owned `fuel_delta_laps`, and the backend-owned page reset (§10). Call evidence awaits
backend fields. The banner clears to "radio quiet" after 20 s / 30 s, then moves the call
to the log. Static sample mockups live in `docs/mockups/` (`dashboard-1080p.html`,
`dashboard-m3-stale.html`, `radio.html`). They use self-contained vanilla HTML/CSS with
no build step. Open them in a browser. Call evidence, fuel targets, and damage values
illustrate planned backend fields.

This doc supersedes the "Dashboard" section of `04-audio-ui.md` for layout. The runtime
is unchanged; planned call-history additions are listed below.

## 1. Goals and non-goals

**Goals**

1. The driver glances at the screen for well under a second while driving F1 26. Every
   glance must answer one question: *what is the engineer saying, and can I act on it?*
   The **call banner** is therefore the largest thing on screen.
2. A small previous-call subtitle provides continuity; the radio log holds older calls
   and distinguishes dispatched, audio started, interrupted and dropped.
3. Connection freshness is essential. LIVE / STALE and packet age are always visible.
   Above 1 s of staleness, the whole page greys out so a frozen screen never looks alive.
4. The few numbers that *justify* a call are visible without reading: tyres per corner,
   fuel laps and delta, pit window / gaps (M3), damage, lap / position / phase, mindset.
5. Readable on a 27-inch overhead monitor at roughly 1 m, at either 1080p or 1440p.
   Use a dark, high-contrast design with large type; pair every colour with a word.
6. Offline. Vanilla HTML/CSS/JS served by the backend; no CDN, no framework, no build.

**Non-goals**

- Not a telemetry gauge cluster. No speed, RPM, gear, throttle/brake traces, g-forces,
  minimap, full sector telemetry or per-lap history table. If the game HUD already shows it, we don't.
- The screen does not control the session. Every mid-race control (ACK / NEG, mindset,
  quiet) has a wheel or keyboard binding (`12-driver-input.md`). The screen only
  *displays* their state.
- Not a debrief tool. Stint plots, deg curves and call grading live in the post-session
  HTML debrief (M4, `16-debrief-design.md`) and review mode (`07-replay-and-debug.md`).
- No continuous animation (motion rules in §11). A new call makes one brief transition
  into the fixed banner while the old call moves to a smaller previous-call line. Urgent
  calls and stale/error states appear immediately. Reduced-motion users see no motion.

## 2. Information hierarchy

Ordered by how often a glance must resolve it. Type sizes follow the order.

| Rank | Element | Why it earns the space | Min size @1080p |
|---|---|---|---|
| 1 | **Call banner** — current call, priority, age, lap, one evidence line | The spoken word is the product; the screen confirms and explains it | ~60 px text, 160–190 px tall |
| 2 | **Freshness** — LIVE/STALE, packet age, WS rate | A stale screen must be recognisable instantly | 26 px, top-left corner, always the same place |
| 3 | **Previous radio** — one line below the current call | Preserves the last call as the next one arrives, without competing for attention | 22 px, dim |
| 4 | **Radio log** — recent calls, lap, priority, audio state / ACK / NEG | "What did it say two laps ago?" Lives on `/radio`, not the main screen | 26 px |
| 5 | **Tyres** — 2×2 car plan view; inner EMA temp, surface, wear %, status word | Justifies thermal / wear calls; the most common M1–M2 call family | 52 px temp, 22–23 px labels and detail, 20 px brake |
| 6 | **Fuel** — laps remaining, then delta to a real target when available | Justifies lift-and-coast / fuel calls | 40 px |
| 7 | **Lap / position / session phase** | Context for every call | 32 px |
| 8 | **Damage** — front wing L/R, rear wing, floor, diffuser, sidepod, gearbox, engine | Justifies "box for a wing"; hidden when all zero | 22 px, list |
| 9 | **Mindset** BALANCED / AGGRESSIVE + verbosity + quiet | One-press control; must be visible to trust the calls | 24 px pill |
| 10 (M3) | **Pit window**, undercut/overcut threats, gaps ahead/behind, stint plan | Race strategy; the tactical block. In a race the ahead/behind duel cards (gap rail, tyre, pace, trend, threat) take the top of the right column | 32 px |
| — | ERS, SC status, latency p99 | Footer, small, dim. Diagnostics, not driving information | 18 px |

## 3. Layout grid

Use a landscape 16:9 layout with a fixed 12-column grid. Set the root font size from
viewport width (`font-size: clamp(14px, 0.9vw, 24px)`) so 1080p and 1440p use nearly the
same physical scale on a 27-inch panel. The live screen is one fixed page. Zones never
reflow between sessions, and empty zones keep their space when values are absent.
`docs/mockups/dashboard-reimagined.html` is the approved visual reference.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ A STATUS  ● LIVE 42 ms  ON TRACK │ RACE · BAHRAIN  LAP 24/57  P5  OV SM  BAL │ 3 rem
├──────────────────────────────────────────────────────────────────────────────┤
│ B CURRENT  "Car behind in range. Defend into turn 1."            L24 · 8 s   │
│   WHY  gap 0.8 s · closing 0.3 s/lap      PREVIOUS  L23 "Fuel on target."    │ 12 rem
├────────────────────────┬──────────────────┬──────────────────────────────────┤
│ C TYRES  MED · 14 laps │ D FUEL   BATT    │ E CONTEXT (auto)                 │
│   FL 103° ▲ │ FR 98° ▬ │   +0.4    62%    │   SAFETY    SC, red flag, rain   │
│   wear 38%  │ wear 35% │ WEATHER now/10/30│   EXECUTION box now              │
│   RL 94° ▼  │ RR 95° ▬ │   crossover ETA  │   REPLY     menu answer          │
│   BB 56%  DIFF ON 60%  │ LAST LAPS        │   TACTICAL  battle, strategy     │
│                        │ DAMAGE strip     │   INFO      rival pitted, penalty│
├────────────────────────┴──────────────────┴──────────────────────────────────┤
│ G FOOTER  update rate · voice queue · TTS · screen lag · ERS · SC             │ 2 rem
└──────────────────────────────────────────────────────────────────────────────┘
   ◄──── cols 1–4 ────►  ◄── cols 5–7 ──►   ◄──────── cols 8–12 ────────►
```

Zone rules:

- **A status bar**: place freshness at the far left. Right-align session, lap and
  position. The OV chip lights when overtake mode is active or earned, and the SM chip
  lights in straight mode. The compact mindset pill sits at the far right.
- **B banner**: keep it full-width and fixed-height. It holds the current call, one
  evidence line and one dim PREVIOUS line. The main screen has no radio log. With no
  call, the banner says "— radio quiet —".
- **C tyres**: draw a 2×2 grid as the car from above (FL top-left, RR bottom-right). Each
  corner shows the inner temperature, a trend arrow, wear %, a meter for the 80 to 110 °C
  window and the brake temperature. The arrow compares `inner` (fast EMA) with
  `tyre_inner_ema_slow`: red ▲ heating, blue ▼ cooling, white ▬ steady. The arrow
  replaces the status word, which is an owner exception to the colour plus word rule.
  The hot-wear corner gets a HOT WEAR tag with the rate, and a locked wheel gets an
  orange LOCKED border. The BB and DIFF strip sits under the grid.
- **D fixed tiles**: fuel at the flag, battery with its budget mode, weather, and two
  LAST LAPS rows. Each row shows the newer lap and time, then lap and sector deltas against
  the lap before. Keep the last three laps so the oldest is the baseline. Damage stays at the bottom.
  Weather shows an icon and rain chance for now, 10 and 30 min. The crossover slot gets
  a marker, and a line under it gives the target compound and ETA.
- **E contextual zone**: cards follow a fixed order: Safety, Execution, Reply, Tactical,
  Info. Safety covers SC, VSC, red flag, blue flag and RAIN CROSSOVER when
  `weather_crossover_min` is 5 or less. Tactical holds the battle card and the strategy
  plans. In qualifying the zone shows the session phase, cut-off, benchmark sectors and
  the current run.
- **Battle card**: one rival as the single card. When `battle.ahead2` or
  `battle.behind2` is set, or places swap, the card lists up to two cars each side
  with a state word, gap and rail around a YOU divider. The direct rival's row word
  comes from `battle.mode`, the same word the header shows; other rows read MANAGING,
  or FREE AIR in free air. A second car within 1 s of the
  car between shows IN TRAIN.
- **Takeovers**: the pit board takes over the main row, and cool-down replaces the tyres
  and tiles. The status bar, banner, contextual zone and footer stay.
- **G footer**: show diagnostics only, in small type.

### 1440p

Use the same grid. The root font scales ~1.33× to preserve text size on a 27-inch panel.

### `/radio` compact view

Show only the status bar (freshness, lap), banner with previous-call subtitle, and log in
~1.6× type. The same `app.js` uses the `body.radio` class. On a screen 600 px wide or
less (or with `?phone=1`), `/` adds `body.phone`: one column with the banner, context
cards, tyres and an ARM PHONE RADIO button for browser speech.

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

Type scale (rem): banner 3.4, previous call 1.3, evidence 1.3, tyre temp 3.0,
tyre corner + status 1.35, surface/wear 1.25, brake 1.15; fuel 2.4,
status bar 1.5, log 1.5, section headings 1.15, footer 0.9.
At 1080p, the tyre temperature is ~52 px and its detail ~22 px. On the same
27-inch screen at 1440p, they are ~72 px and ~30 px, so their physical sizes
stay close. Tabular figures (`font-variant-numeric: tabular-nums`) so
numbers do not jitter. Monospace throughout: values line up and the driver learns
positions, not shapes.

Priority colour is used on the **banner left bar and the log row marker**, never on the
whole text; white text beside a red bar stays readable from the overhead monitor.

### Screen copy

Use short, action-first calls: `Front left hot. Ease trail braking.` instead of a long
preamble. The banner and log show the **exact dispatched call text**. Shorten spoken calls
in their rule templates, not through client-side rewrites of history. Mockup phrases show
the proposed concise style, not a change to current audio.

Keep `HOT`, `COLD`, `LIVE`, `STALE`, and instructions such as `Box` as words. Use
`FL/FR/RL/RR` for corners, `BRK` for brake temperature, `surf` for surface temperature,
`L` after a number for laps, `FW/RW` for wings, `UC` for undercut, and `S3` for sector
three. Change `age 14 laps` to `14L old`, and `34.4 laps remaining · 34 including this
lap` to `34.4 left · 34 to finish`. Keep why an urgent call matters and its action
condition.

## 5. State → component mapping

All fields are from `state_payload` in `src/pitwall/server/app.py` (WS `state` /
`snapshot` frames) unless noted. `call` / `spoken` / `cancel` frames are per
`04-audio-ui.md`.

| Element | Field(s) | Rendering rule |
|---|---|---|
| LIVE/STALE word | `live`, `packet_age_ms` | `LIVE` green when `live`; `STALE` red otherwise. Client also applies its own >1 s guard against local time of the last frame (see §6) |
| Packet age | `packet_age_ms` | `NNN ms`; amber ≥ 500 ms, red ≥ 1000 ms |
| WS update rate | *derived client-side* from frame arrival (`rate_hz` is `null` today) | `N upd/s`, footer, dim; do not label it UDP packet rate |
| Session line | `session_kind`, `session_type`, `track` | `RACE · bahrain` upper-cased; `session_type` numeric is not shown |
| Lap | `lap_num`, `total_laps` | `LAP 24/57`; `total_laps==0` → `LAP 24` |
| Position | `position` | `P5`; `0` → `P--` |
| Phase | `phase` | `garage / out_lap / flying / in_lap / on_track` → upper-case words; `out_lap` amber (thermal calls likely) |
| Compound + age | `tyre_visual`, `tyre_compound`, `tyre_age_laps` | Chip `MED · 14L old`; visual compound preferred, actual as tooltip |
| Tyre tile temp | `tyres.{fl,fr,rl,rr}.inner` | Integer °C, coloured by `status` |
| Tyre tile status | `tyres.*.status` | `COLD` blue / `OK` green / `HOT` red word under the temp |
| Tyre tile detail | `tyres.*.surface`, `tyres.*.wear` | `surf 110° · wear 38%`; wear amber ≥ 50 %, red ≥ 70 % (thresholds from `/api/config` later) |
| Fuel delta | **planned** backend `fuel_delta_laps` relative to a session-specific target | Signed `+0.4 laps`; green ≥ 0, amber −0.5..0, red < −0.5. Absent/null → show laps only; never infer a target in the client |
| Fuel laps | `fuel_remaining_laps` (game MFD: laps spare or short at the flag, not laps in tank) | `+0.4 laps`, one decimal, shown when the backend delta is unavailable |
| Damage list | `damage.{front_left_wing, front_right_wing, rear_wing, floor, diffuser, sidepod, gearbox, engine}`, `damage.drs_fault`, `damage.ers_fault` | Non-zero entries in a strip under the tiles; wraps only when needed; amber ≥ 5 %, red ≥ 25 %; smaller text past 3 entries; `✓ none` when all values and faults are zero |
| Last laps | `race.recent_laps[{lap,time_ms,sectors_ms}]` | Two rows compare the newer lap with the lap before. Each shows its lap and time, lap delta, and S1/S2/S3 deltas. Keep three laps so the oldest is the baseline. Negative deltas are green and positive deltas are amber. With no laps, row 2 says `no lap yet`. With one lap, row 2 shows its time and `--` for the delta and sectors. Missing rows and sectors show `--`; a sector delta is `--` if either value is 0 |
| Mindset pill | `mindset` | `BALANCED` dim outline; `AGGRESSIVE` orange filled |
| Verbosity / quiet | `verbosity`, `quiet` | Small text right of the pill; `QUIET` in amber when true |
| Current / previous radio | latest `call` frames (P1/P2/P3), keyed by `id`; `spoken`/`cancel` events | Show most recent dispatched call large; after the next one arrives, the last audio-started call becomes the dim previous line. Cancel before audio start → remove current and retain last audio-started call |
| Banner age | envelope `t` of the `call` frame relative to a server timestamp + local monotonic elapsed time | `8 s ago`, updated once per second; resumed calls need their original timestamp from the server without assuming clocks are synchronized |
| Call evidence | **planned** optional evidence captured with the call (e.g. `FL inner 112° · HOT`) | One line beneath current call; hidden if no call-specific evidence. Do not present changed live telemetry as if it justified an older call |
| Log rows | `calls[]` from the current hello `snapshot`, then `call`/`spoken`/`cancel` frames | Earlier calls only; restored rows with unknown audio status stay unmarked. ▶ for confirmed audio start, ✗ for cancellation before start, ✓ only after a future audio-finished event. ACK/NEG chips when `12-driver-input.md` lands |
| Brakes | `brakes.{fl,fr,rl,rr}` | Secondary `BRK 412°` in each tyre tile's top-right; neutral until a calibrated brake threshold exists |
| ERS | `ers_pct` | Footer `⚡ BATTERY 62%` |
| SC | `safety_car` | Footer `TRACK GREEN`; non-zero shows `⚠ SAFETY CAR` / `⚠ VSC` as a status-bar chip and paints the status bar background amber with the word `SAFETY CAR` / `VSC` |
| Latency | `latency.queue_count`, `latency.queue_oldest_s`, `latency.queue_wait_p99_ms`, `latency.tts_p99_ms`, `latency.packet_to_ws_p99_ms` | Footer, dim |
| Protocol mismatch | close code 4001 / `v !== 1` | Banner replaced by red `PROTOCOL MISMATCH — RELOAD`; no reconnect |

Fields **not** rendered on purpose: `session_type` (raw enum), `latency` beyond p99.

**Requested payload additions** (for the owner; none are required for phase 1):
`damage` object as above (optional `rear_left_wing` / `rear_right_wing` if available);
`fuel_delta_laps` (server owns the target); optional
call-specific evidence; original dispatch timestamps and audio outcomes in the
existing snapshot's `calls[]`; an audio-finished event if "heard in full" is needed;
`rate_hz` populated;
M3: `strategy: {pit_window: [26, 28], ahead: {pos, name, gap_s, compound}, behind: {...},
undercut_s, overcut_s, stint_plan: [...]}`.
Named plans (docs/03 "Strategy plans") add, inside `strategy`:
`plans: [{id: 'A'|'B'|'C', kind: 'primary'|'alternative'|'reactive', label: '1-stop M-H',
stops, compounds: ['MEDIUM','HARD'], stop_laps: [27], window: [26, 28] | null, delta_s,
active}]` (delta vs the active plan, negative = faster; C is "box now under SC/VSC"),
`active_plan` (`'A'`/`'B'`/`'C'` or null before lap 1), `on_plan` (bool or null),
`plan_off_s` (active plan's loss vs the best legal strategy), `plan_target_lap` (next stop
of the active plan or null) and `plan_switch: {from, reason: 'pace'|'invalid'|'sc', lap}`
or null. Backend only; UI rendering is a separate PR.

Battle state (docs/18 "Battle state") adds `battle: {mode: 'free_air'|'catching'|
'attacking'|'defending'|'under_threat'|'managing', mode_laps, catch_laps | null,
threat_laps | null, closing_ahead_s, closing_behind_s, pass_prob, hold_prob,
result: 'passed'|'failed'|'held'|'lost' | null}` (result only for ~20 s after an episode).
The `battle` object also carries `ahead2` and `behind2` (`{pos, name, gap_s,
overtake}` or `null` when there is no second car) and `swaps` (place changes
inside `battle_swap_window_s`). Backend only.

Other additions: `track_info.weather_crossover_min` and
`track_info.weather_crossover_pct` (minutes out and rain chance, `null` when
no crossover), `strategy.tyres.wear_hot_corner` and
`strategy.tyres.wear_hot_rate` (hot corner name and wear rate, `null` when
none), and per-corner `tyres.*.tyre_inner_ema_slow` plus `tyres.*.lockup`
(`true` only on the wheel that locked). `race.last_lap_ms` and
`race.best_lap_ms` are null before the first lap.

## 6. Stale and reconnect behaviour

Either of these two independent staleness sources greys the page:

1. **Server-reported**: `live === false` or `packet_age_ms > 1000` in the latest frame.
   The game stopped sending UDP (menu, pause, crash).
2. **Client-observed**: no WS frame for > 1 s. A client `setInterval` checks the last
   frame time. The backend or socket is gone, which a server-only check cannot detect.

In the greyed state, `body.stale` desaturates and dims telemetry, strategy, and log. The
status bar turns **red** and counts up `STALE · last packet 4.2 s ago`. The main banner
immediately reads `TELEMETRY STALE · ADVICE PAUSED`. Move the latest audio-started call
to the dim `LAST RADIO (STARTED) · L31 · 5s` subtitle. Never leave a large imperative
such as "Box now" in the banner. Do not animate stale/error transitions. Keep numbers
visible as historical data. Update the status bar's age using locally elapsed monotonic
time when no new packet or frame arrives.

Reconnect: exponential back-off 0.5 → 5 s (as today). On `open` send `hello` with
`last_seq`; the server already sends a `snapshot` with recent calls after **every**
client hello, including a first-time load. `calls[]` currently contains dispatched
call payloads and sequence numbers, but not the original frame timestamps or
`spoken`/`cancel` outcomes. Reconcile by call ID/sequence without duplicating rows;
on a fresh load, mark restored audio status and age as unknown rather than treating
the reconnect time as dispatch time or claiming the call was heard.
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
   large current area with priority bar, `⚠ URGENT` / `● ACTION` / `◦ INFO`, lap and `… queued`. Only P1 is urgent
   enough to skip all transition effects. New lower-priority calls also replace
   current when dispatched; the old call moves to the dim previous line *only if its
   audio started*. A never-audible call remains in the log as dropped.
2. **AUDIO STARTED**: on `spoken`, change the label to `▶ started · 8 s ago`. This can
   remain visible after the sound finishes; no check mark is implied. A later call
   replaces the current one, moving the prior started call into the one-line
   `PREV · L23` slot; the next call replaces that subtitle again. The underlying
   history retains recent calls, but the visible log starts after the two banner calls.
3. **IDLE/FADED**: after 20 s (P2/P3) or 30 s (P1) without a newer call, dim the
   current text but keep the last call readable. Fade is only visual, not proof that
   an instruction is still valid; urgent instructions need expiry/revalidation rules.
4. **CANCELLED/PREEMPTED**: cancelled before audio started → ✗ in the log and restore
   the last audio-started call or quiet state. Cancelled after audio started → label
   `interrupted` in history; never treat the entire instruction as heard. A P1
   preempts the visual banner immediately, without waiting for an animation.
5. **STALE**: replace current advice with the stale warning as in §6. The previous
   line becomes `LAST RADIO (STARTED)` and shows age. Resume only on a fresh live state.

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
| M3 race | Zone F fills: `PIT WINDOW L26–28`, `AHEAD P4 NORRIS +1.4 HARD · UC +1.2`, `BEHIND P6 LECLERC −2.1 OT`, one-line stint plan `M 1–26 → H 27–57`. SC/VSC paints the status bar. `fuel_delta_laps` from server. Damage `box for wing` rows turn red | F, A, D |
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

1. **Tokens + grid + freshness.** Restructure `index.html`/`style.css`. Bring the
   client-side 1 s timeout, `CONNECTING…` and the red stale warning to `app.js` in
   the same PR. Avoid shipping a new banner that could appear live when frozen.
   Verify at 1920×1080 and 2560×1440, including long calls and lost WS/UDP.
2. **Tyres 2×2 + fuel + evidence.** Add `app.js` render functions per zone. Add plan-view
   tyre tiles, remaining fuel laps, WS update rate. Hide the target delta and
   call-evidence strip until the backend supplies them. Unit-testable pure
   formatters can live in `web/format.js`.
3. **Current + previous radio + log.** Add the state machine from §7, an age counter,
   ≤220 ms handoff, reduced motion, next older call at the top of the log,
   and protocol-mismatch state. Restored calls without outcome/timestamp display
   unknown status/age. Test rapid consecutive calls, P1 preemption, cancellation,
   stale during transition and long-text clipping.
4. **Call history status contract.** In a separate server/protocol PR, enrich the
   existing `snapshot.calls[]` entries with original dispatch timestamps and
   `spoken`/`cancel` outcomes, retaining stable call IDs. Add confirmed
   finish/interruption events only if the product needs them. Keep current clients
   compatible or version the protocol. Verify replay and reconnect recovery.
5. **Damage + server-computed fuel delta.** Render `damage` once the owner's
   server change lands in zone D; add `fuel_delta_laps` after its target has a
   defined session-aware source. Do not compute a race-only guess for quali/practice;
   absent/unknown delta leaves fuel laps remaining prominent.
6. **`/radio` compact.** Add `body.radio` rules to the new grid. Test on a phone in portrait.
7. **M3 strategy zone.** Add it when `strategy` exists in the payload. Use a separate PR series.

Definition of done: screenshots at both landscape sizes and portrait `/radio`,
lint/type checks for any changed Python, and an offline `pitwall replay --serve`
render check against a test recording. No running game is needed.

## 10. Open questions for the owner

1. **Fuel delta target.** Which backend strategy target owns `fuel_delta_laps` in
   race, quali and practice? Until defined, display only laps remaining. In the
   illustrative live mockup, lap 24/57 has 34 laps including the current lap;
   34.4 remaining gives a future server delta of +0.4. In the M3 mockup,
   lap 31/52 has 22 laps including the current lap; 21.7 gives −0.3.
2. **Damage thresholds.** Amber 10 % / red 30 % are guesses. Should these come from
   `thresholds` in config like the tyre temps, and should the server emit a
   `status` word per component the way it does for tyres?
3. **Log order.** Should the log be newest-first, which keeps the top row next to the
   banner, or newest-last, like the current chat style?
4. **Banner fade times.** Should banners fade after 20 s or 30 s, or follow the rule's `deadline_ms`?
   Expiration of an actionable P1 needs a distinct rule from its visual fade.
5. **Tyre tile colour source.** Should the server's `status` (COLD/OK/HOT from config)
   set the colour, or should wear set a second colour band? Proposal: temp colours the
   number, wear colours
   only its own `38%` text.
6. **Brake threshold.** Brakes appear small inside each tyre tile and remain
   neutral for now; what calibrated threshold should turn them amber?
7. **SC/VSC.** Should the status bar only show SC/VSC, or should `safety_car != 0` also
   force a synthetic banner if the dispatcher has not sent a P1?
8. **Should `rate_hz` be populated** by the server (it is `null` today) or is a client
   derived number acceptable?
9. **1440p only?** If the second monitor is known, the `clamp()` scaling can be dropped
   for fixed pixel sizes.

## 10. Pages (M3)

The live screen has one layout, and the client never reads `page` or `pages` from the
payload. UDP Action 4, `P`, the menu page item and the voice page intent still reset
the backend page to the first entry of `ui.pages` (`race`) and log a `page` record, but
nothing on screen changes. A named `{"type":"page","name":...}` message still sets the
backend page. `ui.auto_page` (off by default) and the `car` and `track` entries in
`ui.pages` stay in the config but no longer change the screen. Only `pages[0]` matters,
as the reset target.

The pit board, cool-down and qualifying takeovers come from payload fields
(`pit_board`, `quali`, the cool-down flag), not from `page`. Plan C
(`kind: "reactive"`) shows its BOX NOW card only while `safety_car` is 1 (SC) or 2
(VSC), or while plan C is the active plan.

`/radio` keeps its compact layout. Its page pill is the static text RACE, and clicking
it sends the same page reset.

State frames carry the engine's processed snapshot (`engine.last_snapshot`, after the
battle, plan and setup-advisor passes), falling back to the raw snapshot before the
first tick.

Payload: `strategy` (null outside races), `track_info`, `setup` (null until a setup packet).


## 11. Motion (M3)

Motion marks a change and then stops. Nothing loops, pulses or scrolls, and every
number and word is readable in its final state on the first frame.

| Event | Motion | Duration |
|---|---|---|
| New call | priority bar stretches back (P1: three white flashes) and a light sweep crosses the banner; text is instant | 700–900 ms |
| Call age | 3–4 px hairline under the banner shrinks to the fade time for that priority | the call's lifetime |
| Radio log row (`/radio` only) | drops in from the banner side with a brief tint | 700 ms |
| ACK, NEG, new call, voice heard or missed | one edge glow on the screen border | 500 ms |
| Voice listening | steady lilac edge, no pulse | while listening |
| Tyre status flip | one ring pulse in the new colour | 700 ms |
| Position change | number lifts green (gain) or drops red (loss) | 1.6 s |
| Lap change | lap counter flashes | 900 ms |
| Battle gap | marker on a 0–3 s rail glides to the new gap, and the 1.0 s overtake range is shaded | 600 ms |
| SC/VSC, red flag, stale | static diagonal stripes on the status bar | while true |

Animations run off timestamps, not the render tick, so the 4–5 Hz re-render never
restarts one. On phones in portrait the status bar and banner are sticky while the
page scrolls, with notch-safe insets, and the page holds a screen wake lock where the
browser allows. A P1 vibrates once on phones that support it. `prefers-reduced-motion:
reduce` cuts every animation and transition to a single frame and hides the age hairline.

## 12. Glance rules (M4 polish)

Written for a driver who has never read this page.

- **Contrast.** Key values use `--fg`. Supporting text uses `--soft` (#c8c8d0). `--dim` is for diagnostics only (lag, update rate, surface and brake temps).
- **Words over codes.** `WEAR 4%`, `14 laps`, `UNDERCUT`, `2 WARNINGS`, `spare at finish`, `YOU GAIN 0.25s/lap` / `THEY GAIN …`. No `P1`, `UC`, `WS/s`, `L old`.
- **Icons beside words, never alone.** ▲ ahead, ▼ behind, ⚠ warning, ✓ fine, ✗ failed, ⚡ battery, ◷ wait. Only symbols in common monospace/system fonts; no colour emoji.
- **Position** is a white pill in the status bar; the tyre tile puts temperature and state (`103° OK`) on one line.
- **Responsive.**

| Viewport | Layout |
|---|---|
| ≥ 861 px tall, landscape | Fixed grid, no scroll |
| 501–860 px tall, landscape (1366×768, 1024×768) | Same grid; zones keep a minimum height; page scrolls under pinned status + banner |
| ≤ 1100 px wide, landscape | Status bar wraps |
| ≤ 600 px wide (phones) | `body.phone`: one column with status, banner, context cards, tyres and ARM PHONE RADIO |
| ≤ 500 px tall, landscape | Compact scroll, one-line banner |
