# Weather radar and slick/inter crossover calls

This note covers rain forecast calls, track-getting-wetter and track-getting-drier calls, and the timing of the lap-time crossover between slicks and inters. It lists the real radio it copies, the F1 26 telemetry behind each call, the gaps, and the new rules.

## What pitwall already had

- `weather_to_inter`, `weather_to_wet` and `weather_to_dry` (`race_events.yaml`) warn from the forecast once rain reaches `rain_inter_pct` (60) plus hysteresis. They never call a stop.
- `tyre_switch` and `tyre_switch_slicks_traffic` (`race_stint.yaml`) call the stop when the field's lap times on the other compound pay back the pit loss (`strategy/weather.py`, `SessionState._tyre_switch`).
- `weather_box_now` makes the stop a priority 1 call when the driver's slip events rise.

Nothing spoke below the inter band, nothing reported the track trend, and the crossover was only called once it had already happened.

## Reverted mixed-weather work

Commit `00458ab` ("Mixed-weather race fixes from 25% recording") was dropped by `11d23e7`, a branch revert to `a552baf` at the owner's request. Its weather change made `to_wet` need a forecast sample with weather 4 (heavy rain) or 5 (storm), so 90% light rain no longer said "full wets". The same commit also changed `stop_laps`, the plan A/B flip and the out-lap tyres-ready call after a drive-through. This PR does not bring any of it back. The heavy-rain gate is listed under decisions below.

## Real radio: cadence and wording

| Call | Real wording | Source |
|---|---|---|
| Early forecast | "We expect some drops in 10 laps and maybe a bit more rain in 15 to 20 laps." | [RaceFans, Silverstone 2024](https://www.racefans.net/2024/07/09/how-leclerc-blew-another-wet-weather-tyre-call-while-sainz-nailed-it/) |
| Countdown | "Rain expected in seven laps, lasting for 10 minutes." Then "Rain in three to four laps." Then "We expect class two to three rain in one to two laps." | same |
| Countdown in minutes | "Current expectations are for rain in 10 minutes Max, 10 minutes." | [Formula Dream, Germany 2018](https://www.formuladream.app/f1-interactive/tools/team-radio/2018/german-grand-prix/race) |
| Intensity change | "So expecting heavier rain in five minutes." "You can expect the rain intensity to increase in five minutes." | [Formula Dream, Silverstone 2025](https://www.formuladream.app/f1-interactive/tools/team-radio/2025/british-grand-prix/race) |
| Intensity change | "Gap 0.8. Expecting heavier rain." "It is getting heavier and it's only going to get heavier than this." | [RaceFans, Interlagos 2024](https://www.racefans.net/2024/11/04/it-should-go-red-the-leaders-crucial-radio-calls-during-the-interlagos-downpour/) |
| Rain easing | "Rain intensity is reducing now." "Expect rain level to diminish to nothing in roughly 10 minutes." | same |
| Rain length | "This is that patch of rain that we were discussing about, Max. Should last a maximum of five minutes." | [RaceFans, Canada 2024](https://www.racefans.net/2024/06/11/how-red-bull-overruled-verstappen-to-avoid-race-losing-tyre-choice-error/) |
| Field pace as the crossover signal | "Lap times are still for slicks. Inters is one minute 40." "Russell lap time 31.6 and Stroll behind 32.6." | [RaceFans, Silverstone 2024](https://www.racefans.net/2024/07/09/how-leclerc-blew-another-wet-weather-tyre-call-while-sainz-nailed-it/) |
| Drying track | "Rain has now stopped in the pit lane." "Tyres are still improving." | [RaceFans, Singapore 2017](https://www.racefans.net/2017/09/25/2017-singapore-grand-prix-team-radio-transcript/) |
| Slick call on a drying track | "You think to stop for slicks now, new medium?" then "Box, Lewis, box." | [RaceFans, Spa 2025](https://www.racefans.net/2025/07/29/im-down-to-try-how-hamilton-changed-his-mind-about-crucial-spa-pit-stop/) |

Cadence: before rain, engineers repeat the countdown every two to four laps with a number each time (laps or minutes, and sometimes a rain class). During rain they report intensity changes and lap times of rivals on the other tyre. The stop comes when the field's lap times on the other compound cross over.

How teams judge the crossover:

- Pirelli puts the switch to inters at a slick lap time of 110% to 112% of the dry lap ([Pirelli](https://www.pirelli.com/global/en-ww/race/racingspot/formula-1/when-it-s-time-to-change-from-slick-to-wet-tyres-in-formula-1-52943/)).
- A Ferrari strategist says the switch to slicks approaches when lap times are about 10 seconds slower than a dry lap ([Pitpass](https://www.pitpass.com/68613/Ferrari-strategist-reflects-on-a-challenging-Turkish-Grand-Prix)).
- In Singapore 2022 one car's fastest lap on slicks was the trigger for the rest of the field to stop ([Motor Sport](https://www.motorsportmagazine.com/archive/article/december-2022/36/switching-from-inters-to-slicks-the-crucial-strategic-tyre-call-in-f1-wet-races/)).

No team video explainer was transcribed for this note. The Ferrari strategist interview and the Pirelli guide are the strategy sources.

## Telemetry signals

| Signal the calls need | Packet and field | Status |
|---|---|---|
| Rain chance by minutes ahead | Session `m_weatherForecastSamples[].m_rainPercentage`, `m_timeOffset`, `m_sessionType` | Available, parsed and used |
| Forecast weather type | Session `m_weatherForecastSamples[].m_weather` (0 clear to 5 storm) | Available, parsed |
| Weather now | Session `m_weather` | Available (`Snapshot.weather_now`) |
| Track temperature now | Session `m_trackTemperature` | Available, not used by these calls |
| Track temperature trend | Session `m_weatherForecastSamples[].m_trackTemperatureChange` (0 up, 1 down, 2 no change) | Parsed, not read by `SessionState` |
| Forecast accuracy | Session `m_forecastAccuracy` (0 perfect, 1 approximate) | Available (`Snapshot.forecast_accuracy`), picks the softer forecast wording |
| Rival tyre compound | Car Status `m_visualTyreCompound` for each car | Available. Not checked in a restricted online session |
| Rival tyre age | Car Status `m_tyresAgeLaps` for each car | Available |
| Rival last lap | Lap Data `m_lastLapTimeInMS` for each car | Available |
| Rival pit and running state | Lap Data `m_pitStatus`, `m_resultStatus` | Available |
| Neutralised laps | Session `m_safetyCarStatus` | Available |
| GAP: rain intensity now | none. `m_rainPercentage` is a probability, `m_weather` is a 6-step type | Not implemented |
| GAP: water on track by corner or sector ("standing water at turn two") | none | Not implemented |
| GAP: where the rain is on track ("rain approaching sector three") | none | Not implemented |
| GAP: rival grip or aquaplaning | Motion Ex is player-only | Not implemented |

Rival sector times are also in Lap Data (`m_sector1TimeMSPart`, `m_sector2TimeMSPart`), so a later call could compare compounds by sector.

## New rules

All four rules live in `race_events.yaml` next to `weather_to_*`, because they are race weather calls and every sibling is race-only.

| Rule | Priority | Fires when | Signal |
|---|---|---|---|
| `weather_rain_soon` | 3 | On slicks, no rain now, no `weather_crossover` yet, and a forecast sample within `rain_heads_up_max_min` (15) minutes is at or above `rain_heads_up_pct` (40) | Forecast samples |
| `track_wetter` | 2 | Raining now (`weather_now >= 3`) and the field's median lap-over-lap change is at least `track_trend_s` (1.0 s) slower | Rival last laps |
| `track_drier` | 2 | Player on inters or wets, no heavy rain, and the field is at least `track_trend_s` quicker lap over lap | Rival last laps |
| `compound_crossover_close` | 2 | The gap to the other compound's runners is closing by at least `crossover_rate_min_s` (0.3 s) a lap and the projected crossover is within `crossover_warn_laps` (3) laps | `slick_gain_s`, `inter_gain_s` over two laps |

The track-trend and crossover rules stay silent while `tyre_switch_to` is set, because the stop call already carries the news.

Field trend: each rival lap pair counts only when both laps are green and clean (no pit, not neutralised, tyres at least 3 laps old, same compound). The trend needs `track_trend_min_cars` (3) rivals. Fuel burn and tyre wear move lap times by about 0.1 s a lap, far below the 1.0 s threshold.

Crossover projection: gap is how much slower the other compound's runners still are, and rate is how much that gap shrank over the last completed lap. Laps to the crossover is gap divided by rate (`strategy/weather.py` `crossover_projection`). It needs at least `compound_gain_min_cars` (2) cars on each compound, so it can only fire after some rivals have switched.

New thresholds (`thresholds.yaml`):

| Threshold | Default | Why |
|---|---|---|
| `rain_heads_up_pct` | 40 | Below the 60% inter band, so the heads-up comes first |
| `rain_heads_up_max_min` | 15 | Real countdowns start about 10 minutes out |
| `track_trend_s` | 1.0 | Ten times the fuel and wear effect per lap |
| `track_trend_min_cars` | 3 | One car's traffic or mistake can't fire it |
| `crossover_warn_laps` | 3 | Real calls start at three to four laps out |
| `crossover_rate_min_s` | 0.3 | Smaller closing rates are lap-time noise |

## Phrasing

Each rule has four or five variants. ADR 0008 rotation applies. Percent and minutes are whole numbers because the game sends them as integers, matching `weather_to_*`. Lap counts use one decimal. Pace uses `amount_words` ("eight tenths", "1.4 seconds").

| Rule | Example | Modelled on |
|---|---|---|
| `weather_rain_soon` | "Rain expected in 10 minutes, 45 percent" | "Current expectations are for rain in 10 minutes" |
| `weather_rain_soon` | "Rain in 10 minutes, 45 percent chance. Slicks for now" | "Rain expected in seven laps, lasting for 10 minutes" |
| `track_wetter` | "Track getting wetter. Field 2.0 seconds slower last lap" | "It is getting heavier", "Russell lap time 31.6" |
| `track_wetter` | "Rain getting heavier. Field lost 2.0 seconds last lap" | "Expecting heavier rain" |
| `track_drier` | "Track drying. Field 2.0 seconds quicker last lap" | "Tyres are still improving" |
| `compound_crossover_close` | "Crossover in 3.0 laps. Field on slicks 3.0 seconds off, closing a second a lap" | "Lap times are still for slicks. Inters is one minute 40" |

## Owner decisions

- The reverted heavy-rain gate on `to_wet` stays out. `to_wet` keeps using the rain chance alone.
- Pitwall reads Session `m_forecastAccuracy` into `Snapshot.forecast_accuracy`. When it is 1 (approximate), `weather_rain_soon` and `weather_to_*` switch to a softer `severity` pool ("Rain possible in 10 minutes, 40 percent"). When it is 0 (perfect), they keep the firm wording. The parallel dry-to-wet work found 0 in every Interlagos recording it checked, so the softer pool will rarely be heard.
- The bench scenario `brazil-weather-crossover` stays a dry guard with absent checks only, because no mixed-weather race recording exists yet.
