# Lift and coast and power-unit management

## Sources and cadence

- [Mercedes, via Motorsport Week](https://www.motorsportweek.com/2015/06/17/10393/): lift before braking to reduce fuel use with less lap-time loss.
- [RaceFans, lift and coast](https://www.racefans.net/2015/06/11/lift-and-coast-why-f1-drivers-are-told-to-save-fuel/): lifting can also reduce engine and brake temperatures.
- [RaceFans, 2017 Italian GP radio](https://www.racefans.net/2017/09/03/2017-italian-grand-prix-team-radio-highlights-race/): short instructions combine a condition with an action. The transcript includes "Even small lift-and-coast is good" and "front brakes are hot".
- [RaceFans, radio jargon](https://www.racefans.net/2024/10/10/li-co-migration-and-more-a-simply-lovely-guide-to-f1s-team-radio-jargon-busted/): "li-co" means lift and coast. The transcript says "The focus is to manage your tyres, same for the li-co."
- [Mercedes cooling explanation, via PlanetF1](https://www.planetf1.com/news/mercedes-cooling-change-aided-hamiltons-pace): Hamilton left a gap and lifted to cool his car behind another car.
- [Formula 1 brake temperatures](https://www.formula1.com/en/latest/article/under-pressure-why-brake-systems-feel-the-strain-in-canada.7CXceKaKp85uu52wrUhkjK): real brakes can exceed 1,000 C under braking. They need to cool before the next corner.
- [Formula 1 2026 terms](https://www.formula1.com/en/latest/article/explained-the-new-key-terms-for-formula-1-s-new-for-2026-rules.3T5BU6TC9quGcIpGzoWkY0): lift-off regeneration and full-throttle super clipping both harvest energy. Super clipping differs from lift and coast.
- [Autosport on super clipping](https://www.autosport.com/f1/news/what-is-super-clipping-and-can-it-be-f1s-plan-b-for-2026/10800447/): McLaren tested full-throttle harvesting at 350 kW, with a speed tradeoff.
- [Setup Lab F1 26 engine temperatures](https://leaguesetups.com/f126-engine): community data lists peak engine power at 115 to 125 C. The site attributes its values to the game spline.
- [EA forum brake report](https://forums.ea.com/discussions/f1-games-franchise-discussion-en/brakes-on-fire/8492504): a player reported 1,100 C brakes in an older game. This is not an F1 26 limit.

## Telemetry signals

Names below use the EA packet names. Parsed names use snake case in `layouts.py`.

| Signal | Packet and field | Parsed | Snapshot |
| --- | --- | --- | --- |
| Fuel mass, estimated laps | CarStatus `m_fuelInTank`, `m_fuelRemainingLaps` | Yes | `fuel_in_tank`, `fuel_remaining_laps` |
| Battery energy, deployment mode | CarStatus `m_ersStoreEnergy`, `m_ersDeployMode` | Yes | `ers_store_pct` derived from joules, `ers_deploy_mode` |
| Energy harvested and deployed this lap | CarStatus `m_ersHarvestedThisLapMGUK`, `m_ersHarvestedThisLapMGUH`, `m_ersDeployedThisLap` | Yes | Derived `energy_lap_delta_mj`, not raw counters |
| Engine temperature | CarTelemetry `m_engineTemperature` | Yes, C | `engine_temp_c` |
| Brake temperatures | CarTelemetry `m_brakesTemperature` | Yes, C, RL/RR/FL/FR | `brake_temp`, `brake_temp_max_c`, `brake_temp_corner` |
| PU component wear | CarDamage `m_engineICEWear`, `m_engineMGUKWear`, `m_engineESWear`, `m_engineCEWear`, `m_engineTCWear` | Yes, percent | `damage.ice_wear`, `mguk_wear`, `es_wear`, `ce_wear`, `tc_wear` |
| MGU-H wear | CarDamage `m_engineMGUHWear` | Yes | No. It is always zero in the documented F1 26 observations. |
| Gap and name | LapData `m_deltaToCarInFrontMSPart`, Participants `m_name` | Yes | `gap_ahead_s`, `rival_ahead_name` |
| Oil and water temperature | GAP: no packet field | No | No |
| ICE, MGU-K and ES/battery component temperatures | GAP: no separate packet fields | No | No |
| Fuel flow rate | GAP: no packet field | No | No |
| Per-corner fuel use and lift points | GAP: no packet fields | No | No |
| Radiator or cooling setting | GAP: no packet field | No | No |
| Super clipping state and recharge map | GAP: no explicit packet fields | No | No |

The fuel target divides the estimated deficit in kg by remaining laps. It does not prescribe a lift distance or corner.

## Rules and thresholds

The engine warning uses 128 C and clears below 123 C. Setup Lab's 125 C upper peak-power boundary informs the warning. Neither value is verified against a recording.

The brake warning uses 1,000 C and clears below 900 C. The real-racing source reports peaks above 1,000 C, not a game threshold. These values are unverified, and normal braking peaks may cause false alarms.

The wear warning uses 60 percent and clears below 55 percent. These are provisional judgement values, not measured failure boundaries. Dirty air means a positive gap within 1.0 second. The gap is a heuristic, not a cooling measurement.

The owner must calibrate these thresholds from a recording, including transient brake peaks and the Brazil false-alarm guard. The owner must also decide whether to add a fuel/engine-temp bench mutation. The guard uses the unchanged real race because the bench cannot create those sequences.

## Phrasing

| Real line | Pitwall line |
| --- | --- |
| "Even small lift-and-coast is good" | "Fuel 0.4 laps short. Save 0.1 kg a lap, lift and coast" |
| "front brakes are hot" | "Front left brake 1050.0 degrees. Lift earlier, cool them" |
| "we need to cool the right-hand side of the car for engine temps" | "Engine 128.0 degrees, 0.6 behind Norris. Drop out of the tow to cool it" |

The values in these examples are illustrative. Radio phrases rotate deterministically under ADR 0008.
