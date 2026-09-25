# Ingestion

All field names and frequencies below are from the format-2026 structures; see
`reference/f1-26-udp-notes.md` for the extracted details and the source.

## Game configuration

Settings → Telemetry Settings:

| Setting | Value | Note |
|---|---|---|
| UDP Telemetry | On | |
| UDP Broadcast Mode | Off + explicit IP, or On | Off with the backend machine's IP is more reliable on a busy LAN |
| UDP IP Address | backend machine | ignored when broadcast is on |
| UDP Port | 20777 | |
| UDP Send Rate | 30 Hz | Game default is 20 Hz (`sendRate="20"` in the config XML). Menu options have historically been 10/20/30/60 Hz — confirm in F1 26. 30 Hz is the recommended start; 20 Hz is enough for everything except fine thermals |
| UDP Format | 2026 | **Required.** The parser supports F1 26 / format 2026 only |
| Your Telemetry | your choice (it only affects what *others* see) | Assume friends stay Restricted: their fuel, ERS, brake bias, tyre wear, damage and tyre sets read as zero; compound, tyre age and lap times remain. Yours is always visible to you |

Host gotchas: Windows Firewall needs an inbound rule for UDP 20777 (and TCP 8000 for the
phone). If the backend is on a different machine, the game PC and phone must be on the
same subnet with AP isolation off.

## Supported game: F1 26 only

The parser is locked to **F1 26, packet format 2026**. The header check accepts a packet
only when `m_packetFormat == 2026` and `m_gameYear == 26`; anything else is counted and
dropped, and the dashboard shows "unsupported telemetry format — set UDP Format to 2026".
This keeps one set of layout tables, no per-version branches, and fixed struct sizes that
can be asserted at import time. `m_packetVersion` per packet id is also checked against
the expected value, so a mid-season patch that changes a layout fails loudly instead of
mis-parsing. Supporting another year later means adding a second table set, not
branching the parser.

## Send-rate independence

The in-game send rate is a user setting (10–60 Hz; we assume 30 Hz). No calculation depends on it:

- **Time comes from the game.** EMAs, closing rates, deg slopes and deadlines use
  `m_sessionTime` deltas, never packet counts. An EMA is defined by its time constant
  (fast 3 s, slow 30 s), and its per-sample weight is `1 − exp(−Δt/τ)`, so 20 Hz and 60 Hz
  converge to the same value.
- **Lap-level data doesn't care.** Lap times, sector times, pit loss, wear per lap and
  fuel per lap come from Lap Data/Session History/Car Damage and are identical at any rate.
- **Staleness is in seconds.** "Stale" means no update in *N seconds*, scaled to
  the packet's expected period. Round-robin packets (Session History, Tyre Sets) are
  fixed at 20 Hz regardless of the menu rate.
- **The rate is measured.** The ingest layer computes the observed rate per packet
  type over a 5 s window. The configured rate is only used for the health check ("expected
  30 Hz, seeing 20 Hz — check the UDP Send Rate setting") and the recording-size estimate.
- **The engine tick is fixed at 10 Hz** and samples the latest state, so a higher UDP rate
  only makes each snapshot fresher, not more frequent.
- **Low rates degrade gracefully.** At 10 Hz, fine thermal trends and distance-to-corner
  gating get coarser; the doctor warns but does not refuse.

The rate is chosen before starting the backend: `pitwall start --send-rate 30`, the
`connection.send_rate_hz` setting, or the tray menu. A mismatch with the observed rate is
a warning, never an error. Recordings store the observed rate, so replays of 20 Hz and
60 Hz sessions behave the same.

## Packets consumed

| Packet | ID | Rate | What we take it for |
|---|---|---|---|
| Motion | 0 | menu rate | lateral/longitudinal g (quantised, ÷1000) for tyre-loading context; optional |
| Session | 1 | 2 Hz | track, session type, `m_totalLaps`, `m_sessionLength`, safety car status, weather + 64 forecast samples, DRS zones, active-aero zones, track temp |
| Lap Data | 2 | menu rate | position, `m_lapDistance`, deltas (split ms/minutes parts), sector times, `m_pitStatus`, `m_driverStatus`, penalties, warnings, pit-lane timers, result status |
| **Event** | 3 | on occurrence | `SCAR`, `DRSE`/`DRSD`, `FLBK`, `RDFL`, `PENA`, `RTMT`, `COLL`, `SPTP`, `STLG`/`LGOT`, `CHQF`, `SEND` |
| **Participants** | 4 | 0.2 Hz | driver names, teams, AI vs human — radio calls name rivals |
| Car Setups | 5 | 2 Hz | front/rear wing, diff, brake bias for pre-pit adjustment advice |
| Car Telemetry | 6 | menu rate | `m_tyresSurfaceTemperature[4]`, `m_tyresInnerTemperature[4]`, brake temps, speed, throttle/brake, `m_drs` |
| Car Status | 7 | menu rate | fuel (`m_fuelInTank`, `m_fuelRemainingLaps`, `m_fuelMix`), ERS (store, deploy mode, harvested MGU-K/H, `m_ersHarvestLimitPerLap`, `m_ersDeployedThisLap`), `m_actualTyreCompound` / `m_visualTyreCompound`, `m_tyresAgeLaps`, `m_drsAllowed`, `m_drsActivationDistance`, `m_frontBrakeBias`, FIA flags |
| **Final Classification** | 8 | 5 s on results | trigger the debrief |
| **Car Damage** | 10 | 10 Hz | **`m_tyresWear[4]`**, `m_tyreBlisters[4]`, wing/floor/engine damage |
| **Session History** | 11 | 20 Hz, round-robin | every car's lap and sector times, stint compounds → rival pace and deg |
| **Tyre Sets** | 12 | 20 Hz, round-robin | available sets, wear, `m_lifeSpan`, `m_usableLife`, `m_lapDeltaTime`, fitted index |
| **Car Telemetry 2** | 16 | menu rate | `m_activeAeroMode` (0 = corner, 1 = straight), availability + activation distance, Overtake Mode available/active/activation distance, `m_2026Regulations` |

Bold rows are additions to plan v1. Motion Ex (13) and Lap Positions (15) are not needed
initially.

Round-robin caveat: Session History and Tyre Sets deliver **one car per packet**. Each
car's slot therefore needs its own last-updated timestamp, and consumers must treat data
older than ~2 s as stale rather than assuming a synchronous 24-car view.

## Parser

Layouts are declared as data, one entry per (`m_packetId`,
`m_packetVersion`) for format 2026, and compiled at import time into `struct.Struct` objects plus field
maps. Rules:

- Dispatch on the header triple. An unknown triple is counted and dropped with a single
  warning — never parsed with a nearest-match layout.
- Validate `len(datagram) == expected_size` for the matched layout and reject mismatches;
  this catches spec drift immediately instead of silently shifting every field.
- Parse into preallocated slotted objects with `unpack_from`; no per-packet dicts.
- Decode split time fields (`*_MSPart` + `*_MinutesPart`) into plain milliseconds at the
  parser boundary so no downstream code has to remember.
- Enums (session type, track, compound, flags, event codes) live in one module generated
  from the same tables.

## Pause, flashback and red flag

Three distinct events, handled distinctly:

| Event | Detection | Action |
|---|---|---|
| Flashback | `FLBK` event, corroborated by `m_frameIdentifier` going backwards while `m_overallFrameIdentifier` keeps increasing | Roll session state back to the last lap boundary, discard EMA windows, drop queued calls, suppress all output for 2 s |
| Pause / stall | No packets for > 500 ms, or `m_sessionTime` not advancing | Freeze timers and EMAs; mark the UI stale; do not speak |
| Red flag | `RDFL` event | Freeze strategy, announce once, wait for restart; invalidate the in-progress lap |
| Network pause (MP) | `m_networkPaused` | Treat as pause for that car only |

`m_overallFrameIdentifier` is the reliable monotonic clock across flashbacks; prefer it to
`m_sessionTime` comparisons, which also regress on out-of-order UDP delivery.

## Recording format

`.f1bin`: a small header (format version, game version, session UID, wall-clock start),
then repeated `[uint32 offset_us][uint16 length][bytes]`. From the format-2026 packet sizes,
roughly **0.85 GB per hour at 30 Hz** (≈ 0.6 GB at 20 Hz, ≈ 1.5 GB at 60 Hz). Menu-rate
packets dominate: Motion, Lap Data, Car Telemetry, Car Status, Motion Ex and Car Telemetry 2
are ~6.2 KB per frame. Recordings are written raw during the session and compressed
(zstd) at low priority when the session closes; expect several-fold reduction, to be
measured at M0.

`tools/replay.py` streams a recording into the ingest path at 1×, N×, or as fast as
possible, optionally seeking to a lap. Tests use the same code path with a deterministic
clock.
