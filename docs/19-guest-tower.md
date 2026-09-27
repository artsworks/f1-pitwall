# Guest timing tower (`/tower`)

Status: design only. Not built. Owner decision (M3): write up now, build later.

A second screen for people watching the race from the garage: friends, family, guests
on a laptop across the room. It shows the whole field, sectors and pace, and tells the
story of our race in plain words. It never talks to the driver and never changes anything
on the driver's side.

## 1. Why a separate route, not a dashboard page

`15-dashboard-design.md` bans a 22-car timing tower from the driver's screen because it
fails the glance test at 300 km/h. Guests have the opposite problem: they have time,
no context, and want the TV picture. So this is a separate route, `/tower`:

- Not in the Action 4 page cycle and not affected by `ui.auto_page`.
- Read-only socket: the server ignores `mindset`, `page`, `ack` and control messages
  from `/tower` clients.
- Its own `timing` payload, sent at 1 Hz (the tower doesn't need 5 Hz), so the
  driver's `state` frames are unchanged.
- A mouse and keyboard are allowed (hover or click a row for details); the rule against
  touching the screen applies only to the driver.

## 2. What guests want, in order

| # | Question a guest asks | Answer on screen |
|---|---|---|
| 1 | Where's our car? | our row pinned and highlighted: position, interval, gap to leader, places gained/lost since the start |
| 2 | What's happening? | header: lap X/Y, race phase, SC/VSC/red flag, weather now and the next forecast change, fastest lap (driver, time) |
| 3 | What's the order? | every car: position, team colour, name, interval, gap to leader, last lap, best lap, tyre + age, stops, status tags (IN PIT, OUT LAP, PEN +5s, DNF) |
| 4 | Who's quick right now? | sectors S1/S2/S3 in TV colours: purple session best, green personal best, yellow otherwise |
| 5 | Who's faster over a stint? | 5-lap sparkline + average pace, and an arrow against the car ahead |
| 6 | What's our plan? | strategy strip in plain words from the engine: "One stop, window L26–28. Norris pitted L24, we're covering." |
| 7 | What just happened? | events feed: overtakes, stops, penalties, fastest laps, SC, retirements |

Row 6 is what only our pitwall can offer. The engine already knows the pit window, the
relevant rivals and the undercut threat (`18-race-engine.md`), so the tower explains
them the way a TV commentator would.

## 3. Data sources

Everything comes from the packets the engine already parses. No new ingestion.

| Field | Source | Restricted telemetry? |
|---|---|---|
| position, interval, stops, pit/result status, penalties, warnings | Lap Data (`CarLap`: `car_position`, `delta_to_car_in_front_ms`, `num_pit_stops`, `pit_status`, `result_status`, `penalties`) | available |
| gap to leader | Lap Data delta to leader, or summed intervals | available |
| S1/S2, current lap | Lap Data `sector1_ms`, `sector2_ms`, `current_lap_time_ms`; S3 = lap − S1 − S2 | available |
| last/best lap and per-lap sectors | Session History (one car per packet, cycled), `laps` table for our car | available |
| name, team, human/AI | Participants | name may show as the player tag online |
| tyre compound and age | Car Status visual compound + tyre age | available |
| tyre wear, fuel, ERS | Car Status / Car Damage | **hidden for other cars** under restricted telemetry: show nothing, not zeros |
| events | Event packet: `OVTK`, `FTLP`, `PENA`, `DTSV`, `SGSV`, `RTMT`, `SCAR`, `RCWN`, `COLL` | available |
| our plan and battle | `strategy` payload (`18-race-engine.md`) | own-car plan is exact; rival parts carry the engine's confidence |
| weather forecast | Session forecast samples | available |

Places gained comes from the grid position (Final Classification / Lap Data grid
position) and is stored in the `sessions` row so the number stays correct after a
recovery restart. The sparklines use stored `laps` for our car and the Session History
buffer for the others. No new tables are needed for v1.

## 4. `timing` payload (sketch)

```json
{
  "type": "timing",
  "lap": 18, "total_laps": 50, "phase": "racing", "sc": null,
  "weather": {"now": "light_cloud", "next": {"in_min": 10, "kind": "light_rain", "pct": 40}},
  "fastest": {"idx": 4, "name": "NORRIS", "ms": 91234, "lap": 16},
  "session_best": {"s1": 28123, "s2": 33010, "s3": 30101},
  "cars": [
    {"idx": 0, "pos": 5, "grid": 8, "name": "ARTHUR", "team": 3, "ours": true,
     "interval_ms": 1400, "leader_ms": 12800,
     "last_ms": 91800, "best_ms": 91500, "s": [28300, 33200, null],
     "s_flag": ["pb", "", null], "pace5_ms": [91900, 91850, 91800, 91820, 91800],
     "tyre": {"compound": "M", "age": 12}, "stops": 0,
     "tags": []}
  ],
  "story": {"plan": "One stop, window L26–28", "battle": "Norris +1.4, closing 0.2/lap",
            "calls": [{"t": 1234.5, "text": "Box this lap"}]},
  "events": [{"lap": 17, "kind": "OVTK", "text": "Leclerc passes Sainz for P6"}]
}
```

Absent fields hide, never error (same rule as `15-dashboard-design.md` §5).

## 5. Layout (landscape laptop, 1366×768 to 1920×1080)

```
┌ L18/50 · RACING · ☁ rain 40% in 10 min · FASTEST NORRIS 1:31.234 L16 ────────┐
├───────────────────────────────────────────────────────┬──────────────────────┤
│ P  DRIVER     INT    GAP    LAST     S1   S2   S3  TYRE STOPS │ OUR RACE      │
│ 1  VERSTAPPEN  —      —     1:31.4   ■    ■    ■   H 20   1   │ P5  ▲3 from P8│
│ 2  NORRIS    +1.2   +1.2    1:31.2   ■    ■    ■   M  2   1   │ One stop,     │
│ …                                                             │ window L26–28 │
│ 5  ARTHUR    +1.4  +12.8    1:31.8   ■    ■    ·   M 12   0   │ Norris pitted │
│ …  (all rows fit: row height = (100vh − header − footer)/cars)│ L24: covering │
│                                                               ├──────────────┤
│                                                               │ EVENTS        │
│                                                               │ L17 Leclerc P6│
└───────────────────────────────────────────────────────────────┴──────────────┘
```

- Our row: highlighted background, never scrolled away; if the list overflows, the
  tower compresses row height before it scrolls.
- Font sized for 3 m viewing: `clamp(14px, 1.3vw, 28px)`; names shortened to 3-letter
  codes below 1280 px width.
- Colour always paired with a word or shape (purple/green/yellow sectors also carry a
  `●`/`▲`/`·` glyph), matching `15-dashboard-design.md` §4.

## 6. Motion

Same rules as `15-dashboard-design.md` §11, tuned for spectators:

- Row reorder: rows slide to the new position (FLIP technique) in 600 ms, and the
  car that gained flashes green once. This is how guests see overtakes.
- Intervals update once per lap per car, at the line, not every tick, so numbers
  can be read.
- Sector cells fill in as each car completes them; a new purple flashes once.
- Events feed: a new item drops in at the top; the feed keeps 8 items.
- `prefers-reduced-motion: reduce`: rows jump, nothing flashes.

## 7. Guardrails

- **Radio delay.** The calls ticker shows a call only after its audio started, plus
  `tower.call_delay_s` (default 5 s), so a guest never reads a call before the driver
  hears it, and the ticker cannot spoil a plan to someone on the same voice channel.
  `tower.show_calls: false` hides it.
- **Privacy in leagues.** `tower.show_rival_detail` (default `true` offline, `false`
  in online sessions) controls pace sparklines and the strategy story's rival names.
- **No load on the driver path.** The `timing` payload is built at 1 Hz from the
  latest snapshot; building it must stay under 2 ms (`09-performance.md` budget) and
  it is skipped when no `/tower` client is connected.
- **Stale.** Same >1 s rule: the whole tower greys and shows `STALE` in the header.

## 8. Config (sketch)

```yaml
tower:
  enabled: true
  hz: 1
  call_delay_s: 5
  show_calls: true
  show_rival_detail: auto   # auto = true offline, false online
  events_kept: 8
```

## 9. Build plan (later PRs)

1. `timing` payload + `/tower` route with the static table; unit tests on the
   payload with restricted and unrestricted fixtures.
2. Sectors, fastest lap, events feed from Event packets; replay test on the synthetic
   race (`tests/fixtures`).
3. Story strip from the `strategy` payload + delayed calls ticker.
4. Motion (row FLIP, sector flashes) and the render check at 1366×768 and 1920×1080.

## 10. Open questions

1. Should the tower also work in qualifying (sort by best lap, show the cut line) or
   races only? Qualifying needs a different sort order and a "provisional" tag.
2. Team colours: team id → colour table in config, or a neutral palette so it matches
   game liveries poorly but never wrongly?
3. Should guests be able to open the post-race debrief (`16-debrief-design.md`) from
   the tower after the chequered flag?
