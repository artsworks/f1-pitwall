# 18: Race engine (M3) design

Implements `docs/05-roadmap.md` M3 on top of the M2 pipeline. Governing constraints:

- **Persistence over minimalism** (`docs/01-architecture.md`): every model reads its priors
  from SQLite, fits on rows already committed to SQLite, and writes fitted parameters back.
  In-memory state is a cache of the database, never the source of truth.
- **Two clock domains** (ADR 0002): all model maths uses session time; freshness and
  cooldowns use the tick clock.
- **No LLM in the rules** (ADR 0008, 0009): every call is a YAML rule over snapshot fields;
  under ADR 0009 an optional model may only pick among a rule's candidates. Predictions the
  rule speaks are snapshot fields too, so they land in `calls.inputs` for grading.
- **Thresholds live in YAML**; module code takes them as arguments.

## Module map

| Module | Role |
| --- | --- |
| `pitwall.store.db` | migrations 2, 8 and 9; read/write APIs for stints, pit events, model params |
| `pitwall.model.deg` | pace/degradation fit, laps-of-pace, priors |
| `pitwall.model.pitloss` | measured pit loss, track priors, cold-start overlay |
| `pitwall.model.budget` | fuel and 2026 energy per-lap budgets |
| `pitwall.state.race` | race phase machine, rival scope, weather crossover, discipline facts |
| `pitwall.strategy.pitwindow` | pit-window optimiser (undercut/overcut/free/cheap stop) |
| `pitwall.mask` | `--mask-restricted` datagram transformer |
| `pitwall.supervisor` | watchdog restarts the engine while the recorder continues |
| `pitwall.tune` | `pitwall tune`: grades and A/B results → stored cooldown multipliers |

## SQLite migration 2

Applied by the M2 runner (`PRAGMA user_version` 1 → 2). All new columns are nullable.

```sql
ALTER TABLE sessions ADD COLUMN game_mode INT;
ALTER TABLE sessions ADD COLUMN ended_at REAL;
ALTER TABLE laps ADD COLUMN wear_pct REAL;          -- mean of 4 corners at lap end
ALTER TABLE laps ADD COLUMN fuel_kg REAL;           -- fuel_in_tank at lap end
ALTER TABLE laps ADD COLUMN ers_deployed_j REAL;    -- ers_deployed_this_lap at lap end
ALTER TABLE laps ADD COLUMN sc_status INT;          -- max safety_car_status seen in the lap
ALTER TABLE laps ADD COLUMN weather INT;
ALTER TABLE stints ADD COLUMN n_valid_laps INT;
ALTER TABLE stints ADD COLUMN base_ms REAL;
ALTER TABLE stints ADD COLUMN deg_ms_per_lap REAL;
ALTER TABLE stints ADD COLUMN fuel_ms_per_lap REAL;
ALTER TABLE stints ADD COLUMN rmse_ms REAL;
ALTER TABLE stints ADD COLUMN updated_at REAL;
ALTER TABLE pit_events ADD COLUMN lane_ms INT;
ALTER TABLE pit_events ADD COLUMN in_lap_ms INT;
ALTER TABLE pit_events ADD COLUMN out_lap_ms INT;
ALTER TABLE pit_events ADD COLUMN ref_pace_ms INT;
ALTER TABLE pit_events ADD COLUMN car_idx INT;
CREATE TABLE model_params (
    track_id INT NOT NULL,
    compound INT NOT NULL,        -- 0 = compound-independent (e.g. pit loss)
    name TEXT NOT NULL,           -- 'deg_ms_per_lap' | 'base_ms' | 'fuel_ms_per_lap'
                                  -- | 'pit_loss_green_ms' | 'pit_loss_vsc_ms' | 'pit_loss_sc_ms'
                                  -- | 'fuel_kg_per_lap' | 'energy_j_per_lap'
    value REAL NOT NULL,
    weight REAL NOT NULL,         -- number of observations folded in
    updated_at REAL,
    PRIMARY KEY (track_id, compound, name)
);
CREATE TABLE ab_results (
    id INTEGER PRIMARY KEY,
    recorded_at REAL,
    recording TEXT,
    a_dir TEXT, b_dir TEXT, a_mindset TEXT, b_mindset TEXT,
    rule_id TEXT,
    only_a INT, only_b INT, both INT
);
CREATE TABLE runtime (
    key TEXT PRIMARY KEY,         -- 'heartbeat'
    session_uid INT,
    session_t REAL,               -- last session time processed
    wall_t REAL,                  -- time.time() at write
    recording_path TEXT,
    lap_num INT
);
```

`Database` APIs (all synchronous, one transaction each):

```python
def insert_lap(session_uid, car_idx, lap: LapSummary) -> None          # existing, extended cols
def laps_for(session_uid, car_idx=0) -> list[LapRow]
def upsert_stint(session_uid, car_idx, compound, start_lap, end_lap, fit: DegFit) -> None
def stints_for_track(track_id, compound, limit=20) -> list[StintRow]   # newest first, other sessions too
def insert_pit_event(session_uid, car_idx, lap_num, loss_ms, neutralised, lane_ms, in_lap_ms, out_lap_ms, ref_pace_ms) -> None
def pit_events_for_track(track_id, neutralised, limit=20) -> list[PitEventRow]
def get_param(track_id, compound, name) -> ModelParam | None
def fold_param(track_id, compound, name, value, weight=1.0) -> ModelParam
    # weighted running mean: new = (old*w_old + value*weight) / (w_old+weight); weight capped at param_weight_cap (default 50)
def params_for_track(track_id) -> list[ModelParam]
def record_ab(...) -> None
def write_heartbeat(session_uid, session_t, wall_t, recording_path, lap_num) -> None
def read_heartbeat() -> Heartbeat | None
def end_session(uid, ended_at) -> None
```

Rows are frozen dataclasses (`LapRow`, `StintRow`, `PitEventRow`, `ModelParam`, `Heartbeat`),
no dict-of-Any results.

## Track overlays (`config/tracks/<id>.yaml`)

Packaged at `src/pitwall/config/defaults/tracks/<track_id>.yaml`; user overlays at
`~/.pitwall/tracks/<track_id>.yaml` win. Schema (Pydantic `TrackOverlay`):

```yaml
track_id: 7
name: silverstone
pit_loss_s: {green: 21.5, vsc: 12.0, sc: 8.0}   # cold-start priors only
pit_exit_m: 320.0
pit_entry_m: 5700.0
fuel_kg_per_lap: 1.26
deg_ms_per_lap: {16: 110, 17: 70, 18: 45}       # by actual compound id
base_pace_ms: 0                                   # 0 = unknown
thresholds: {energy_over_tolerance_j: 600000}     # optional threshold overrides for this track
```

`ConfigStore.set_track(track_id: int | None)` selects the overlay; `_build()` deep-merges
`overlay.thresholds` and exposes `settings.track: TrackOverlay | None`. Engine calls
`store.set_track(snapshot.track_id)` when the track changes. The overlay contributes to the
config hash. When a `<id>.yaml` is absent `settings.track` is `None` and models use the
`thresholds` defaults (`pit_loss_default_s`, etc.).

**Prior resolution order** (`model.pitloss.resolve_prior`, `model.deg.resolve_prior`):
For degradation, enough earlier practice data in the same weekend takes precedence. For a
race, use the total-distance-scoped `model_params` value when its weight reaches
`prior_min_weight`. Otherwise, try the unscoped value, then the track overlay, then the
global default. Non-race stint values are unscoped. Never combine scoped values from
different race distances. Blend a learned degradation prior below `prior_full_weight`
laps toward the overlay or default in proportion to its weight. Scale its confidence by
the same proportion so one steep stint cannot set the plan alone.

Names include `deg_ms_per_lap@52L`, `base_ms@13L`, and `fuel_ms_per_lap@52L`. `N` is the
session's total race laps. Weekend degradation uses earlier fitted practice stints on the
same compound and track, with at least `weekend_min_laps` total.

For other priors, `model_params` with weight at least `prior_min_weight` wins, then the track
overlay, then the global default. Sources are reported as `weekend`, `learned`, `overlay`, or
`default`.

When a track has no learned or track-file base, use the median of this session's clean
laps after removing tyre-age degradation. Before the first clean lap, use the 95 s
`release_fallback_lap_s` fallback.

## `pitwall.model.deg`

```python
@dataclass(frozen=True, slots=True)
class DegFit:
    base_ms: float           # pace at tyre_age 0, fuel normalised to fuel_ref
    deg_ms_per_lap: float    # linear degradation slope
    fuel_ms_per_lap: float   # pace gained per lap of fuel burned (>= 0)
    n: int                   # valid laps used
    rmse_ms: float
    confidence: float        # 0..1: from n and rmse (see below)
    source: str              # 'fit' | 'prior' | 'blend'
    deg2_ms_per_lap2: float = 0.0
    deg_sd_ms_per_lap: float = 0.0

def fit_stint(
    laps: Sequence[LapRow], prior: DegFit, *, min_laps: int,
    fuel_coeff_fixed: float | None, thermal_hi_c: float | None = None,
    quadratic: bool = False, quad_min_laps: int = 8, quad_min_t: float = 2.0,
) -> DegFit
```

Fit valid laps only when `valid == 1` and `sc_status == 0`. Exclude `traffic == 1` and laps above `thermal_hi_c`.
Keep `tyre_inner_c == 0` because the temperature is unknown.

The engine reads `thermal_hi_c` from `model_params` when its weight reaches `prior_min_weight`.
Otherwise, it uses `thermal_window(settings.thresholds, compound)[1]`.

The fit uses `lap_time_ms = base + deg * tyre_age_laps - fuel * laps_of_fuel_burned`.
Fuel burn comes from the recorded `fuel_kg` drop since the stint's heaviest lap.
The game's `fuel_remaining_laps` is a spare-at-flag margin, so the engine uses it only as a fallback.

When fuel burn and tyre age correlate too closely, fix fuel to the prior.
Store that value as `deg_fuel_ref_ms_per_lap`. Re-split learned degradation with `deg + fuel_now - deg_fuel_ref`.
Only fits that estimate fuel update `fuel_ms_per_lap`.

With `n < min_laps` (default `th.deg_min_laps = 3`), return the prior.
Blend fits when `min_laps <= n < 2*min_laps` with `w = (n - min_laps + 1) / (min_laps + 1)`.
Fix fuel to `fuel_coeff_fixed` when supplied. Clamp linear slopes to `[0, th.deg_max_ms_per_lap]`.
Set confidence to `clamp(n / (2*min_laps), 0, 1) * clamp(1 - rmse_ms / th.deg_rmse_bad_ms, 0.2, 1)`.

Fit a quadratic only for an unblended `source == 'fit'` result with at least `quad_min_laps` laps and four distinct tyre ages.
Keep fuel fixed to the linear fit. Accept the quadratic only when `_solve` succeeds, `deg2 > 0`, and `deg >= 0`.
The slope at the last age must not exceed `th.deg_max_ms_per_lap`.
Require `deg2 >= quad_min_t * se(deg2)`, where `se(deg2) = rmse_quad * sqrt(inv(XᵀX)[2][2])`.
Rejected quadratic fits keep the linear result. Real fits store `deg_sd_ms_per_lap = rmse / sqrt(sum((age - mean_age)²))`.

`cliff_age(fit, cliff_ms)` solves `deg * age + deg2 * age² = cliff_ms`.
It uses the positive quadratic root, `(-deg + sqrt(deg² + 4*deg2*cliff_ms)) / (2*deg2)`.
For a positive linear slope, it returns `cliff_ms / deg`. Otherwise, it returns infinity.
`laps_of_pace` is `max(0, cliff_age - tyre_age)`, capped by the earliest of four corner wear limits.
Each corner uses its own wear rate since the stint baseline. The default rate applies until a lap has run.
The baseline resets on a compound change, tyre-age reset, or wear drop.

`planning_fit(fit, prior, deg_rmse_bad_ms)` scales both degradation terms by
`clamp(1 - rmse / deg_rmse_bad_ms, 0, 1)`. Clean fits are used as-is.
The live planner uses this adjusted fit.

**Rival pace** (`rival_pace_ms(history: SessionHistoryPacket, window: int) -> int`): median of
the last `window` (default `th.rival_pace_window = 3`) valid laps from Session History, 0 if none.
Rival rows go to `laps` with `car_idx = i`, `valid` = the packet's valid bit, so rival pace also
persists and `stints_for_track` can fit rival deg later.

**Persistence hook**: after inserting a player lap, refit and store the current stint. A
stint folds when the next stint starts or the session ends. The fold is deduplicated by
session and the stint's first lap. Only clean fits on known tracks fold:

- Live-tail planning may use a quadratic fit. Persist a linear fit in `stints` and learned priors.
- `source == "fit"` and `rmse_ms <= deg_rmse_bad_ms`
- `0 < deg_ms_per_lap < deg_max_ms_per_lap`
- `0 <= fuel_ms_per_lap < deg_max_ms_per_lap`
- `learn_base_min_ms <= base_ms <= learn_base_max_ms`

Race folds use the total-distance-scoped names above; non-race folds use unscoped names.

## SQLite migration 9

Automatic upkeep stores removed or replaced values and its run versions in:

```sql
CREATE TABLE model_params_quarantine (
    track_id INT NOT NULL,
    compound INT NOT NULL,
    name TEXT NOT NULL,
    reason TEXT NOT NULL,
    value REAL NOT NULL,
    weight REAL NOT NULL,
    updated_at REAL,
    quarantined_at REAL,
    PRIMARY KEY (track_id, compound, name, reason)
);
CREATE TABLE maintenance (
    key TEXT PRIMARY KEY,
    version INT NOT NULL,
    ran_at REAL
);
```

## SQLite migration 14

Migration 14 adds `laps.traffic INTEGER DEFAULT 0`. The engine sets the flag when more than half of race-lap ticks meet `traffic_gap_s`.
Tyre degradation fits exclude rows with `traffic = 1`.

## `pitwall.model.pitloss`

```python
@dataclass(frozen=True, slots=True)
class PitLoss:
    loss_ms: int
    lane_ms: int
    in_lap_ms: int
    out_lap_ms: int
    ref_pace_ms: int
    ref_after_ms: int
    neutralised: int

def measure(
    in_lap: LapRow, out_lap: LapRow, lane_ms: int, ref_pace_ms: int, neutralised: int,
    *, ref_after_ms: int | None = None,
) -> PitLoss
    # loss_ms = (in_lap_ms - ref_pace_ms) + (out_lap_ms - (ref_after_ms or ref_pace_ms)).
    # The lap times include the lane time. Store lane_ms for review only.
```

The in-lap is the lap where `pit_status` first changes from 0 to nonzero.
The out-lap is the next lap, even when the car reaches the box after crossing the line.
An early pit request does not invalidate a lap. `after_in_lap` marks the out-lap.

`ref_pace_ms` is the median of the last 3 valid player laps before the in-lap.
`ref_after_ms` is the median of the first up to `pit_ref_after_laps` valid laps after the out-lap.
It is 0 when no after-stop reference was used. If no valid lap follows the out-lap, measurement
uses `ref_pace_ms` for both laps.
`neutralised` is the max `sc_status` over the in-lap and out-lap. 0 means green, 1 means SC,
and 2 means VSC.
The engine waits for `pit_ref_after_laps` valid laps after the out-lap before it stores and folds
the event. It flushes a pending event before another pit, at session end, and before a session
reset. A flush uses the valid after-stop laps available at that time.
Measurement is skipped if either pit lap has the `flashback` or `red_flag` reason.

`current_pit_loss(db, track_id, neutralised, overlay, th) -> Prior` per the resolution order,
preferring **this session's** measured events (`pit_events` for this uid) over cross-session
learned params: this-session mean if ≥1 event, else learned, else overlay, else default.

## `pitwall.model.budget`

```python
@dataclass(frozen=True, slots=True)
class FuelBudget:
    laps_remaining: int
    fuel_laps: float
    margin_laps: float
    per_lap_kg: float
    source: str
    # margin_laps = fuel_laps - laps_remaining ; negative = short


@dataclass(frozen=True, slots=True)
class EnergyBudget:
    per_lap_j: float  # allowance per remaining lap = store_j / laps_remaining_in_stint_or_race, floored at 0
    deployed_this_lap_j: float
    harvested_this_lap_j: float
    lap_delta_j: float  # deployed - harvested - per_lap_j : >0 = over budget this lap
    store_pct: float
    soc_floor_pct: float
    laps_to_floor: (
        float  # at the current net drain, laps until store hits the floor (inf if not draining)
    )
    mode: str  # 'on_budget' | 'over' | 'under' | 'attack_ok'
```

`fuel_kg_per_lap` is measured from consecutive `laps.fuel_kg` deltas this session (median),
folded into `model_params 'fuel_kg_per_lap'`; falls back to the overlay/`th.fuel_kg_per_lap_default`.
Energy: `per_lap_j = max(0, (store_j - floor_j)) / max(1, laps_remaining) + harvest_limit_per_lap`
(2026: the store is a per-lap budget, not a threshold). `mode = 'over'` when
`lap_delta_j > th.energy_over_tolerance_j`, `'under'` when below `-tolerance`,
`'attack_ok'` when `laps_to_floor > laps_remaining + 1` and `mode.ers_policy == 'attack_rival'`.
The default tolerance is 400 kJ. The Silverstone track overlay raises it to 600 kJ.

## `pitwall.state.race`

Race phase machine (race sessions only; other kinds keep the M2 `_phase()`):

```
formation → racing → sc | vsc → racing …
racing → in_lap (pit_status becomes PITTING) → out_lap (pit_status back to 0, until the
  next lap boundary) → racing
any → finished (player result_status FINISHED, or CHQF event then the line crossed)
red_flag as in M2.
```

`formation` = `safety_car_status == FORMATION_LAP` or (lap_num <= 1 and no LGOT yet).
`sc`/`vsc` from `safety_car_status` (1/2) with a `th.sc_exit_hold_s` hysteresis so a flicker
doesn't bounce the phase. `SessionState.race_phase: str` and `Snapshot.phase` returns it in
races.

Snapshot fields added (all plain floats/ints/strs/bools so rules and JSON can use them):

```
laps_remaining: int                 # total_laps - lap_num + 1 (0 when unknown)
race_phase: str                     # as above
sc_laps: int                        # laps spent under the current SC/VSC
gap_ahead_s, gap_behind_s: float    # from delta_to_car_in_front (player) and the car behind's delta
rival_ahead_idx, rival_behind_idx, rival_pit_exit_idx: int   # -1 = none
rival_ahead_pace_ms, rival_behind_pace_ms, rival_pit_exit_pace_ms: int
rival_ahead_name, rival_behind_name, rival_pit_exit_name: str
true_rival_ahead_idx, true_rival_behind_idx: int # -1 = none, stop-adjusted order
true_rival_ahead_name, true_rival_behind_name: str
true_rival_ahead_gap_s, true_rival_behind_gap_s: float # stop-adjusted gaps
true_rival_ahead_stops_owed, true_rival_behind_stops_owed: int
true_rival_ahead_pitted, true_rival_behind_pitted: bool
rival_ahead_age, rival_behind_age: int         # tyre age from Session History stints (0 if unknown)
rival_ahead_age_known, rival_behind_age_known: bool # False if masked or no Session History
rival_ahead_pitted, rival_behind_pitted: bool  # that rival's pit_status was non-zero during the current lap
rival_ahead_in_pit_lane, rival_behind_in_pit_lane: bool  # validated rival is in the pit lane now
rival_ahead2_pos, rival_behind2_pos: int       # second car on each side (0 = none)
rival_ahead2_name, rival_behind2_name: str
rival_ahead2_gap_s, rival_behind2_gap_s: float # our gap to the rival plus its own delta to that car
rival_ahead2_overtake, rival_behind2_overtake: bool
# Ahead and behind latches keep each validated rival's pre-pit gap while it stops.
rival_data_restricted: bool         # rival Car Status/Damage fields all zero for >= th.restricted_detect_laps
pit_exit_rival_gap_s: float         # projected gap to rival_pit_exit_idx at pit exit (positive = rival ahead)
pit_exit_clean: bool                # release_window() at pit exit with the race gap threshold
deg_fit_source: str; deg_ms_per_lap: float; deg_confidence: float; base_pace_ms: float
laps_of_pace: float; wear_mean_pct: float; wear_per_lap_pct: float
blister_max_pct: int                # max tyre_blisters corner (Car Damage)
graining: bool                      # slow inner EMA in the graining band (< th.tyre_graining_c) with hysteresis
overheat: bool                      # slow inner EMA > th.tyre_inner_hot_c with hysteresis; never in the pit lane
pit_loss_s, pit_loss_source: float, str
fuel_margin_laps: float; fuel_per_lap_kg: float; fuel_source: str
energy_per_lap_mj, energy_lap_delta_mj, energy_laps_to_floor: float; energy_mode: str
drs_zone_ahead: bool; drs_available: bool (2026: overtake_active; pre-2026: drs_allowed and gap_ahead_s < 1.0 and not sc)
penalty_s: int; unserved_drive_through: int; unserved_stop_go: int; warnings: int; corner_cut_warnings: int
track_warning_kind: str              # 'cut' | 'limits'
track_warning_count: int             # shared corner-cut count for PENA 7-9 and 27-29, or Lap Data if higher
cut_warning_count: int; limit_warning_count: int  # PENA warning counts split by family
penalty_recent: bool                # PENA event for the player inside th.penalty_recent_s
blue_flag: bool                     # vehicle_fia_flags == 4 (blue)
weather_now: int; rain_pct_now: int; rain_pct_in_10: int; rain_pct_in_30: int
weather_crossover: str              # '' | 'to_inter' | 'to_wet' | 'to_dry' from forecast + th.rain_inter_pct/th.rain_wet_pct/th.rain_dry_pct
tyre_switch_net_s: float            # projected net gain of the tyre_switch_to call (s); 0 = no call
slip_trend: int                     # player slip events last completed lap minus the lap before
pit_plan: str                       # '' | 'stay' | 'box_now' | 'box_in_n' | 'undercut' | 'overcut' | 'cover' | 'defend' | 'free_stop' | 'cheap_stop'
pit_plan_lap: int; pit_plan_gain_s: float; pit_plan_confidence: float; pit_plan_risk: float
pit_plan_rival_idx: int; pit_plan_rival_name: str; pit_plan_reason: str
predicted_lap_ms: int               # model prediction for the *next* lap, for grading
```

Rival scope: `relevant_rivals(cars, player_idx, gap_behind_max_s, pit_exit_projection)` returns
`(ahead, behind, pit_exit)` indices. Pit-exit rival: `D_target = (D_player − L · T_pit/P) mod L`
(docs/03) and the car whose lap distance is nearest ahead of `D_target` among cars not pitting.

Restricted detection: track per-car `fuel_in_tank`, `ers_store_energy`, and mean `tyres_wear`
for all non-player active cars; when every one is exactly 0 for `th.restricted_detect_laps`
consecutive laps, `rival_data_restricted = True`. In restricted mode `rival_*_age` is 0 and the
optimiser's `confidence` for rival-dependent plans (undercut, overcut) is multiplied by
`th.restricted_confidence_factor` (default 0.7), so those calls degrade rather than fabricate;
plans from the player's own tyre model keep full confidence. `cheap_stop` confidence is the pit-loss source factor alone (measured wear and the
neutralised pit loss decide it, not the deg fit). Position-loss `risk` is 0 when
`laps_of_pace < laps_remaining − mode.tyre_life_buffer_laps` (the stop is forced, so the place
is lost whenever it is taken).

Driver rating uses the learned track profile or the global fallback described in
[driver rating](26-driver-rating.md). The optimiser lowers confidence and adds risk for
undercut, overcut, cover and defend. The pace cap changes our undercut laps and old-tyre
overcut laps, but not cover math. Monte Carlo uses the resulting confidence without a
separate driver input.

## `pitwall.strategy.pitwindow`

```python
@dataclass(frozen=True, slots=True)
class PitPlan:
    plan: str; lap: int; gain_s: float; confidence: float; risk: float; rival_idx: int; reason: str
    projections: tuple[tuple[int, float], ...]   # (candidate_lap, race_time_delta_s) for review
    mc_lap: int = 0
    mc_agree: float = 1.0

@dataclass(frozen=True, slots=True)
class McResult:
    modal_lap: int
    agree: float
    sims: int

def plan_agreement(...) -> McResult | None

def optimise(
    *, lap_num, laps_remaining, tyre_age, wear_mean, fit: DegFit, wear_per_lap, pit_loss_s,
    laps_of_pace, sc_status, rival_ahead: RivalView | None, rival_behind: RivalView | None,
    gap_ahead_s, gap_behind_s, pit_exit_clean, restricted, driver: DriverScale = NEUTRAL,
    mode: Mapping[str, float|str], th: Mapping[str, float],
    horizon: int,
) -> PitPlan
```

Race-time model over the horizon (default `th.pit_horizon_laps = 8`): for each candidate stop
lap `k` in `[lap_num, lap_num + horizon]` (and "no stop" if `laps_of_pace >= laps_remaining`),
sum predicted lap times using `fit` on the current tyre until `k`, add `pit_loss_s`, then a
fresh-tyre stint from the prior `DegFit` for the *next* compound (same compound prior if
unknown). The best `k` minimises total time; `gain_s` = best − stopping now (positive means
waiting gains). Decisions:

- `cheap_stop`: `sc_status != 0` and `wear_mean >= mode.sc_stop_min_wear_pct` and
  `laps_remaining > th.sc_stop_min_laps_left` → `plan='cheap_stop', lap=lap_num`.
  The pit loss used is the SC/VSC prior.
- `free_stop`: pitting now costs no position: `gap_behind_s > pit_loss_s + th.free_stop_margin_s`
  and `pit_exit_clean` → `plan='free_stop'` when `best_k <= lap_num + 1`.
- `undercut` requires `gap_ahead_s < th.undercut_max_gap_s` and an open pit window, `window[0] <= lap_num`.
  The predicted fresh-tyre gain over `th.undercut_laps` must reach `gap_ahead_s + mode.undercut_speak_threshold_s`.
  It also requires `laps_remaining > th.undercut_laps + th.pit_min_laps_left`.
  The optimiser returns `plan='undercut', lap=lap_num`.
- `overcut` requires `rival_ahead.pitted`, `laps_of_pace >= th.overcut_min_laps`, and a predicted gain of at least `mode.pit_gain_min_s`.
  It also requires `laps_remaining > th.overcut_laps` and a possible stop.
  The optimiser returns `plan='overcut', lap=lap_num + th.overcut_laps`.
- `cover` requires a pitted rival behind, a finite gap below `th.cover_max_gap_s`, and a possible stop.
  It compares our current-tyre pace with the shared fresh-tyre fit over `th.undercut_laps`.
  `cover_s = round(sum(current_lap_ms - fresh_fit_lap_ms) / 1000 - outlap_warmup_s - gap_behind_s, 2)`.
  A positive value means the rival could jump us. The optimiser returns `cover` when
  `cover_s + th.cover_margin_s > 0`, with `risk = 0`.
- Defaults are `cover_margin_s=0.5`, `cover_max_gap_s=3.0`, and `cover_defend_gain_s=1.0`.
- `defend` requires the same rival and gap gates, `-cover_s >= th.cover_defend_gain_s`,
  `laps_of_pace >= th.overcut_min_laps`, and `best_k >= 1`.
  The optimiser returns `defend` for `lap_num + th.undercut_laps`.
  Cover and defend use the true rival behind and its stop-adjusted gap.
- The engine holds a `cover` or `defend` plan for the same rival after the pit-lane latch clears.
  It keeps the plan through the current or next lap during green racing with enough laps left.
  A phase change, safety car, cheap stop, or later lap clears the held plan.
- otherwise `box_now` if `best_k == lap_num`, `box_in_n` if `best_k <= lap_num + th.box_in_max_laps`,
  else `stay`.

The optimiser sets `undercut_s` and `overcut_s` to zero when their remaining-lap requirements fail.
When no stop is possible, the optimiser returns `no_stop` with both values at zero.
The snapshot therefore excludes tyre gains that cannot affect the finish.

`deterministic_confidence = fit.confidence * (restricted_factor if restricted else 1) * pit_loss_conf` where
`pit_loss_conf = 1.0 learned/this-session, 0.8 overlay, 0.6 default`.
`risk` estimates position loss: `clamp((pit_loss_s − gap_behind_s) / pit_loss_s, 0, 1)`
(0 when the car behind is further back than the pit loss). A `cover` plan sets risk to zero.
Rules gate on
`pit_plan_confidence >= mode.pit_confidence_min`, `pit_plan_gain_s >= mode.pit_gain_min_s`,
`pit_plan_risk <= mode.position_loss_risk_max`.

On green laps, the engine runs `plan_mc_sims` simulations over the same candidate range as `optimise`.
Each simulation samples current linear degradation with `sigma = max(deg_sd_ms_per_lap, plan_mc_deg_sd_frac * deg_ms_per_lap)`.
When the fit is a blend with positive posterior spread and at least `max(deg_min_laps, 3)` laps, sample a Student-t value instead.
Use `df = max(fit.n - 2, 3)` and scale `max(deg_posterior_sd_ms_per_lap, sigma)`.
Full fits and other blends keep the normal draw.
Clip sampled slopes at zero and keep quadratic degradation fixed.
Fresh-tyre lap times stay deterministic.

With three or more green pit events, sample `rng.choice(samples) - median(samples) + pit_loss_s`.
Otherwise, sample `N(pit_loss_s, plan_mc_pit_loss_sd_s)` and clip the result to a positive value.
For each candidate lap, sample a safety-car event with `plan_mc_sc_prob_per_lap`.
Use the first event at `j0` for stops from `j0` through `j0 + 2`.

Compute each stop delta against the no-stop time:

```text
delta[s,k] = (cumcur[s,k] + cumfresh[R-k] + (pit[s,k] + outlap_warmup_s) * 1000 - cumcur[s,R]) / 1000
```

Select the first minimum. Choose no stop when the best stop delta is nonnegative.
`modal_lap` is the most common simulated choice. Break ties at the smallest lap.
`plan_mc_risk_quantile` defaults to `0.8`. The engine clamps it to `0.001` to `0.999` before calculating quantiles.
`q_modal_lap` is the first minimum candidate lap when its quantile is negative, or zero otherwise.
`PitPlan.mc_risk_lap` carries this downside stop lap. The modal lap and agreement keep their existing meanings.
For a no-stop plan, agreement counts simulations with `min(delta) >= -mode.pit_gain_min_s`.
For a candidate stop, agreement counts simulations within `mode.pit_gain_min_s` of their best stop.
Plans outside the candidate range get agreement `1.0`.
For `cover` and `defend`, agreement counts how often sampled cover math repeats the plan's decision.

Multiply deterministic confidence by agreement and round to three decimals. Keep `plan.lap` unchanged.
Skip Monte Carlo when `safety_car_status != 0`. Do not scale `cheap_stop`.
Seed each run with `np.random.default_rng([uid & 0xFFFFFFFF, (uid >> 32) & 0xFFFFFFFF, lap_num])`.
Use UID zero when the session UID is absent.
Cache results once per lap with `(session_uid, lap_num, plan.lap, tyre_age, plan.plan, plan.rival_idx)`.
Defaults are `plan_mc_sims: 500`, `plan_mc_risk_quantile: 0.8`, `plan_mc_sc_prob_per_lap: 0.03`, `plan_mc_deg_sd_frac: 0.15`, and `plan_mc_pit_loss_sd_s: 1.5`.

## Named strategy plans (`pitwall.strategy.plans`)

Plan A/B/C portfolio on top of the tactical optimiser (model and calls in docs/03
"Strategy plans"). `Engine._plans()` runs after `optimise()` on each lap, stop, SC/VSC or
compound change, feeds `PlanTracker.update()` and folds `PlanFields` into `ModelView` and
the `Snapshot` (`active_plan`, `on_plan`, `plan_spoken`, `plan_target_lap`,
`plan_window_start/end`, `plan_window_open`, `plan_off_s`, `plan_switch_*`,
`plan_off_prob`, `plan_delta_sd_s`, `plan_b_spoken`, `plan_b_delta_sd_s`,
`plan_b_switch_prob`, `plan_c_spoken`, ...). Thresholds: `plan_*` in `settings.yaml`.

`PlanEval.race_time_sd_s` estimates uncertainty from degradation error. For each stint,
multiply its degradation posterior SD by the sum of tyre ages in that stint.
All stint terms share one slope error, so add them directly and convert milliseconds to seconds.
Plan B ranks alternatives by `race_time_s + z * race_time_sd_s`, where `z` is the inverse
normal CDF of `plan_mc_risk_quantile`, clamped to `0.001` to `0.999`.
The difference spread is `abs(sd_active - sd_best)`. When either plan has a spread, switch only when
`p_faster(active_time - best_time, active_sd, best_sd) > plan_switch_prob`.
Without a spread, the existing `plan_switch_s` threshold applies.
The active plan reports zero `switch_prob` and `delta_sd_s`. Other plans report their
probability of beating the active plan and their combined time spread.

Migration 3 (append-only): `calls.active_plan TEXT`, `calls.on_plan INT` and a
`plan_events(session_uid, t, session_time, lap, kind, from_plan, to_plan, reason, delta_s,
sequence, plans JSON)` table; `Database.insert_plan_event()` /
`plan_events_for_session()`.

## Battle state (`pitwall.strategy.battle`)

`Engine._battle()` runs every racing tick after the plans and classifies the fight around
the player (first match wins):

| mode | condition |
|---|---|
| `defending` | car behind within `battle_defend_gap_s` (1.0 s) |
| `attacking` | car ahead within the mindset's `attack_window_s` |
| `under_threat` | car behind within `battle_threat_gap_s` (3 s), closing ≥ `battle_closing_min_s` and in range before the flag |
| `catching` | car ahead within `battle_catch_gap_s` (5 s), closing ≥ `battle_closing_min_s` and in range before the flag |
| `managing` | a car within catch/threat range but gaps stable |
| `free_air` | otherwise |

Closing rate = measured gap trend when available, else the rival pace delta. Attack and
defend ranges get `gap_hysteresis_s` once entered. Snapshot fields: `battle_mode`,
`battle_mode_laps`, `battle_catch_laps`, `battle_threat_laps`, `battle_closing_*_s`,
`battle_tyre_offset_*`, `battle_tyre_known_*`, `battle_pass_prob`, `battle_hold_prob`, `battle_result`,
`battle_result_recent`, `battle_result_name`, `battle_swaps` (place changes
inside `battle_swap_window_s`).

`gap_closing_sd_ahead_s` is the sample standard deviation of consecutive gap-closing
changes over the trailing valid run for the current car ahead. The history keeps
`battle_closing_window_laps + 1` records, with a default window of five laps.
`catch_prob` estimates the chance of reaching the attack gap before the flag. It uses
the closing-rate mean and `closing_sd_s * sqrt(laps_remaining)` when spread is known.
Without spread, the catch result stays deterministic. `chase_prob` is `catch_prob`
times the pass rate with overtake active. Snapshot fields include `battle_catch_prob`
and `battle_chase_prob`.

**Episodes and the pass model.** Each attacking / defending spell against one rival is an
episode, closed when the mode or rival changes: attack → `passed` (position gained and the
rival no longer ahead) or `failed`; defend → `held` or `lost`. Episodes shorter than
`battle_min_episode_s` that change nothing are dropped. Each is written to the decision log
(`outcome: "battle"`) and folded into `model_params` (track, compound 0) as
`battle_pass_overtake`, `battle_pass_no_overtake` or `battle_hold`. Live `battle_pass_prob` /
`battle_hold_prob` are those rates shrunk toward the `battle_*_prior` settings with
`battle_prior_weight` pseudo-episodes, so one race can't swing them. The default pass priors
(`battle_pass_overtake_prior` and `battle_pass_no_overtake_prior`) are both 0.6.

**Calls** (shared `battle` cooldown group, budgeted; mode calls re-arm each lap so a
budget-suppressed call is retried): `battle_catching` ("Push now…, on him in 4 laps"),
`battle_attack` ("Hammer time", only when `battle_pass_prob ≥ battle_attack_prob_min`),
`battle_patience` (stuck in range where passing is hard: "stay with him, look after the
tyres"), `battle_defend` (P2), `battle_under_threat`, `battle_manage` (every
`battle_manage_every_laps`), and encouragement `battle_passed` / `battle_held` /
`battle_lost`. `battle_let_by` (P2) tells the driver to let a chaser pass when the
hold rate is low, the chaser has fresher tyres, and enough laps remain. Its thresholds
are `battle_letby_hold_max`, `battle_letby_tyre_laps`, and `battle_letby_min_laps`.

## Rules (`config/defaults/rules/race_*.yaml`)

All `sessions: [race]`, hysteresis via `clear_when`, per-lap budget via `mode.call_budget_per_lap`
(dispatcher `budget_override` follows the mindset), predictions in `inputs` because rules
reference the `pit_plan_*`/`predicted_lap_ms`/`laps_of_pace` fields in `when`/`say`.

| id | P | when (sketch) |
| --- | --- | --- |
| `box_now` | 1 | `pit_plan in ('box_now','cheap_stop','free_stop','undercut') and pit_plan_confidence >= mode.pit_confidence_min and pit_plan_risk <= mode.position_loss_risk_max` |
| `box_in_n` | 2 | `pit_plan == 'box_in_n' and confidence gate` cooldown 60 |
| `overcut_stay_out` | 2 | `pit_plan == 'overcut'` |
| `cover_box` | 1 | `pit_plan == 'cover' and confidence and risk gates; laps remain above pit minimum` |
| `overcut_defend` | 1 | `pit_plan == 'defend' and confidence gate; laps remain above pit minimum` |
| `tyre_life` | 3 | every `mode.rival_info_every_n_laps` laps: "these tyres have about {laps_of_pace:.0f} laps of pace" |
| `tyre_overheat` / `tyre_graining` | 2 | `overheat` / `graining` with cooldown 90 |
| `tyre_blister` | 2 | `blister_max_pct >= th.blister_warn_pct` |
| `fuel_short` | 1 | `fuel_margin_laps < -mode.lift_coast_trigger_laps` |
| `fuel_marginal` | 2 | `fuel_margin_laps < mode.fuel_margin_laps` |
| `fuel_spare` | 3 | `fuel_margin_laps > th.fuel_spare_laps` once per N laps |
| `energy_over` / `energy_under` | 2/3 | `energy_mode == 'over'`/`'under'` at lap end |
| `overtake_mode` | 2 | `energy_mode == 'attack_ok' and overtake_active and aero_zone_ahead and gap_ahead_s <= mode.overtake_call_gap_s` |
| `overtake_earned` / `overtake_lost` | 3/3 | `overtake_available and not overtake_active` / `overtake_active and not overtake_available and gap_ahead_s < th.overtake_lost_call_gap_s` |
| `energy_charge_for_battle` | 3 | sector 1, `battle_mode == 'catching'`, `battle_catch_laps <= th.battle_charge_catch_laps`, `ers_store_pct < th.battle_charge_store_pct` |
| `battle_let_by` | 2 | low hold rate, chaser on fresher tyres, and `battle_threat_laps <= laps_remaining * 0.5` |
| `energy_burst` | 3 | sector 2, `battle_mode == 'attacking' and energy_mode == 'on_budget' and ers_store_pct < th.energy_burst_store_pct` |
| `penalty` | 1 | `penalty_recent and penalty_kind == 'time'` (PENA types 0/1/4 only; warnings, lap invalidations and retirements carry `time_s = 255` and are not penalties) |
| `penalty_pit` | 1 | `penalty_kind in ('drive_through','stop_go')` |
| `serve_penalty` | 2 | `unserved_drive_through + unserved_stop_go > 0 and pit_plan in ('box_now','box_in_n')` |
| `track_limit_warning` / `track_limit_last_warning` | 2/1 | Running-wide warnings come from PENA 27–29. They share the count with PENA 7–9. The rule keeps its running-wide wording. |
| `corner_cut_warning` / `corner_cut_last_warning` | 2/1 | Corner-cut warnings come from PENA 7–9. They share the count with PENA 27–29. The rule keeps its corner-cut wording. |
| `penalty_corner_cut` | 1 | time penalty for a corner cut (infringement 7–9) |
| `blue_flag` | 1 | `blue_flag` cooldown 20 |
| `rival_ahead_pitted` / `rival_behind_pitted` | 2 | Ahead call uses `rival_ahead_in_pit_lane`; behind call uses `rival_behind_in_pit_lane`, tyre age ≥ 2, and the pit-minimum lap gate |
| `weather_crossover` | 2 | `weather_crossover != ''` |
| `sc_deployed` / `vsc_deployed` / `sc_ending` | 1 | phase transitions |
| `lights_out` | 3 | LGOT: "laps_remaining, fuel margin, plan" |
| `pit_exit_traffic_race` | 2 | out_lap and `pit_exit_rival_gap_s < th.pit_exit_traffic_s` |

Fuel burn per lap: this session's measured burn (median of green-lap deltas, ≥ 2 laps)
→ the game's own MFD estimate `fuel_in_tank / (laps_remaining + fuel_remaining_laps)` →
the learned/overlay/default prior. Energy allowance is net store drawdown per lap,
`(store − floor) / laps_remaining`, compared against `deployed − harvested`; the energy
calls skip laps 1–2, which spend the launch charge.

Race phase: `in_lap` starts on the *transition* into driver status IN_LAP (the game can
hold it there for the rest of the race) or on pit entry; `out_lap` only follows an
observed pit-lane visit.

Setup feedback: brake bias (`front_brake_bias`, Car Status) is read live and quoted in
lock-up advice (rear lock → bias forward; the same front lock-up on 3+ laps → brake
earlier or bias rearward). The differential comes from Car Setups
(`setup_on_throttle_diff` / `setup_off_throttle_diff`), which the game rebroadcasts after
in-race MFD changes (the reviewed race recording shows on-throttle 60 → 50 and bias
57 → 56 mid-race). Race setup calls come from the setup advisor (`22-setup-advisor.md`):
`setup_bias` and `setup_on_throttle` quote the live value and the target, and
`setup_stop_wing` asks for a front-wing value when a stop is planned. Off-throttle diff is
never advised in a race, because it can't change on track.

`mindset` remains a first-class field of every decision-log record; `Engine.apply_settings()`
propagates a live mindset change to the rule engine (`mode`), dispatcher (`budget_override`)
and decision log (`mindset`). The dashboard sends `{"type":"mindset","name":"aggressive"}`
over the WebSocket; the server sets `store.set_override(("mindset","active"), name)`.

## `pitwall replay --mask-restricted`

`pitwall.mask.mask_restricted(payload: bytes, player_idx: int) -> bytes` zeroes, for every
non-player car, Car Status `fuel_in_tank`, `fuel_capacity`, `fuel_remaining_laps`,
`ers_store_energy`, `ers_deployed_this_lap`, `ers_harvested_this_lap_mguk/mguh`,
`fuel_mix`, `ers_deploy_mode`; Car Damage `tyres_wear`, `tyres_damage`, `tyre_blisters`; and
Tyre Sets packets for non-player cars entirely (all `wear`). Uses `car_field_offset`. `Ingest`
gains an optional `transform: Callable[[bytes], bytes]` applied after the header parse, before
the recorder (so a masked replay recorded again stays masked). `run_replay(..., mask_restricted=True)`.

## Recovery

`Engine.tick` writes `runtime.heartbeat` every `th.heartbeat_s` (default 2 s of session time).
`pitwall start` on boot: `recovery.plan(db, recordings_dir) -> RecoveryPlan | None` when the
heartbeat's session has no `ended_at` and `wall_t` is younger than `th.recovery_max_age_s`
(default 1800). `recover(engine, plan)`: load `laps_for(uid)` into `SessionState` (so the deg
fit and pit loss have their history), then replay the recording tail from the `.f1idx` lap entry
at `heartbeat.lap_num` through the current engine with the rule engine muted (dispatcher
`quiet` for the tail) so state is rebuilt without re-speaking old calls. Logged as
`{"outcome": "recovered", "from_lap": …, "tail_s": …}`.

Watchdog (`pitwall.supervisor`). `pitwall start` is split into two processes, so the
recorder is the first thing to start and the last to stop:

```
pitwall start                     # supervisor: binds udp_port, RecordingRotator, watchdog
  └─ engine child process          # rules, dashboard, speech on 127.0.0.1:engine_port
```

- **Supervisor:** writes each datagram to the recording, then forwards it to the child. It
  writes the live recording path to `recordings/.runtime/current_recording`; the child's
  heartbeat and `recover()` read the recording through that pointer.
- **Crash:** if the child exits non-zero, it is respawned. The backoff doubles up to
  `engine.watchdog_backoff_max_s`, and resets once the child has run `watchdog_reset_s`.
  Exit 0 (Ctrl-C) stops everything.
- **Hang:** if `recordings/.runtime/engine.alive`, which `run_live` touches every second, is
  older than `watchdog_stall_s` after `watchdog_grace_s`, the child is killed and respawned.
- **Rejoin:** the respawned child runs `recover()`. The rule engine sees the replayed tail,
  so conditions that were already true don't all fire on the first live tick. The child then
  speaks `Engine.rejoin_text()`: "Back with you. Lap 30, P4. 21 to go."
- **Clean exit:** a clean child exit calls `clear_heartbeat()`, so the next `start` is a
  fresh session and not a recovery.
- `pitwall start --no-watchdog` keeps the old single-process mode.

## Rule tuning

`pitwall tune [PATHS...] [--calls-mode on|off] [--db PATH]` ingests optional recordings,
then folds human grades and hindsight outcomes into per-rule cooldown multipliers in SQLite.
Outcomes count only when no human grade exists for that call. It also reports per-rule A/B
net counts saved by `pitwall diff --record`. The live dispatcher loads saved cooldown
multipliers at start.

## Fixtures and tests

Real recordings stay out of git (ADR 0005); `tests/race_synth.py` builds a deterministic
synthetic race stream (`race_stream(laps=N, ...)`) with Session, Lap Data (player + 3 rivals),
Car Status, Car Damage, Car Telemetry, Session History and Event packets on a 5 km track at 5 Hz
with configurable deg slope, fuel, SC window, rival gaps, penalty and weather forecast.
`scripts/make_m3_fixture.py` writes it to `~/m3-race-25.f1bin` / `~/m3-race-100.f1bin` for
`pitwall replay` / review. Every race rule has a replay test asserting it fires (and
does not fire on the control stream), and `test_race_budget.py` asserts calls per lap never
exceeds `mode.call_budget_per_lap` on the 100% stream.
