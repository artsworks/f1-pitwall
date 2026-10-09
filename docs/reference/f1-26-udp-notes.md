# F1 26 UDP notes (packet format 2026)

Working notes extracted while reviewing plan v1. These are the facts the design depends
on. The authoritative source is EA's published specification; the 2026 structures are
also mirrored in community repositories such as `MacManley/f1-26-udp`. The game can
also emit formats 2025 and 2024; this project accepts only 2026.

**Always re-check against the official spec after a game patch.** `m_packetVersion` in
the header exists precisely because layouts change within a season.

## Header (29 bytes)

```c
uint16 m_packetFormat;            // 2026
uint8  m_gameYear, m_gameMajorVersion, m_gameMinorVersion, m_packetVersion, m_packetId;
uint64 m_sessionUID;
float  m_sessionTime;
uint32 m_frameIdentifier;         // rewinds on flashback
uint32 m_overallFrameIdentifier;  // does NOT rewind on flashback
uint8  m_playerCarIndex, m_secondaryPlayerCarIndex;  // 255 if no second player
```

Car arrays are 24 slots.

Packet sizes (bytes): Motion 1325 · Session 926 · Lap Data 1399 · Event 45 ·
Participants 1470 · Car Setups 1233 · Car Telemetry 1448 · Car Status 1445 ·
Final Classification 1134 · Lobby Info 1062 · Car Damage 1133 · Session History 1460 ·
Tyre Sets 231 · Motion Ex 273 · Time Trial 104 · Lap Positions 1231 · Car Telemetry 2 269.

## Packet IDs

| ID | Packet | Frequency |
|---|---|---|
| 0 | Motion | menu rate |
| 1 | Session | 2 Hz |
| 2 | Lap Data | menu rate |
| 3 | Event | on occurrence |
| 4 | Participants | every 5 s |
| 5 | Car Setups | 2 Hz |
| 6 | Car Telemetry | menu rate |
| 7 | Car Status | menu rate |
| 8 | Final Classification | every 5 s on the results screen |
| 9 | Lobby Info | 2 Hz in the lobby |
| 10 | Car Damage | 10 Hz |
| 11 | Session History | 20 Hz, **one car per packet, cycling** |
| 12 | Tyre Sets | 20 Hz, **one car per packet, cycling** |
| 13 | Motion Ex | menu rate |
| 14 | Time Trial | 1 Hz (Time Trial only) |
| 15 | Lap Positions | 1 Hz |
| 16 | Car Telemetry 2 | menu rate |

## Session types

0 unknown · 1–3 Practice 1–3 · 4 Short Practice · 5–7 Qualifying 1–3 · 8 Short Qualifying ·
9 One-Shot Qualifying · 10–12 Sprint Shootout 1–3 · 13 Short Sprint Shootout ·
14 One-Shot Sprint Shootout · **15 Race · 16 Race 2 · 17 Race 3** · 18 Time Trial.

## Track IDs (sparse — do not assume contiguity)

0 Melbourne · 2 Shanghai · 3 Sakhir · 4 Catalunya · 5 Monaco · 6 Montreal · 7 Silverstone ·
9 Hungaroring · 10 Spa · 11 Monza · 12 Singapore · 13 Suzuka · 14 Abu Dhabi · 15 Texas ·
16 Brazil · 17 Austria · 19 Mexico · 20 Baku · 26 Zandvoort · 27 Imola · 29 Jeddah ·
30 Miami · 31 Las Vegas · 32 Losail · 39 Silverstone (reverse) · 40 Austria (reverse) ·
41 Zandvoort (reverse) · 42 Madrid.

IDs 1, 8, 18, 21–25, 28 and 33–38 are absent from the F1 26 list.

## Session packet — fields we use

`m_weather`, `m_trackTemperature`, `m_airTemperature`, `m_totalLaps`, `m_trackLength`,
`m_sessionType`, `m_trackId`, `m_formula`, `m_safetyCarStatus` (0 none, 1 full, 2 virtual,
3 formation lap), `m_numWeatherForecastSamples` + `m_weatherForecastSamples[64]`,
`m_sessionLength` (0 none, 2 very short, 3 short, 4 medium, 5 medium long, 6 long,
7 full), `m_gameMode`, `m_ruleSet`, `m_timeOfDay`, sector start distances, DRS zones
(`m_numDRSZones`, `m_drsZones[4]`), active-aero zones (full and partial, max 8 each),
`m_startReactionTime`.

Each `WeatherForecastSample` carries the **session type it applies to**, a
`m_timeOffset` in minutes, weather, temperatures and rain percentage — filter to the
current session before using.

There is **no** `m_raceDistance` field.

## Lap Data — fields we use

Times are split into a milliseconds part and a whole-minutes part:
`m_sector1TimeMSPart` / `m_sector1TimeMinutesPart`, likewise sector 2,
`m_deltaToCarInFrontMSPart` / `MinutesPart`, `m_deltaToRaceLeaderMSPart` / `MinutesPart`.

Also `m_lastLapTimeInMS`, `m_currentLapTimeInMS`, `m_lapDistance`, `m_totalDistance`,
`m_safetyCarDelta`, `m_carPosition`, `m_currentLapNum`, `m_pitStatus` (0 none, 1 pitting,
2 in pit area), `m_numPitStops`, `m_sector`, `m_currentLapInvalid`, `m_penalties`,
`m_totalWarnings`, `m_cornerCuttingWarnings`, unserved penalty counts, `m_gridPosition`,
`m_driverStatus` (0 garage, 1 flying lap, 2 in lap, 3 out lap, 4 on track),
`m_resultStatus` (0 invalid, 1 inactive, 2 active, 3 finished, 4 DNF, 5 DSQ,
6 not classified, 7 retired), `m_pitLaneTimerActive`, `m_pitLaneTimeInLaneInMS`,
`m_pitStopTimerInMS`, `m_pitStopShouldServePen`, `m_speedTrapFastestSpeed`.

## Event string codes

`SSTA` `SEND` `FTLP` `RTMT` `DRSE` `DRSD` `TMPT` `CHQF` `RCWN` `PENA` `SPTP` `STLG`
`LGOT` `DTSV` `SGSV` `FLBK` `BUTN` `RDFL` `OVTK` `SCAR` `COLL`.

`SCAR` carries the safety car type and event type; `COLL` carries both vehicle indices
and a severity; `FLBK` carries the flashback frame identifier and session time.

## Car Status — fields we use

`m_fuelMix`, `m_frontBrakeBias`, `m_pitLimiterStatus`, `m_fuelInTank`, `m_fuelCapacity`,
`m_fuelRemainingLaps`, `m_drsAllowed`, `m_drsActivationDistance`, `m_actualTyreCompound`,
`m_visualTyreCompound`, `m_tyresAgeLaps`, `m_vehicleFiaFlags`, `m_enginePowerICE`,
`m_enginePowerMGUK`, `m_ersStoreEnergy` (J), `m_ersDeployMode` (0 none, 1 medium,
2 hotlap, 3 boost), `m_ersHarvestedThisLapMGUK`, `m_ersHarvestedThisLapMGUH`,
**`m_ersHarvestLimitPerLap`**, `m_ersDeployedThisLap`, `m_networkPaused`.

Compounds — actual: 16 = C5, 17 = C4, 18 = C3, 19 = C2, 20 = C1, 21 = C0, 22 = C6,
7 = inter, 8 = wet. Visual: 16 soft, 17 medium, 18 hard, 7 inter, 8 wet.

### Inner tyre temperature defaults

The dashboard and out-lap/overheat calls use **inner (core/carcass)** telemetry,
not surface temperature. Default cold/hot boundaries, in °C, are configured in
`config/defaults/thresholds.yaml` by **actual** compound; track overlays can
override one compound without changing the rest.

| Actual ID | Compound | Cold below | Hot above |
| --- | --- | ---: | ---: |
| 7 | Intermediate | 60 | 85 |
| 8 | Full wet | 50 | 80 |
| 16 | C5 | 70 | 90 |
| 17 | C4 | 75 | 100 |
| 18 | C3 | 80 | 105 |
| 19 | C2 | 85 | 115 |
| 20 | C1 | 90 | 115 |
| 21 | C0 | 90 | 125 |
| 22 | C6 | 65 | 85 |

These are conservative **call boundaries**, not verified game MFD colour
transition values. The [F1 26 compound grip table](https://simracingsetup.com/setups/f1-26-setups-pro/australia/)
places intermediate peak grip around 65–75°C and wet peak around 65°C; its
reported grip declines only gradually past those temperatures. It has no C0/C6
data. The [F1 25 tyre guide](https://simracingsetup.com/ea-sports-f1/f1-25-tyre-guide/)
places inter at 60/70/80°C, wet at 50/60/70°C, and C6 at 65/75/85°C
(min/optimum/max). These historical numbers are a cross-check, not proof that
F1 26 has unchanged thresholds. The user's recorded F1 26 practice had inner
temperatures of 67–74°C at the coolest wheel on inters (median 71°C), consistent
with their in-game green indication. The exact MFD transitions need a
simultaneous in-game MFD/UDP observation to measure; tune the boundaries from
that evidence rather than interpreting blanket temperatures or real F1 tyre
operating ranges as game telemetry.

Qualifying pressure advice and cool-lap hints use the same compound window:
the pressure target is inset by `pressure_window_inset_c` from each edge, and
the cool-lap hot limit is below the hot edge by `cool_tyre_hot_margin_c`. Those
offsets are configurable in YAML. Unknown compound IDs keep the existing
global fallback thresholds.

**Tyre wear is not here.**

## Car Damage (10 Hz) — where tyre wear lives

`m_tyresWear[4]` (percentage), `m_tyresDamage[4]`, `m_brakesDamage[4]`,
`m_tyreBlisters[4]`, wing/floor/diffuser/sidepod damage, `m_drsFault`, `m_ersFault`,
gearbox and engine damage, per-component engine wear (MGU-H, ES, CE, ICE, MGU-K, TC),
`m_engineBlown`, `m_engineSeized`.

## Car Telemetry 2 (ID 16)

```c
uint8  m_activeAeroMode;               // 0 = corner mode, 1 = straight mode
uint8  m_activeAeroAvailable;
uint16 m_activeAeroActivationDistance; // metres until available, 0 = not available
uint8  m_overtakeAvailable;
uint8  m_overtakeActive;
uint16 m_overtakeActivationDistance;
uint8  m_2026Regulations;              // 1 = 2026 regulations applicable
uint8  m_drivingWrongWay;
```

## Observed under 2026 regulations

These observations come from real F1 26 recordings from the Silverstone and Interlagos weekends, across all session types.

- `DRSE` and `DRSD` events never fired. `OVTK` fired for position changes, not overtake mode.
- Car Status `m_drsAllowed` and `m_drsActivationDistance` stayed at 0. Car Telemetry `m_drs` also stayed at 0.
- Session packets reported zero DRS zones and zero full and partial active aero zones. Zone timing comes from Car Telemetry 2 activation distances.
- `m_overtakeAvailable` changed at one detection point per lap. The points were about 5631 m at Silverstone and 3401 m at Interlagos.
- A gap of about 1.0 s or less turned `m_overtakeAvailable` on. The value held until the next detection.
- `m_overtakeActivationDistance` counted down from about 300 m. `m_overtakeActive` matched `m_overtakeAvailable` within one frame in every observed lap.
- `m_overtakeAvailable` was on most of the time in practice and qualifying. It never switched on in one sprint. The cause is unknown.
- `m_activeAeroMode` switched to 1 a median 0.07 s after each zone start in 98 of 99 zones across three races. This suggests the game switches the mode.
- `m_activeAeroAvailable` pulsed for one frame at each zone start. `m_activeAeroActivationDistance` counted down from about 249 m to each zone.
- `m_ersDeployMode` used only values 0 through 3, the documented range.
- `m_ersHarvestedThisLapMGUH` and MGU-H engine wear stayed at 0.

## Tyre Sets (ID 12)

Per set: actual and visual compound, `m_wear`, `m_available`, `m_recommendedSession`,
`m_lifeSpan` (laps left), `m_usableLife`, `m_lapDeltaTime` (ms versus the fitted set),
`m_fitted`. 20 sets per car (13 dry + 7 wet), plus `m_carIdx` and `m_fittedIdx`.

## Differential (Car Setups, ID 5)

The game exposes the diff only as two setup percentages, `m_onThrottle` and `m_offThrottle`
(uint8, % locking). There is no live diff-lock or torque-split channel. Observed in the
first full race recording:

- The values are rebroadcast when changed from the MFD mid-race, a few seconds after
  the change (bias 57 → 56 at 807 s, on-throttle 60 → 50 at 810 s; off-throttle stayed 25).
- MFD changes step in 10-point increments on on-throttle; off-throttle was not touched,
  so its in-race step is unconfirmed.

How the game interprets the percentage is not in the UDP spec; the setup-screen and
community convention (not verified against game internals) is:

| Setting | Higher % (more locked) | Lower % (more open) |
|---|---|---|
| On-throttle | better straight-line traction, more stable exits, more exit understeer | more rotation on exit, inside rear spins up |
| Off-throttle | more stable turn-in, less lift-off oversteer, more entry understeer | more rotation on entry and mid-corner |

Wheelspin is measurable from Motion Ex `m_wheelSlipRatio` (rear slip with high throttle).
In that race, rear spin samples (slip > 0.08, throttle > 0.7, 60–200 km/h) ran ~3–13 %
per lap before the change and ~4–30 % after; tyre age and traffic make it inconclusive
whether the lower on-throttle diff helped or hurt.

## Restricted telemetry

With "Your Telemetry: Restricted" (the default), other players see zeros for that car's
`m_fuelInTank`, `m_fuelCapacity`, `m_fuelMix`, `m_fuelRemainingLaps`, `m_frontBrakeBias`,
`m_ersDeployMode`, `m_ersStoreEnergy`, `m_ersDeployedThisLap`, harvest fields,
`m_enginePowerICE`, `m_enginePowerMGUK`; in Car Damage, all wing/floor/diffuser/sidepod,
engine and gearbox damage, **`m_tyresWear`**, `m_tyresDamage`, `m_brakesDamage`,
`m_drsFault` and per-component engine wear; and all of Tyre Sets. You always see your
own car in full. The setting belongs to each human player, so AI cars are unaffected
(to be confirmed from the per-car `m_yourTelemetry` flag in the first league recording).
Compound, `m_tyresAgeLaps`, lap and sector times remain visible for everyone.
