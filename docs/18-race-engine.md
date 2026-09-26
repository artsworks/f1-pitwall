# 18 — Race engine (M3) design

Implements `docs/05-roadmap.md` M3 on top of the M2 pipeline. Governing constraints:

- **Persistence over minimalism** (`docs/01-architecture.md`): every model reads its priors
  from SQLite, fits on rows already committed to SQLite, and writes fitted parameters back.
  In-memory state is a cache of the database, never the source of truth.
- **Two clock domains** (ADR 0002): all model maths uses session time; freshness and
  cooldowns use the tick clock.
- **No LLM** (ADR 0008): every call is a YAML rule over snapshot fields. Predictions the
  rule speaks are snapshot fields too, so they land in `calls.inputs` for grading.
- **Thresholds live in YAML**; module code takes them as arguments.

## Module map

| Module | Role |
| --- | --- |
| `pitwall.store.db` | migration 2 (below); read/write APIs for stints, pit events, model params |
| `pitwall.model.deg` | pace/degradation fit, laps-of-pace, priors |
| `pitwall.model.pitloss` | measured pit loss, track priors, cold-start overlay |
| `pitwall.model.budget` | fuel and 2026 energy per-lap budgets |
| `pitwall.state.race` | race phase machine, rival scope, weather crossover, discipline facts |
| `pitwall.strategy.pitwindow` | pit-window optimiser (undercut/overcut/free/cheap stop) |
| `pitwall.mask` | `--mask-restricted` datagram transformer |
| `pitwall.recovery` | watchdog heartbeat + crash recovery |
| `pitwall.tune` | `pitwall tune`: grades + diff results → priors / learned overlay |

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
fuel_kg_per_lap: 1.75
deg_ms_per_lap: {16: 110, 17: 70, 18: 45}       # by actual compound id
base_pace_ms: 0                                   # 0 = unknown
thresholds: {}                                    # optional threshold overrides for this track
```

`ConfigStore.set_track(track_id: int | None)` selects the overlay; `_build()` deep-merges
`overlay.thresholds` and exposes `settings.track: TrackOverlay | None`. Engine calls
`store.set_track(snapshot.track_id)` when the track changes. The overlay contributes to the
config hash. When a `<id>.yaml` is absent `settings.track` is `None` and models use the
`thresholds` defaults (`pit_loss_default_s`, etc.).

**Prior resolution order** (`model.pitloss.resolve_prior`, `model.deg.resolve_prior`):
1. `model_params` row with `weight >= th.prior_min_weight` (default 2) → learned prior.
2. Track overlay value.
3. Global threshold default.
Returned as `Prior(value, weight, source)` with `source in {"learned","overlay","default"}`;
`source` flows to the snapshot so calls say where their number came from.

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

def fit_stint(laps: Sequence[LapRow], prior: DegFit, *, min_laps: int, fuel_coeff_fixed: float | None) -> DegFit
```

Fit: ordinary least squares on valid laps only (`valid == 1`, plus `sc_status == 0`) of
`lap_time_ms = base + deg * tyre_age_laps + fuel * (fuel_ref - fuel_remaining_laps_at_end)`.
With `n < min_laps` (default `th.deg_min_laps = 3`) return the prior with `source='prior'`.
With `min_laps <= n < 2*min_laps` blend: `w = (n - min_laps + 1) / (min_laps + 1)`,
`fit = w*ols + (1-w)*prior`, `source='blend'`. Fuel slope is fixed to `fuel_coeff_fixed`
when given (from `model_params fuel_ms_per_lap`) so the 2-parameter fit is well-conditioned on
short stints; slopes are clamped to `[0, th.deg_max_ms_per_lap]`.
`confidence = clamp(n / (2*min_laps), 0, 1) * clamp(1 - rmse_ms / th.deg_rmse_bad_ms, 0.2, 1)`.

```python
def laps_of_pace(fit: DegFit, tyre_age: int, wear_pct: float, *, cliff_ms: float, wear_cliff_pct: float, wear_per_lap: float) -> float
```

Remaining laps before the tyre is `cliff_ms` slower than at age 0 **or** mean wear crosses
`wear_cliff_pct`, whichever is sooner: `min((cliff_ms/deg - age), (wear_cliff - wear)/wear_per_lap)`.
`wear_per_lap` is measured this stint (Δmean wear / Δlaps, from `laps.wear_pct`), falling back
to `th.wear_per_lap_default_pct`. Returned ≥ 0.

**Rival pace** (`rival_pace_ms(history: SessionHistoryPacket, window: int) -> int`): median of
the last `window` (default `th.rival_pace_window = 3`) valid laps from Session History, 0 if none.
Rival rows go to `laps` with `car_idx = i`, `valid` = the packet's valid bit, so rival pace also
persists and `stints_for_track` can fit rival deg later.

**Persistence hook** (`Engine._write_laps`): after inserting a player lap, refit the current
stint from `db.laps_for(uid)` (filtered to the current stint range) and `upsert_stint`. When a
stint ends (compound or `tyre_age_laps` resets to 0 while on track), `fold_param(track,
compound, 'deg_ms_per_lap', fit.deg_ms_per_lap, weight=fit.n)` and likewise `base_ms`,
`fuel_ms_per_lap` if `fit.source == 'fit'`.

## `pitwall.model.pitloss`

```python
@dataclass(frozen=True, slots=True)
class PitLoss:
    loss_ms: int; lane_ms: int; in_lap_ms: int; out_lap_ms: int; ref_pace_ms: int; neutralised: int

def measure(in_lap: LapRow, out_lap: LapRow, lane_ms: int, ref_pace_ms: int, neutralised: int) -> PitLoss
    # loss_ms = (in_lap_ms - ref) + (out_lap_ms - ref). The lane time is already inside
    # the in/out lap times; lane_ms is stored for review only.
```

`ref_pace_ms` = median of the last 3 valid player laps before the in-lap (from SQLite).
`neutralised` = max `sc_status` over in-lap/out-lap (0 green, 1 SC, 2 VSC).
On out-lap completion: `insert_pit_event`, then `fold_param(track, 0, 'pit_loss_{green|sc|vsc}_ms', loss_ms)`.
Measurement is skipped if either lap is flagged `flashback` or `red_flag`.

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
rival_ahead_age, rival_behind_age: int         # tyre age from Session History stints (0 if masked/unknown)
rival_ahead_pitted, rival_behind_pitted: bool  # that rival's pit_status was non-zero during the current lap
rival_data_restricted: bool         # rival Car Status/Damage fields all zero for >= th.restricted_detect_laps
pit_exit_rival_gap_s: float         # projected gap to rival_pit_exit_idx at pit exit (positive = rival ahead)
pit_exit_clean: bool                # release_window() at pit exit with the race gap threshold
deg_fit_source: str; deg_ms_per_lap: float; deg_confidence: float; base_pace_ms: float
laps_of_pace: float; wear_mean_pct: float; wear_per_lap_pct: float
blister_max_pct: int                # max tyre_blisters corner (Car Damage)
graining: bool                      # slow inner EMA in the graining band (< th.tyre_graining_c) with hysteresis
overheat: bool                      # slow inner EMA > th.tyre_inner_hot_c with hysteresis
pit_loss_s, pit_loss_source: float, str
fuel_margin_laps: float; fuel_per_lap_kg: float; fuel_source: str
energy_per_lap_mj, energy_lap_delta_mj, energy_laps_to_floor: float; energy_mode: str
drs_zone_ahead: bool; drs_available: bool (drs_allowed and gap_ahead_s < 1.0 and not sc)
penalty_s: int; unserved_drive_through: int; unserved_stop_go: int; warnings: int; corner_cut_warnings: int
penalty_recent: bool                # PENA event for the player inside th.penalty_recent_s
blue_flag: bool                     # vehicle_fia_flags == 4 (blue)
weather_now: int; rain_pct_now: int; rain_pct_in_10: int; rain_pct_in_30: int
weather_crossover: str              # '' | 'to_inter' | 'to_wet' | 'to_dry' from forecast + th.rain_inter_pct/th.rain_wet_pct/th.rain_dry_pct
pit_plan: str                       # '' | 'stay' | 'box_now' | 'box_in_n' | 'undercut' | 'overcut' | 'free_stop' | 'cheap_stop'
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
optimiser's `confidence` is multiplied by `th.restricted_confidence_factor` (default 0.7), so
calls degrade rather than fabricate.

## `pitwall.strategy.pitwindow`

```python
@dataclass(frozen=True, slots=True)
class PitPlan:
    plan: str; lap: int; gain_s: float; confidence: float; risk: float; rival_idx: int; reason: str
    projections: tuple[tuple[int, float], ...]   # (candidate_lap, race_time_delta_s) for review

def optimise(
    *, lap_num, laps_remaining, tyre_age, wear_mean, fit: DegFit, wear_per_lap, pit_loss_s,
    laps_of_pace, sc_status, rival_ahead: RivalView | None, rival_behind: RivalView | None,
    gap_ahead_s, gap_behind_s, pit_exit_clean, restricted, mode: Mapping[str, float|str], th: Mapping[str, float],
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
- `undercut`: viable when `gap_ahead_s < th.undercut_max_gap_s` and predicted fresh-tyre delta over `th.undercut_laps`
  laps versus the rival's current pace (`rival_ahead.pace_ms` vs our fresh prediction) exceeds
  `gap_ahead_s + mode.undercut_speak_threshold_s` → `plan='undercut', lap=lap_num`.
- `overcut`: rival ahead has pitted this lap (`rival_ahead.pitted`) and our `laps_of_pace >=
  th.overcut_min_laps` and predicted our-pace-vs-their-out-lap gain > `mode.pit_gain_min_s` →
  `plan='overcut', lap=lap_num + th.overcut_laps`.
- otherwise `box_now` if `best_k == lap_num`, `box_in_n` if `best_k <= lap_num + th.box_in_max_laps`,
  else `stay`.

`confidence = fit.confidence * (restricted_factor if restricted else 1) * pit_loss_conf` where
`pit_loss_conf = 1.0 learned/this-session, 0.8 overlay, 0.6 default`.
`risk` = probability-ish of losing a position: `clamp((pit_loss_s − gap_behind_s) / pit_loss_s, 0, 1)`
(0 when the car behind is further back than the pit loss). Rules gate on
`pit_plan_confidence >= mode.pit_confidence_min`, `pit_plan_gain_s >= mode.pit_gain_min_s`,
`pit_plan_risk <= mode.position_loss_risk_max`.

## Rules (`config/defaults/rules/race.yaml`)

All `sessions: [race]`, hysteresis via `clear_when`, per-lap budget via `mode.call_budget_per_lap`
(dispatcher `budget_override` follows the mindset), predictions in `inputs` because rules
reference the `pit_plan_*`/`predicted_lap_ms`/`laps_of_pace` fields in `when`/`say`.

| id | P | when (sketch) |
| --- | --- | --- |
| `box_now` | 1 | `pit_plan in ('box_now','cheap_stop','free_stop','undercut') and pit_plan_confidence >= mode.pit_confidence_min and pit_plan_risk <= mode.position_loss_risk_max` |
| `box_in_n` | 2 | `pit_plan == 'box_in_n' and confidence gate` cooldown 60 |
| `overcut_stay_out` | 2 | `pit_plan == 'overcut'` |
| `tyre_life` | 3 | every `mode.rival_info_every_n_laps` laps: "these tyres have about {laps_of_pace:.0f} laps of pace" |
| `tyre_overheat` / `tyre_graining` | 2 | `overheat` / `graining` with cooldown 90 |
| `tyre_blister` | 2 | `blister_max_pct >= th.blister_warn_pct` |
| `fuel_short` | 1 | `fuel_margin_laps < -mode.lift_coast_trigger_laps` |
| `fuel_marginal` | 2 | `fuel_margin_laps < mode.fuel_margin_laps` |
| `fuel_spare` | 3 | `fuel_margin_laps > th.fuel_spare_laps` once per N laps |
| `energy_over` / `energy_under` | 2/3 | `energy_mode == 'over'`/`'under'` at lap end |
| `overtake_mode` | 2 | `energy_mode == 'attack_ok' and gap_ahead_s <= mode.overtake_call_gap_s and drs_available` |
| `drs_enabled` | 3 | DRS enabled event after SC / lap 2 |
| `penalty` | 1 | `penalty_recent` |
| `serve_penalty` | 2 | `unserved_drive_through + unserved_stop_go > 0 and pit_plan in ('box_now','box_in_n')` |
| `warnings` | 3 | `corner_cut_warnings >= th.warnings_warn` |
| `blue_flag` | 1 | `blue_flag` cooldown 20 |
| `rival_pitted` | 2 | `rival_ahead_pitted or rival_behind_pitted` |
| `weather_crossover` | 2 | `weather_crossover != ''` |
| `sc_deployed` / `vsc_deployed` / `sc_ending` | 1 | phase transitions |
| `lights_out` | 3 | LGOT: "laps_remaining, fuel margin, plan" |
| `pit_exit_traffic_race` | 2 | out_lap and `pit_exit_rival_gap_s < th.pit_exit_traffic_s` |

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

Watchdog: `pitwall start` runs the engine in a supervised thread-free loop; a stalled tick
(`> th.watchdog_stall_s` wall seconds without a tick while packets arrive) logs
`{"outcome":"watchdog_stall"}` and resets the dispatcher queue. Graceful exit sets `ended_at`.

## Learning loop: `pitwall tune`

`pitwall tune [--apply] [--db PATH]` reads `calls` + `call_grades` + `pit_events` + `laps` +
`ab_results` and prints / writes:

1. **Prediction grading**: for every fired `box_*`/`tyre_life` call with `inputs.predicted_lap_ms`
   and `inputs.laps_of_pace`, compare with the actual next lap (from `laps`) and the actual laps
   run before pitting; report mean signed error per track/compound and fold a correction into
   `model_params 'deg_ms_per_lap'` (`weight` = number of graded calls).
2. **Grade feedback**: per rule, `noise_ratio = noise / graded`. Rules with `noise_ratio >=
   th.tune_noise_ratio` get `cooldown_s *= th.tune_cooldown_factor` (and `min_gap` raised) in the
   learned overlay `~/.pitwall/learned.yaml` (a profile-layer file loaded after `profile.yaml`);
   rules graded mostly `good` get nothing changed. `--apply` writes the overlay; without it, prints.
3. **Diff results**: `pitwall diff --record` inserts `ab_results`; `tune` reports rules whose B
   variant fired strictly less often with no `bad` grades as "candidates to promote".

Every write is to SQLite or the learned YAML overlay — never to in-memory state.

## Fixtures and tests

Real recordings stay out of git (ADR 0005); `tests/race_synth.py` builds a deterministic
synthetic race stream (`race_stream(laps=N, ...)`) with Session, Lap Data (player + 3 rivals),
Car Status, Car Damage, Car Telemetry, Session History and Event packets on a 5 km track at 5 Hz
with configurable deg slope, fuel, SC window, rival gaps, penalty and weather forecast.
`scripts/make_m3_fixture.py` writes it to `~/m3-race-25.f1bin` / `~/m3-race-100.f1bin` for
`pitwall replay` / review. Every rule in `race.yaml` has a replay test asserting it fires (and
does not fire on the control stream), and `test_race_budget.py` asserts calls per lap never
exceeds `mode.call_budget_per_lap` on the 100% stream.
