# Safety car and VSC restart calls

Research for the SC/VSC restart rules in `src/pitwall/config/defaults/rules/race_events.yaml`.
Pitwall already had the deploy, ending and green-flag calls. This note covers what real
engineers add around a restart and which of those calls the F1 26 telemetry supports.

## Sources

| Id | Source | What it shows |
|---|---|---|
| S1 | [RaceFans, Jeddah 2025 radio](https://www.racefans.net/2025/04/22/youre-the-effective-leader-full-radio-from-piastri-and-verstappens-jeddah-tussle/) | McLaren: "Safety Car, Safety Car. Delta positive." The first call names the procedure, then the delta. |
| S2 | [RaceFans, Jeddah 2022 radio](https://www.racefans.net/2022/03/30/transcript-how-verstappen-fought-leclerc-on-his-radio-and-on-the-track-in-jeddah/) | Red Bull: "Virtual Safety Car deployed Max. Target positive, dash positive." |
| S3 | [RaceFans, Baku 2022 radio](https://www.racefans.net/2022/06/16/transcript-why-norris-was-mistaken-about-ricciardos-one-lap-team-order/) | McLaren: "Virtual Safety Car ending, recharge off, recharge off. Top-left toggle for turn one, cold tyres." The ending call carries the tyre state. |
| S4 | [RaceFans, Monaco 2013 radio](https://www.racefans.net/2013/05/29/2013-monaco-grand-prix-team-radio/) | Mercedes: "We're targeting -0.3 on delta time this lap." Engineers speak the delta as a number. |
| S5 | [Formula Dream, Monza 2021 radio](https://www.formuladream.app/f1-interactive/tools/team-radio/2021/italian-grand-prix/race) | Alpine under the SC: tyre temperature management, brake temperatures "under control", what to expect "after the restart". |
| S6 | [Formula Dream, Styrian GP 2021 radio](https://www.formuladream.app/f1-interactive/tools/team-radio/2021/styrian-grand-prix/race) | Haas: "getting the brake temps down and the tire temps where we want them". |
| S7 | [RaceFans, Hamilton at the end of a VSC, 2019](https://www.racefans.net/2019/05/01/hamilton-didnt-make-a-mistake-at-end-of-vsc-period/) | Hamilton lost about 2.5 s when the VSC ended. The VSC end is a timing risk, so the warning matters. |
| S8 | [Formula 1, 2022 safety car rule change](https://www.formula1.com/en/latest/article/fia-make-changes-to-safety-car-rules-ahead-of-2022-f1-season-start.2h2XmjOmYYyusyllcZmVIP) | Article 55.13: lapped cars may overtake only after race control sends "LAPPED CARS MAY NOW OVERTAKE". |
| S9 | [OpenSourceSports, safety car procedures](https://opensourcesports.io/rules/formula-1-fia/rules-of-play-5-7-safety-car-procedures-sporting-regulations-article-b5-13) | "SAFETY CAR IN THIS LAP" is sent and the SC orange lights go out. The SC enters the pit lane at the end of that lap. |
| S10 | [SportRules, SC, VSC and red flag rules 2026](https://sportrules.org/formula-1/safety-car-vsc-and-red-flags/) | Under the VSC each car must stay slower than the ECU minimum time in every marshalling sector. Under the SC, cars close to ten car lengths. |
| S11 | [EA, F1 25 UDP specification](https://forums.ea.com/t5/s/tghpe58374/attachments/tghpe58374/f1-games-game-info-hub-en/61/4/Data%20Output%20from%20F1%2025%20v3.pdf) | `SCAR` event: `safetyCarType` (0 none, 1 full, 2 virtual, 3 formation) and `eventType` (0 deployed, 1 returning, 2 returned, 3 resume race). |

## Real call cadence

1. Deployment. "Safety car, safety car" or "VSC deployed", then the delta (S1, S2). Pitwall has `sc_deployed` and `vsc_deployed`.
2. During the period. Delta as a number when the driver is near or under the limit (S4). Tyre and brake temperatures (S5, S6).
3. Ending. "Safety car in this lap" or "VSC ending", often with the tyre state (S3, S9). Pitwall has `sc_ending` and `vsc_ending`.
4. Green. "Green flag" and racing again. Pitwall has `sc_restart` and `vsc_restart`.

Lapped-car permission is a race control message (S8). Engineers relay it, but the game does not send it (see GAPs).

## Telemetry signals

| Call need | Packet and field | Status |
|---|---|---|
| SC or VSC active | Session `m_safetyCarStatus` (0 none, 1 full, 2 virtual, 3 formation) | Used. `RacePhase` gives `phase` `sc` or `vsc` |
| SC in this lap, SC lights out, VSC ending | Event `SCAR`, `eventType` 1 (returning) | Used. `sc_ending` is true for event types 1 and 2 |
| SC in the pit lane | Event `SCAR`, `eventType` 2 (returned) | Used through `sc_ending` |
| Resume race | Event `SCAR`, `eventType` 3 | Parsed, not used. Green comes from `m_safetyCarStatus` returning to 0 after `sc_exit_hold_s` |
| SC or VSC type on the event | Event `SCAR`, `safetyCarType` | Parsed, not used. `m_safetyCarStatus` already gives the type |
| Restart delta | Lap Data `m_safetyCarDelta` (s) | New. `Snapshot.safety_car_delta_s` |
| Tyre temperature for the restart | Car Telemetry `m_tyresInnerTemperature`, `m_tyresSurfaceTemperature` | Used. `coldest_tyre`, `coldest_tyre_c`, `tyre_inner_front_c`, `tyre_inner_rear_c` |
| Brake temperature | Car Telemetry `m_brakesTemperature` | Available (`brake_temp`). No calibrated cold limit, so no call yet |
| Gap to the car ahead (ten car lengths) | Lap Data `m_deltaToCarInFrontMSPart` | Available. No call yet |
| Lights out at a standing restart | Event `LGOT` | Used. Ends a red flag and any SC left from before it |
| Red flag | Event `RDFL` | Used |
| Session end | Event `SEND` | Used. Not a restart signal |
| Lapped cars may overtake | None | GAP |
| Number of SC and VSC periods | Session `m_numSafetyCarPeriods`, `m_numVirtualSafetyCarPeriods` | Parsed (`num_safety_car_periods`), not used |

### GAPs

- **Lapped cars may overtake.** No event code, flag or field carries race control's permission. `OVTK` only reports position changes. Pitwall does not guess it from lap counts.
- **Delta sign.** `m_safetyCarDelta` has no documented sign in the EA spec. The rules assume the in-game HUD meaning: negative means faster than the minimum time. A recording with a safety car must confirm this before the delta call is trusted.

## Rules and thresholds

| Rule | When | Priority |
|---|---|---|
| `sc_ending_tyres_cold` | `phase == 'sc'`, `sc_ending`, and the coldest inner tyre is below the compound's cold limit plus `mode.thermal_warn_offset_c`. Supersedes `sc_ending` | P1 safety |
| `vsc_ending_tyres_cold` | The same for `phase == 'vsc'`. Supersedes `vsc_ending` | P1 safety |
| `sc_delta_negative` | `phase in ('sc', 'vsc')` and `safety_car_delta_s < th.sc_delta_warn_s`. Re-arms above `th.sc_delta_clear_s` or after the period. 20 s cooldown | P1 safety |

| Threshold | Value | Why |
|---|---|---|
| `sc_delta_warn_s` | -0.1 | A small dead band, so a delta at 0.0 does not call |
| `sc_delta_clear_s` | 0.3 | Hysteresis, so a delta near zero does not repeat the call |

The cold-tyre rules reuse `tyre_inner_cold_by_compound_c`, the same limit as the out-lap tyre check.

## Phrasing

Each rule has four variants. ADR 0008 rotation picks them in a fixed order per rule id.
Every line carries a number the driver can act on, with one decimal.

| Rule | Example | Based on |
|---|---|---|
| `sc_ending_tyres_cold` | "Safety car in this lap. front left coldest at 68.0" | S3, S9 |
| `sc_ending_tyres_cold` | "Lights out on the safety car. front left at 68.0, cold for the restart" | S9 |
| `sc_ending_tyres_cold` | "Safety car in. Fronts 72.0, rears 81.0. front left is coldest" | S5 |
| `vsc_ending_tyres_cold` | "VSC ending, cold tyres. front left at 66.0" | S3 |
| `sc_delta_negative` | "Delta negative, -0.4. Back off" | S1, S4 |
| `sc_delta_negative` | "Delta -0.4. Too fast, get it positive" | S2 |

## Not built

- A brake temperature call. The telemetry has the values, but pitwall has no cold or hot brake limit for F1 26.
- A ten-car-lengths gap call under the SC. The gap is available, but the game's SC queue rules are not checked against a recording.
- A VSC-ending call for a delta far above zero (time lost at green, S7). It depends on the same unconfirmed delta sign.
