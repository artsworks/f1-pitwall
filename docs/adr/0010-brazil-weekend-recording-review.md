# 0010 · Brazil weekend recording review: rates, energy, boost and penalty calls

Status: accepted (2026-10)

## Context

Seven full-profile recordings from one F1 26 Brazil weekend (track 16) were
replayed, ingested, digested and checked against the live decision log
(447 rows). The race was 18 laps (session type 15). The recordings stay on
the game PC per [0005](0005-recordings-never-committed.md). This record keeps
only the observed values.

### What the game sent

Every header read `packet_format 2026` and `send_rate_hz 30`. The parser
accepted every datagram in all seven files, with 0 malformed, 0 unsupported
and 0 size-mismatched packets. The race file had 243,153 datagrams.

Measured race rates:

| ID | Packet | Hz |
|----|--------|----|
| 0, 2, 6, 7, 13, 16 | Motion, Lap Data, Car Telemetry, Car Status, Motion Ex, Car Telemetry 2 | 20.2 |
| 11, 12 | Session History, Tyre Sets | 20.2 |
| 10 | Car Damage | 10.2 |
| 1, 5 | Session, Car Setups | 2.2 |
| 15 | Lap Positions | 1.2 |
| 3 | Event | 0.8 |
| 4 | Participants | 0.4 |

The menu-rate packets arrived at 20.0 to 20.2 Hz in all seven files. The
capture in [0006](0006-recording-profiles.md) measured 28.7 Hz. The header
value is the rate Pitwall was configured for. The game does not report its
send rate in any packet, so the header does not prove what the game sent.

Lap Data and Car Status arrive as separate packets. In all 33 lap crossings
in the seven files, Car Status reset its per-lap ERS counters on the same
frame as the Lap Data lap change. `overall_frame_identifier` was equal to
`frame_identifier` and never went back.

### What happened in the race

- Strategy M-S, one stop at the end of lap 11. The plan said window 7 to 13,
  target lap 11, and the window-open call fired on lap 9.
- Medium wear at the stop was 26 to 30 %. Soft wear at the end of lap 17 was
  20 to 25 %.
- Fuel: 17.48 kg at the end of lap 1 and 0.89 kg at the end. The burn was
  0.98 kg per lap. The game's own `fuel_remaining_laps` read −0.24 after lap 1
  and rose to +0.77 at the end.
- ERS: harvest hit 6.0 MJ every lap from lap 4. The store at the line was
  0.52 MJ after lap 13, 0.02 MJ after lap 14 and 0.00 MJ after lap 15.
- Track limits: warnings on laps 2, 3 and 10, then a 3 s penalty on lap 10
  and a 4th warning on lap 16. Every track-limit warning and penalty call matched.
- At the last sample, the player was 5.82 s behind the leader with the 3 s
  penalty. Antonelli was 7.62 s behind and Verstappen 8.05 s. The player
  finished P2 on the road and P4 on time.

### Calls that were wrong, and why

1. **energy_under.** "1.7 MJ left that lap. Use it" fired at sector-3 entry
   with 0.5 MJ in the store. "Harvest more" followed in the same lap. The
   budget compared the drain of a part lap with a full-lap allowance. At
   Brazil most of the deploy happens on the straight after sector-3 entry.
   The allowance also used the current store, which already included that
   lap's drain. This also triggered a "harvest more" call at 4,018 m on the
   last lap.
2. **boost_left_on.** The weekend had 17 boost calls. 16 came from the
   "on for 12 s regardless" clause, at full throttle with zero brake, between
   4,213 m and the line or in the first 19 m. The longest legal activation in
   the race was 12.9 s. The one useful call came on lap 11, with the driver
   braking at 0.55.
3. **penalty_cost starved.** "Gap covers the penalty. P2 safe" was correct on
   lap 16 (margin 1.3 s). The player then lost about 2 s in that lap. The
   "With penalty, P4" call 30 s later shared the `penalty_standing` cooldown
   group, so it was suppressed. The rule engine had already used the edge, so
   the call never retried. The last call the driver heard was
   "Last lap. P2, bring it home".
4. **plan_announce_no_stop** fired on lap 13, after the only stop: "We're on
   Plan A, no stop, soft to the end". The rule checked stops left, not stops
   made.
5. **pit_exit_traffic_race** fired 13 s after the exit, after
   `pit_exit_traffic` had already called that exit. `pit_exit_rival_gap_s` is
   measured from the exit line, not from the player, so it is only valid at
   the exit.
6. **Number format.** "Hot lap behind, 0 seconds" (0.33 s), "On him in
   1 laps" (0.7 laps).

### Calls that were right

Fuel, plan, window, pit-now, out-lap tyre, lock-up, track-limit, penalty,
fastest-lap and final-lap status calls matched the telemetry. The digest found
no findings. `pitwall evaluate` reported 0 negative grades. The weekend had no
calls-off sessions, so that result does not compare anything.

The fuel call "0.2 laps short" at the start was right for its input. That
input was a prior of 1.04 kg per lap from the live database. The real burn was
0.98 kg per lap, and the learned value took over by lap 5.

## Decision

- The energy budget uses the store and laps remaining from the start of the
  lap as the allowance. `energy_under` grades the finished lap and fires in
  sector 1 of the next lap. `energy_over` still fires in sector 3 of the
  current lap.
- `SessionState` matches each Car Status sample to a lap by
  `overall_frame_identifier`. The finished lap's totals are the last sample
  before the Lap Data frame that changed the lap. Pitwall grades the lap 0.5 s
  of session time after the lap change, so a late old-lap sample still counts.
  Pitwall ignores old-lap samples that arrive after that. An older frame never
  overwrites the live counters. Pitwall keeps the last 2 s of samples, and
  always keeps the last one before a pending lap change. A flashback clears
  this tracking, and Pitwall does not grade that lap.
- `boost_left_on` fires only on a lift or brake after 3 s. This replaces the
  12 s rule in [0007](0007-driving-event-signals.md), and the
  `boost_max_s` threshold is gone.
- `penalty_cost` has its own cooldown. Good news can no longer block bad news.
  `last_lap` gives the position after the penalty when the penalty costs
  places.
- `plan_announce_no_stop` needs `num_pit_stops == 0`.
- `pit_exit_traffic_race` fires only at the exit and shares the `car_behind`
  group with `pit_exit_traffic`.
- `pitwall propose` suggested `energy_over_tolerance_j: 420000` for track 16
  (7 sessions, 24 laps, not converged). The thresholds stay unchanged, because
  the energy bug above affected the signal behind that suggestion.

## Consequences

- Do not trust `send_rate_hz` in a header. Check per-packet rates with
  `pitwall replay <file> --stats`. Rules that count ticks get two thirds of
  the expected samples at 20 Hz.
- An energy, battery or tyre call made mid-lap must say which part of the lap
  it measured. A full-lap allowance needs a full-lap measurement.
- A shared cooldown group can starve a rule whose `when` stays true, because
  the rule only re-arms on `clear_when`. Put opposite messages in different
  groups, or re-arm each lap.
- Not explained yet: on lap 3, `battle_defend` said the car behind was
  "8.6 seconds slower". Check the rival pace input on the next race recording.
- The CLI has no `hindsight` command. `pitwall digest` grades calls in
  hindsight.
