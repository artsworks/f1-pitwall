# Race-end and formation-lap radio

## Sources and cadence

Real engineers separate preparation, the last racing lap, the road result and cooling.
Game telemetry does not expose every real procedure. Historical switch instructions are not game instructions.

| Source | Observed wording or sequence | Application |
| --- | --- | --- |
| [RaceFans, 2014 Bahrain radio](https://www.racefans.net/2014/04/08/mercedes-fair-fight-hamilton-rosberg/) | During formation, Bonnington says "front brakes need a bit of work" after a tyre update. Rosberg receives axle temperatures. | Give a measured temperature and the preparation action, before the grid. |
| [RaceFans, 2017 Russian GP transcript](https://www.racefans.net/2017/05/05/2017-russian-grand-prix-team-radio-transcript/) | Bottas asks for less talking over the last laps. | Keep the existing single final-lap family. Do not add another countdown. |
| [RaceFans, Ricciardo's 2022 Mexican GP transcript](https://www.racefans.net/2022/11/03/i-was-in-the-fight-which-hasnt-been-often-this-year-ricciardos-mexican-gp-transcript/) | The last-lap call names Ocon and his gap. At the flag, Stallard gives P7 and the rear gap, then requests cooling. | Report the road result first, then cooling. Confirm official results when their packet arrives. |
| [Mercedes, hitting the brakes in Monza](https://www.mercedesamgf1.com/news/insight-hitting-the-brakes-in-monza) | Discs cool towards 200 degrees on straights and exceed 1,000 under braking. Temperature varies rapidly. | Use current brake telemetry. The 200-degree preparation threshold is a conservative default, not a universal optimum. |
| [Mercedes, 2025 Italian GP debrief](https://www.mercedesamgf1.com/news/2025-italian-gp-f1-akkodis-race-debrief-what-happened-to-kimi-at-the-start) | The team reviews Kimi's difficult start and tyre choices. The page does not supply a transferable launch setting. | Do not infer clutch, fuel or ERS targets from a real team's start. |
| [McLaren, start-light explainer](https://www.mclaren.com/racing/formula-1/2025/another-seven-f1-questions-youve-been-afraid-to-ask) | The starter arms the light sequence after the cars return to their grid positions. | Stop formation reminders when STLG reports lights. Do not talk through the launch. |

The Mexican transcript's cooling request includes "high speed and big lift-offs" after the position and gap.
Pitwall keeps the cooling reminder but does not prescribe speed or copy team-specific switches.
Tyre, brake and grid calls occupy separate parts of formation to avoid a single burst.

## Telemetry audit

The [EA format-2026 specification](https://forums.ea.com/t5/s/tghpe58374/attachments/tghpe58374/f1-games-game-info-hub-en/61/10/Data%20Output%20from%20F1%2025%202026%20Season%20Pack%20(Season%208).pdf)
defines Final Classification, including `m_resultReason` before the best-lap field.
The [community mirror](https://github.com/MacManley/f1-26-udp#final-classification-packet) supplies the same 47-byte car structure.
The parser now supports its 24 rows, count byte and 29-byte header, totalling 1,134 bytes.
EA and the mirror differ on publication cadence. The rules assume neither cadence and speak once per session.

| Required signal | Packet and wire field | Parser or Snapshot | Decision |
| --- | --- | --- | --- |
| Formula and session | Session `m_formula`, `m_sessionType` | `formula`, `session_type`, `session_kind` | Formation requires modern F1, formula 0, and a race session. The wire name is `m_formula`, not `formulaType`. |
| Formation configured | Session `m_formationLap`, `m_formationLapExperience` | Both parsed. `formation_lap_enabled` exposes the first. | Configuration alone does not prove the car is on formation. |
| Formation active | Session `m_safetyCarStatus`, value 3 | `safety_car_status`, existing `race_phase` | Require status 3 and phase `formation`, plus configuration. Lap zero alone is insufficient. |
| Driving or finished | Lap Data `m_driverStatus`, `m_resultStatus` | `driver_status`, new `result_status` | Result 3 confirms the player's finish. Values 4 through 7 never trigger finish or cooling calls. |
| Session boundaries | Event `SSTA`, `SEND` | Existing event handling and `session_ended` | SEND suppresses cooling. SSTA does not imply lights out or a completed race. |
| Start lights | Event `STLG.m_numLights`, `LGOT` | New `start_lights`, existing `lights_out` | Suppress formation messages once lights begin. LGOT ends formation. |
| Chequered flag | Event `CHQF` | Existing `chequered`, race phase state machine | This can precede the player's finish. Require the player's result status before reporting their road result. |
| Tyre heat | Car Telemetry `m_tyresInnerTemperature[4]` | `tyre_inner`, EMA `coldest_tyre_c`, `coldest_tyre` | Reuse compound-specific cold thresholds. Reject zero measurements and stale sources. |
| Brake heat | Car Telemetry `m_brakesTemperature[4]` | `brake_temp`, new `brake_min_c`, `brake_max_c` | Report the coldest brake during preparation and hottest brake after finishing. |
| Grid slot | Lap Data `m_gridPosition` | Existing `grid_position` | Remind the driver in sector 3. GAP: no distance or alignment to the physical grid box. |
| Road result | Lap Data `m_carPosition`, `m_gridPosition` | Existing position and positions gained/lost | Extend existing finish rules. Label this result "on the road" or "on track", not official classification. |
| Road gaps | Lap Data `m_deltaToCarInFrontInMSPart`, `m_deltaToCarInFrontInMinutesPart` | Existing decoded delta for the player and next car | Latch positive gaps when the player finishes. Require adjacent positions, matching lap numbers and active or finished status. Do not use strategy projections. |
| Official result | Final Classification `m_numCars`, `m_position`, `m_gridPosition`, `m_resultStatus` | New parser, `final_position`, `final_details` | Report confirmed position and grid changes. Ignore inactive rows and non-finishers. |
| Official gaps | Final Classification `m_totalRaceTime`, `m_penaltiesTime`, `m_numLaps`, `m_position` | `final_details` | Compare adjacent classified finishers with equal lap counts, including penalties. Omit non-finite or non-positive totals and gaps. |
| Fastest lap event | Event `FTLP.m_vehicleIdx`, `m_lapTime` | Existing fastest-lap state | Retain existing racing calls. Append the player's current fastest lap to their road result when known. |
| Final fastest lap | Final Classification `m_bestLapTimeInMS` | `final_details` | Include the player's time only when it is the unique minimum across non-zero best laps. GAP: tied times do not identify the award winner. |
| Fuel and ERS | Car Status `m_fuelMix`, `m_fuelInTank`, `m_fuelRemainingLaps`, `m_ersDeployMode`, `m_ersStoreEnergy` | Already parsed, fuel and ERS Snapshot facts exist | GAP: current state is not a prescribed launch mode or target. Do not invent switch instructions. |
| Clutch and launch | Car Telemetry `m_clutch`, `m_engineRPM`, current inputs | Current clutch and RPM parsed | GAP: no target bite point, launch RPM or start-assist state. Do not tell the driver to hold or release a control. |
| Cool-down lap | Finished result, moving Car Telemetry and driving Lap Data | `finish_elapsed_s`, speed and brake temperatures | Infer only post-finish movement. GAP: no dedicated manual cool-down phase or parc-ferme instruction. Suppress at SEND, stationary telemetry or official results. |

Restricted or missing measurements remain silent. No call requests overtake mode or active aero.
Pitwall does not add a driver menu, voice intent, external process or LLM dependency.

## Rules and thresholds

Formation calls belong in `race_events.yaml` because they describe race-only transitions.
Finish additions belong beside the existing `race_finish.yaml` calls, not in `shared.yaml`.
The existing final-lap family and its shared cooldown remain unchanged.

| Rule | Timing and condition | Threshold or limit |
| --- | --- | --- |
| `formation_tyres_cold` | Moving formation car with a measured cold tyre | Existing compound-specific inner-temperature thresholds, once per stint |
| `formation_brakes_cold` | Sector 2 of formation, measured positive coldest brake | `formation_brakes_cold_c: 200`, once per stint |
| `formation_grid_slot` | Sector 3 of formation, known grid position | `formation_min_speed_kmh: 10`, once per stint |
| Existing `last_lap*` | Final scheduled lap, sector 1 | Existing fight threshold, one shared cooldown |
| Existing `finish_*` | Player result 3, before official classification | Road position and latched gaps, preserving existing win and podium calls |
| `finish_classified` | A valid finished player row in Final Classification | One confirmation, no lap-data freshness dependency |
| `race_cool_down` | Moving finished player, before SEND or classification | `race_cool_down_delay_s: 8`, `race_cool_down_min_speed_kmh: 10`, once per stint |

New calls use priority 2 with informational urgency, below safety, pit strategy and battle execution.
Priority 3 requires sustained full throttle, which conflicts with formation preparation and post-finish cooling.
The dispatcher retains its existing safety and strategy precedence. No priority policy changes are necessary.
The speed gates avoid instructions while parked. The cooling delay separates cooling from the flag result.
These defaults need real-drive validation, not a claim of calibrated thermal optima.

## Phrasing examples

Each new rule has three variants. ADR 0008 supplies deterministic rotation without an LLM.
New spoken numbers use one decimal. Missing gaps or grid changes produce no placeholder or zero claim.

| Call | Example |
| --- | --- |
| Tyres | "Rear left at 60.0 degrees. Work the tyres before the start" |
| Brakes | "Brakes need heat, lowest 150.0 degrees" |
| Grid | "Your grid slot is P6.0 for the start" |
| Road finish | "Flag. P4.0 on the road, up 2.0 from the grid. 1.2 to the car ahead. 2.4 clear behind." |
| Penalty projection | "Crossed P4.0. Provisional P5.0 with the penalty." |
| Confirmed result | "Confirmed P3.0. Up 3.0 from the grid. 1.2 to the car ahead. 2.4 clear behind. Fastest lap, 80.2 seconds." |
| Cooling | "Race complete. Hottest brake 450.0 degrees. Cool the car" |

The confirmation may correct the road result after penalties. It is not another chequered-flag celebration.
The fastest-lap report does not claim an extra championship point.

## Replay evidence and limits

`tests/test_race_end.py` replays synthetic format-2026 packets through Ingest, SessionState and the rules.
The scenario test uses the full engine, compares maximum speed with 10x replay and checks the YAML expectations.
The fixture emits packets at 4 Hz, below the freshness limits for Lap Data and Car Telemetry.
Tests cover cold and hot temperatures, missing or stale measurements, lights and non-race sessions.
Finish coverage includes early CHQF, retirement, delayed classification, penalties, lapped gaps, repeated packets, flashback and session reset.

The source for `scenarios/brazil-race-end.yaml` is synthetic, with a fixed recording header and SHA-256.
To generate that source locally, run:

```sh
uv run python -c 'from pathlib import Path; from tests.test_race_end import write_race_end_recording; write_race_end_recording(Path("/tmp/brazil-race-end.f1bin"))'
```

No recording belongs in git. The scenario remains a target until an owner validates a real drive.
Real recordings are unavailable in this session. A bench invocation without the generated fixture will skip this source.
Do not run the full bench or update a baseline to hide that limit.

## Owner follow-up

No owner decision blocks these deterministic calls.
Validate the conservative brake threshold and formation timing with a future recording that includes formation and the results screen.
Keep unsupported launch instructions silent unless EA exposes the required targets or the owner supplies a separate explicit contract.
