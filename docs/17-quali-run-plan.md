# Qualifying run plan: push, cool, or box

Status: design proposal (not yet implemented). Grounded in the Q1 recording
`session_e94df8c0e0f9bf12_1790343037` (Session type 5, ~75.4 s lap).

## 1. What the Q1 recording shows

Run 1 was four consecutive push laps with no cool-down lap:

| Hot lap | Line time (s) | ERS at the line | FR inner at the line | Lap time | Fuel at the line |
|---|---|---|---|---|---|
| 1 | 117.9 | 55 % | 85 °C | 88.98 (warm-up) | 19.0 kg |
| 2 | 206.9 | 31 % | 98 °C | **75.44 (best)** | 18.0 kg |
| 3 | 282.4 | **2 %** | 104 °C | ~76.2 | 17.0 kg |
| 4 | 358.6 | **6 %** | 105 °C | aborted | 16.0 kg |

- Each lap harvests up to the per-lap limit (~6.0 MJ reported as harvested, `ers_harvest_limit_per_lap`), but a
  push lap deploys more than it harvests, so the battery ran out after two laps. Lap 3 started
  on 2 % and was ~0.8 s slower on an unchanged setup.
- The front right went past the 102 °C carcass target from lap 3 onwards and never
  came back down while pushing.
- Fuel is not the constraint here: 20 kg loaded, ~1.0 kg per lap, `fuel_remaining_laps` 19 at
  the start. It only becomes one if you run a light fuel load.
- Time is the other constraint. A cool-down lap costs about 1.25 × best lap (~95 s here).

## 2. Decision at the end of each hot lap

This is evaluated once, on crossing the line after a hot lap, from the actual battery and tyre state (see §3). Inputs,
all already in the snapshot or cheap to add:

| Input | Source |
|---|---|
| `ers_at_line_pct` | ERS store now + expected harvest − expected deploy for the rest of the lap (from the player's last-lap S3 energy trace) |
| `ers_needed_pct` | ERS at the line of the player's best lap (learned per track; default 50 %) |
| `tyre_hot` | slow 30 s inner EMA above `pressure_window_high_c` on any corner, with hysteresis |
| `cool_lap_s` | 1.25 × best lap (learned from the player's own cool laps once seen) |
| `laps_possible` | how many more line crossings fit before the flag: `session_time_left` vs lap times |
| `fuel_laps` | `fuel_remaining_laps` minus in-lap reserve |
| `quali_margin_ms` / `abort_advised` | already implemented (safe margin, projection vs cut-off) |
| `fresh_sets_current` | Tyre Sets |

Outcomes (evaluated in this order):

1. **BOX**: `quali_margin_ms >= quali_safe_margin_ms` (already safe), or tyres/fuel cannot
   support another lap. Save the set. This is already covered by `quali_safe_cut` / `quali_safe_pole`.
2. **PUSH NOW (no time to cool)**: `laps_possible` allows only one more push lap if you cool first,
   i.e. `session_time_left < cool_lap_s + hot_lap_s + margin`, and the player is outside the cut
   (`quali_margin_ms < 0`). "No time to cool. Keep pushing, battery's at 20, use it on the
   straights." This is the danger-of-elimination override you described.
3. **COOL**: `ers_at_line_pct < ers_needed_pct − ers_cool_gap_pct` *or* `tyre_hot`, and
   `laps_possible` and `fuel_laps` allow cool + hot + in.
4. **PUSH AGAIN**: otherwise.

Why a single decision state instead of separate rules: the three signals interact
(a hot tyre can be ignored if it's the last lap; low battery can be ignored if you are
outside the cut and out of time). One pure function `run_plan(...) -> Plan` in
`state/quali.py`, like `abort_advice`, keeps it testable from recordings.

## 3. When to tell the driver

| Moment | Call | Why then |
|---|---|---|
| Crossing the line after a hot lap (sector 1, main straight) | "Battery's 2. Cool this one, recharge mode" / "Go again" / "No time to cool, keep pushing" / "Box this lap" | Implemented timing. A sector 3 projection was tried against Q1 and rejected: sector 3 deploy varied from 6 to 58 points of battery between laps, so the projection flipped the call on the wrong laps |
| Abort advised mid-lap | Existing `abort_lap` wins (P2, same cooldown group) | An aborted lap becomes a cool lap, so the plan switches to COOL |

Priority 2, one cooldown group `run_plan`, so at most one plan call per lap.

## 4. Cool-down lap coaching

Detected as: flying lap aborted, or plan = COOL and the player crossed the line (the game
still reports `driver_status` flying/out lap, so we track our own `cool_lap` flag).

Script, each item gated to straights and inside the per-run budget:

1. **Line / T1**: "Recharge mode. Harvest everything." (checks `ers_deploy_mode`; repeats
   once if the mode has not changed by the end of the first straight.)
2. **Mid-lap**: target status: "Battery 45, heading for 90. Fronts 104, lift early into the
   braking zones, stay off the kerbs." Uses the same inner-temp EMA as the pressure advice.
3. **Setup / MFD**: from the last hot lap:
   - repeated front lock-ups in one zone → brake bias one click rearward;
   - rear lock-ups / spins on entry → one click forward or more engine braking;
   - wheelspin on exit → diff on-throttle down. (Only the brake bias is reported back to us in Car
     Status, so only bias can be confirmed. Other changes are advice only.)
4. **Comparison with pole** (Session History has every car's best sectors):
   "Pole is 1.4 up. Most of it is sector 2, 0.8. You lost 0.3 at the turn 10 lock-up."
   Mini-sectors are possible later from Lap Data `lap_distance` × `current_lap_time` for all
   cars.
5. **Mistakes on the last lap**: lock-ups (count and where), spins, track-limit warnings,
   yellow-flag lifts, from the lap summary we already keep.
6. **Traffic**: cars behind on a flying lap within 3 s. "Car behind on a hot lap, 2 seconds.
   Move off the line." This is a P1 safety/penalty call; impeding in qualifying is a grid penalty.
7. **Final straight before the line**: "Hot lap mode. Tyres 95, battery 88. Go." This is
   triggered by distance `track_length_m − hot_lap_warning_m` (default 600 m), not time.

Items 3–5 are "chat": P3, dropped first when the budget is spent, and skipped entirely if
the cool lap is short or traffic calls are active.

## 5. Open questions / to verify in game

- `ers_deploy_mode` values in F1 26 (2026 regs): the recording shows 0–3, with 3 at the line and
  1 mid-lap. We need a labelled capture (switch each mode on a straight) before the reminders
  can check the mode.
- ERS store capacity: we assume 4 MJ for `ers_store_pct`; 2026 cars may differ. Calibrate
  from the maximum store energy seen in the garage.
- Whether a full recharge fits in one cool lap on every track (after the Q1 abort the store went from 0 % to 90 %
  in ~40 s of lifting, so probably yes on this track).
- Minimum useful battery for a hot lap per track: learn from the player's best laps.

## 6. Implementation outline (~1 session)

1. Snapshot: `ers_at_line_pct` projection, `cool_lap` flag, `plan` (`push|cool|box|push_now`),
   `laps_possible`, pole car sector deltas, last-lap mistake summary.
2. `run_plan()` pure function + unit tests; replay test against the Q1 recording (lap 2 →
   COOL expected because it predicts 2 % at the line).
3. Rules: `run_plan_heads_up`, `run_plan_changed`, `cool_recharge`, `cool_status`,
   `cool_vs_pole`, `cool_mistakes`, `cool_traffic_behind`, `cool_hot_mode_reminder`.
4. Dashboard Zone F: plan chip (PUSH / COOL / BOX / PUSH NOW) and, during a cool lap,
   battery and tyre bars towards target.
5. Thresholds: `ers_needed_pct`, `ers_cool_gap_pct`, `cool_lap_factor`, `hot_lap_warning_m`.
