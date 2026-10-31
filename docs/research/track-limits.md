# Track-limit escalation chain

## Sources and real cadence

These sources describe the 2023 Austrian Grand Prix. They establish radio cadence, not proof of the game's enforcement settings.

| Source | Evidence | Use in Pitwall |
| --- | --- | --- |
| [RaceFans, Hamilton radio transcript, 2 July 2023](https://www.racefans.net/2023/07/02/hamilton-reveals-how-front-wing-set-up-change-led-to-his-track-limits-penalty/) | Bonnington first says "Track limits, exit turn 10." He later reports "we now have two tracks limits" and "Next track limit will be penalty." | State the count as warnings accumulate. State the next consequence before the penalty. Do not copy the corner reference without telemetry. |
| [Formula Dream, Austria race radio transcript](https://www.formuladream.app/f1-interactive/tools/team-radio/2023/austrian-grand-prix/race) | Sainz's engineer at 14:31:16 says "a third strike" and "black and white flag", then "the next one will be five seconds penalty". | Make the third call the final warning. This transcript has transcription errors, so use it as supporting evidence. |
| [Mercedes, Austrian GP race debrief, 5 July 2023](https://www.mercedesamgf1.com/news/2023-austrian-gp-akkodis-f1-race-debrief) | Andrew Shovlin's debrief includes the track-limits situation. The page has a video, but no written technical transcript. | Team context only. Do not attribute invented wording or enforcement details to the video. |
| [PlanetF1, track-limits explainer, 3 July 2023](https://www.planetf1.com/news/explained-f1-track-limits-rules) | The third violation brings the black-and-white flag. The fourth brings a five-second penalty. Later violations follow a different cycle. | Announce the initial 1/2/3 chain. Do not use modulo arithmetic to invent subsequent penalty cycles. |

The real calls report an offence, then the running count, then the consequence of another offence.
Pitwall uses short deterministic variants with decimal counts, as required by the radio-copy rules.
Pitwall does not announce an actual time penalty until telemetry reports that penalty.

## Telemetry signals

The [F1 26 UDP notes](../reference/f1-26-udp-notes.md) and parser define the fields below.
Packet numbers are the protocol packet IDs.

| Signal | Packet and wire field | Parser/state field | Availability |
| --- | --- | --- | --- |
| Player and session | Header `m_playerCarIndex`, `m_sessionUID`, `m_sessionTime` | `header.player_car_index`, `session_uid`, `session_time` | Available. State filters rival PENA events. |
| Track-warning count | Packet 2, Lap Data `m_lapData[].m_cornerCuttingWarnings` | `CarLap.corner_cutting_warnings`, `Snapshot.corner_cut_warnings` | Available without event packets. |
| General warnings | Packet 2, Lap Data `m_lapData[].m_totalWarnings` | `CarLap.total_warnings`, `Snapshot.warnings` | Available, but includes non-track warnings. Do not use for this chain. |
| Track-warning event | Packet 3, Event `m_eventStringCode = PENA`, detail `penaltyType`, `infringementType`, `vehicleIdx`, `lapNum` | `penalty_type`, `infringement_type`, `vehicle_idx`, `lap_num` | Available. Type 5 is a warning. Infringements 7/8/9 are cuts, 27/28/29 are running wide. |
| Shared warning count | Packet 2 and packet 3 above | `Snapshot.track_warning_count = max(len(_warning_events), corner_cut_warnings)` | Existing dashboard count. Reuse it without another counter or Snapshot field. |
| Race context | Packet 1, Session `m_sessionType`, `m_safetyCarStatus`. Packet 2 `m_driverStatus`, `m_pitStatus` | `session_kind`, `phase` | Available. Rules run in racing, SC and VSC phases only. |
| Actual penalty | Packet 3 PENA `penaltyType`, `infringementType`, `time`, plus packet 2 `m_penalties` | `penalty_kind`, `penalty_infringement`, `penalty_time_s`, `penalty_s` | Existing penalty rules announce confirmed penalties separately. |
| Offending corner number | No corner field in PENA or Lap Data warnings | None | **GAP.** Neither the infringement code nor `lapNum` identifies the corner. |
| Displayed black-and-white flag | Packet 7 `m_vehicleFiaFlags` has no black-and-white value | None | **GAP.** The third-count call describes the conventional warning stage, not an observed flag display. |
| Configured penalty cycle | No warning-limit or track-limit strictness field in the inspected packets | None | **GAP.** Use the requested initial third-warning/fourth-offence convention. Do not infer later resets or a penalty duration. |

The PENA detail layout lives in `src/pitwall/protocol/packets.py`.
`src/pitwall/state/session.py` combines the event and Lap Data counts.
`src/pitwall/server/app.py` sends the same `track_warning_count` to the dashboard.

Do not derive a warning corner from current `lap_distance`, sector or motion coordinates.
Those fields locate the car, not the offence, and notifications can arrive late.
When only Lap Data arrives, Pitwall cannot distinguish a corner cut from running wide.
The aggregate track-limit wording covers both without guessing.

## Rules and threshold

The rules belong in `race_events.yaml`, alongside the existing warning and penalty rules.
The chain replaces the separate corner-cut warning rules because both kinds share the same game counter.
The old `corner_cut_warning` and `corner_cut_last_warning` IDs no longer exist.
Custom rule references must use the shared track-limit IDs.

`thresholds.yaml` adds `track_limits_flag_warnings: 3`.
The first rule matches that value minus 2, the second minus 1, and the last matches the value itself.
The existing `warnings_warn` setting remains available to the dashboard. The radio chain no longer uses its modulo calculation.

| Rule | Initial count | Next consequence |
| --- | --- | --- |
| `track_limit_warning` | 1 | The black-and-white stage comes at 3 warnings. |
| `track_limit_second_warning` | 2 | The next warning brings the black-and-white stage. |
| `track_limit_last_warning` | 3 | The next offence brings a penalty. |

All three rules require fresh Lap Data, but do not require PENA packets.
All use priority 3 and `info` urgency, below safety, pit execution, tyre and battle calls.
Each count fires once while the count stays unchanged. A pit stop does not restart the chain.
Queued calls expire if the count changes. Higher-count calls also supersede lower-count calls.
Joining at count 3 reports only count 3. Missing intermediate counts do not produce historical calls.
Counts of 4 or more do not wrap around. An actual counter rollback can rearm earlier count rules.
ADR 0008 phrase rotation remains deterministic and needs no LLM key.

## Phrasing

| Stage | Variants |
| --- | --- |
| First warning | "Track limits, 1.0 warning. Black-and-white warning at 3.0" / "That's 1.0 for track limits. Black-and-white warning at 3.0" / "1.0 track-limit warning now. At 3.0, black-and-white warning" |
| Second warning | "Track limits, 2.0 warnings. Next is the black-and-white warning" / "That's 2.0 for track limits. Next warning brings the black-and-white flag" / "2.0 track-limit warnings now. Black-and-white warning next" |
| Third warning | "Track limits, 3.0 warnings. Next warning is a penalty" / "That's 3.0, black-and-white warning. Next track limit is a penalty" / "3.0 track-limit warnings now. Next warning costs time" |

## Scenario and verification

`scenarios/austria-track-limits.yaml` uses a synthetic Austria stream, not an unavailable real recording.
`tests.synth.track_limits_scenario()` creates warnings on laps 2, 3 and 4, then a confirmed penalty on lap 5.
The helper deliberately compresses lap durations. The stream tests radio behavior, not driving physics.
The scenario stores the exact hash of a recording with a fixed wall-clock start of zero.

Generate the uncommitted fixture when a targeted bench run needs the source recording:

```sh
uv run python - <<'PY'
from pathlib import Path
from tests.synth import track_limits_scenario, write_packet_stream

directory = Path.home() / ".pitwall" / "recordings"
directory.mkdir(parents=True, exist_ok=True)
write_packet_stream(
    directory / "austria-track-limits.f1bin",
    track_limits_scenario(),
    wall_clock_start_us=0,
)
PY
```

Run `uv run pytest tests/test_track_limits.py` to verify the chain and scenario checks without a bench baseline.
Tests cover event-only counts, Lap Data-only counts, merged counts, mixed cut/wide warnings and deterministic replay.
Tests also reject rival and collision warnings, expired lower-count calls, repeated calls after pit phases and modulo wraparound.
No real race recordings were supplied. The synthetic replay cannot validate game-specific warning timing or enforcement settings.
