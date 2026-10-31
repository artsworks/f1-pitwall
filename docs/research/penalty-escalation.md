# Penalty escalation

Research and design notes for drive-through and stop-go calls: the issue call, game reminders, the served confirmation, and out-lap tyre calls after a drive-through.

## Earlier attempt

Commit `00458ab` ("Mixed-weather race fixes from 25% recording") gated `out_lap_s3_tyres_cold` and `out_lap_s3_tyres_ready` on `tyre_age_laps == 0` in races, so that a drive-through did not get a "tyres are in" call. The repo owner asked to revert the whole batch (`11d23e7`, "Revert branch to a552baf"), and `docs/status.md` listed drive-through tyre calls as a follow-up. The change had no test. The tyre-age check is also wrong after a real stop onto a used set, because a used set has an age above 0 on its out-lap.

This change uses Lap Data `num_pit_stops` instead. The game counts a real stop in the box, and a drive-through leaves the count unchanged. Commit `228271b` already uses the same signal to skip the wrong-tyre warning after a drive-through.

## Sources

Real radio (speech-recognition transcripts, wording can be imperfect):

| Race | Call | Source |
|---|---|---|
| Qatar 2024 | "Lando, we have been given a 10-second stop-go penalty, which we will take this lap." | https://www.formuladream.app/f1-interactive/tools/team-radio/2024/qatar-grand-prix/race |
| Qatar 2024 | "Lando has been given a 10 second stop and go penalty, which he needs to serve within two laps." (to Verstappen) | same |
| Qatar 2024 | "OK, so we've got a drive through penalty for speeding in the pit lane." (Hamilton) | same |
| USA 2018 | "OK, we have a drive-through penalty, we'll serve that now." (Stroll) | https://www.formuladream.app/f1-interactive/tools/team-radio/2018/united-states-grand-prix/race |
| Mexico 2024 | "Update: our start is under investigation." Two laps later: "So we have a five-second penalty. We were forwards on the grid box." (Perez) | https://www.racefans.net/2024/10/30/lawson-is-blocking-norris-unheard-radio-highlights-from-the-mexican-gp/ |
| Canada 2023 | "We have a five second penalty, so we'll want to try and build the gap on the cars behind." (Norris) | https://www.formuladream.app/f1-interactive/tools/team-radio/2023/canadian-grand-prix/race |
| Austria 2021 | "We have a five second penalty... the gap to Daniel is 24.9." (Norris) | https://www.formuladream.app/f1-interactive/tools/team-radio/2021/austrian-grand-prix/race |
| Austria 2024 | "So we do have a five second time penalty." (Hamilton) | https://www.formuladream.app/f1-interactive/tools/team-radio/2024/austrian-grand-prix/race |
| Belgium 2026 | "We need to serve a five-second penalty." (Hamilton) | https://www.formuladream.app/f1-interactive/tools/team-radio/2026/belgian-grand-prix/race |

Game behaviour and telemetry:

- F1 UDP specification, penalty and infringement tables: https://f1-2020-telemetry.readthedocs.io/en/latest/telemetry-specification.html
- Drive-through serving in the game (drive into the pit lane with no pit request): https://steamcommunity.com/app/1692250/discussions/0/3453716250587647098/
- Stop-go in the game is served at the next tyre stop, and is not forced into its own visit: https://steamcommunity.com/app/737800/discussions/0/1741101364282211320/
- A league rulebook says a game drive-through must be served within 3 laps or the driver is disqualified: https://www.indianracingcommunity.co.in/F1_Rulebook_v8.pdf

No team strategy debrief (Mercedes, Ferrari or McLaren) covered drive-through or stop-go handling, so this note has none.

## Real call cadence

1. Investigation notice ("under investigation"), sometimes with the likely penalty.
2. The penalty, with its kind and reason. Time penalties add the gap that matters.
3. For a drive-through or stop-go, when it is served ("we'll serve that now", "we will take this lap").
4. Engineers rarely confirm a served penalty on air. Pitwall confirms it with the new position, because the driver rejoins blind.

## Telemetry signals

| Need | Packet and field | Status |
|---|---|---|
| Penalty issued, kind and reason | Event `PENA`: `penaltyType` 0 drive-through, 1 stop-go, 4 time; `infringementType`; `time` | Used (`penalty`, `penalty_pit`) |
| Lap of issue | Event `PENA`: `lapNum` | Available, not used |
| Places gained | Event `PENA`: `placesGained` | Used for grid penalties only |
| Unserved drive-through or stop-go | Lap Data `m_numUnservedDriveThroughPens`, `m_numUnservedStopGoPens` | Used |
| Game reminder, serve within N laps | Event `PENA`: `penaltyType` 3, `infringementType` 15 | New: `penalty_serve_reminder` |
| Game reminder, serve this lap | Event `PENA`: `penaltyType` 3, `infringementType` 16 | New: `penalty_serve_this_lap` |
| Drive-through served | Event `DTSV`: `vehicleIdx` | New: `penalty_served_drive_through` |
| Stop-go served and time held | Event `SGSV`: `vehicleIdx`, `stopTime` | New: `penalty_served_stop_go` |
| Pit-lane pass with no tyre change | Lap Data `m_numPitStops` unchanged across a pit visit | New: `pit_pass_no_stop` |
| Time penalty total | Lap Data `m_penalties` | Used (`penalty_cost`, `finish_penalty`) |
| Warning count | Lap Data `m_totalWarnings`, `m_cornerCuttingWarnings` | Used (track-limit rules) |
| Investigation notice | none | GAP |
| Length of the serve window (N laps) | none. Infringement 15 says "within n laps" but no field carries n | GAP |
| Whether a stop-go stop also changes tyres | none. `pit_pass_no_stop` covers only a visit with no stop | GAP |
| Unserved drive-through or stop-go converted at the flag | none | GAP |
| Time penalty served at a pit stop | Lap Data `m_penalties` may drop, and `m_pitStopShouldServePen` may flag it. Neither is checked against a recording | GAP (unverified) |

## Rules

The new rules sit in `race_events.yaml` next to `penalty`, `penalty_pit` and `serve_penalty`. The out-lap tyre rules stay in `shared.yaml` with one added condition.

| Rule | Priority, urgency | Fires when |
|---|---|---|
| `penalty_serve_this_lap` | 1, execution, pit entry | `PENA` reminder 16 inside `penalty_recent_s` and an unserved drive-through or stop-go |
| `penalty_serve_reminder` | 2, execution, pit entry | `PENA` reminder 15 inside `penalty_recent_s` and an unserved drive-through or stop-go |
| `penalty_served_drive_through` | 2, info | `DTSV` for the player inside `penalty_recent_s` |
| `penalty_served_stop_go` | 2, info | `SGSV` for the player inside `penalty_recent_s` |
| `out_lap_s3_tyres_cold`, `out_lap_s3_tyres_ready` | unchanged | now also need `not pit_pass_no_stop` |

The repo owner also decided two wording changes. `penalty_pit` names no lap count, because telemetry has no field for the serve window and the reminder calls cover timing. `serve_penalty` has a drive-through pool that never implies a tyre change, because a drive-through fits no tyres. Each line keeps the tyre stop a separate visit, because `box_now` can ask for tyres on the same lap. Stop-go keeps the stop wording.

There are no new thresholds. All four rules reuse `penalty_recent_s` (10 s), the same latch as the other penalty calls.

## Phrasing

| Rule | Variants |
|---|---|
| `penalty_serve_this_lap` | "Box this lap for the drive-through. Last lap to serve it" / "Last lap to serve the drive-through. Box this lap" / "Drive-through must be served this lap. Box, box" / "Box, box for the drive-through. This is the last lap" |
| `penalty_serve_reminder` | "Reminder, drive-through still to serve. 4 laps to go" / "Drive-through still on the car. 4 laps left" / "We still owe the drive-through. 4 to go" / "Drive-through not served yet. 4 laps remaining" |
| `penalty_served_drive_through` | "Drive-through served. P7 now" / "That's the drive-through done. P7" / "Drive-through served, P7. Penalty clear" / "Penalty served. Back out in P7" |
| `penalty_pit` | "We've got a drive-through. We'll call when to serve it" / "Drive-through penalty. We'll call the lap to take it" (no lap count) |
| `serve_penalty`, drive-through | "Drive-through first, no tyre change. Tyres on a separate stop" / "Drive-through is a separate pass, no tyres. The tyre stop comes after" / "Serve the drive-through first. No tyre change on that pass" / "Drive-through pass first, no tyres. Then we box for the stop" |
| `penalty_served_stop_go` | "Stop-go served, 10.0 stationary. P9" / "Stop-go done, 10.0 seconds held. P9" / "That's the stop-go served. P9, penalty clear" / "Stop-go served. 10.0 in the box, P9" |

## Scenario

`scenarios/brazil-penalty-escalation.yaml` uses a new bench mutation, `drive_through: LAP`. The mutation sends a `PENA` drive-through for pit lane speeding on that lap. It sets one unserved drive-through in Lap Data until the player's next pit visit, then sends `DTSV`. The scenario is a target, because the source recording is not in the repo.
