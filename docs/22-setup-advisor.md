# Setup advisor

Design and implementation proposal for the "second product" parked as item 9 in
`10-angles-not-yet-considered.md`: setup advice derived from stored tyre, wear and pace
history, delivered in three phase-aware modes. **Status: A1 to A4 implemented (PR #37). A5 not started.** Numbered 22
because 17 is taken by `17-quali-run-plan.md`.

Today the only setup advice is tyre pressure (`state/pressure.py`, core temperature vs a
window, clamped to the setup range), plus the brake-bias / diff hints in the lock-up calls
(`18-race-engine.md`). This document extends that to the whole setup screen without
breaking the project invariants: decisions are deterministic and replayable (ADR 0008),
and thresholds live in YAML. An LLM is optional and never required. With no API key the
advisor runs entirely on the deterministic tables in §4, and every mode, signal, grade and
replay in this document works unchanged (§7, ADR 0009).

## 1. Research summary

### Sources

Codemasters/EA publish no physics documentation for the setup screen, so every mechanism
below is either an official one-liner, community consensus, or our own telemetry. Sources
are labelled by kind; confidence in §1.3 is derived from how many *independent* kinds
agree.

| Id | Source | Kind |
|----|--------|------|
| S1 | EA Help, *F1 25 car setup guide for tracks* — https://help.ea.com/en/help/f1/f1-25/car-setup-guide-for-tracks/ | official, broad |
| S2 | Traxion, *F1 25 setup guide* — https://traxion.gg/f1-25-setup-guide/ | community guide |
| S3 | SimRacingSetup, *F1 25 tyre temperature & pressures explained* — https://simracingsetup.com/ea-sports-f1/f1-25-tyre-guide/ | community guide (esports setup vendor) |
| S4 | SimRacingSetup, *F1 25 brake bias and differential explained* — https://simracingsetup.com/ea-sports-f1/f1-25-brake-bias-and-differential/ | community guide |
| S5 | SimRacingSetup, *F1 26 setups* hub, *F1 26 setup differences*, and the F1 26 Australia pro setups — https://simracingsetup.com/setups/f1-26/ · https://simracingsetup.com/ea-sports-f1/f1-26-setups-differences/ · https://simracingsetup.com/setups/f1-26-setups-pro/australia/ | F1 26-specific community, incl. published esports setup values |
| S6 | SimRacingConfigs, *F1 26 Season Pack: what's new* — https://simracingconfigs.com/f1-26-season-pack-whats-new-how-to-get-faster/ | F1 26-specific community |
| S7 | SimRacingConfigs, *F1 24 tyre temperatures & pressures guide* — https://simracingconfigs.com/f1-24-tyre-temperatures-pressures-guide/ | community, with a measured min-vs-max pressure test |
| S8 | ADT eSports Academy, *How to warm up tyres in F1 24* — https://adtesportsacademy.com/how-to-warm-up-tires-in-f1-24/ | telemetry analysis (carcass temps on a formation lap) |
| S9 | f125game.com, *camber and toe* / *front vs rear wing* — https://www.f125game.com/car-setup-fundamentals/f125-camber-and-toe-settings/ · https://www.f125game.com/car-setup-fundamentals/f125-front-wing-vs-rear-wing-explained/ | unattributed guide site, **low trust** (contains claims contradicted by UDP, see §1.3) |
| S10 | SimRacingSetup, *F1 24 how to create car setups* — https://simracingsetup.com/f1-24/f1-24-how-to-create-car-setups/ | community method guide |
| S11 | GameRant, *F1 23 how to change car setup* — https://gamerant.com/f1-23-how-to-change-car-setup/ | games press |
| S12 | SimRacingSetup, *Why are there qualifying & race setups with parc fermé enabled?* — https://simracingsetup.com/support/why-are-there-qualifing-race-setups-with-parc-ferme-enabled/ ; *F1 25 race strategy guide* — https://simracingsetup.com/ea-sports-f1/f1-25-race-strategy-guide/ | community guide |
| S13 | EA, *F1 25 / 26 Season Update wheel MFD shortcuts (PC)* — https://www.ea.com/games/f1/f1-25/26-season-update-controls-hub/mfd-shortcuts-wheel-pc | official (bindings only) |
| S14 | FIA, *Parc life: how parc fermé regulations work* — https://www.fia.com/news/fia-insights-parc-life-how-fias-parc-ferme-regulations-make-sure-car-qualifies-one-races | real-world regulation (what the game imitates) |
| S15 | EA Forums, *Brake and differential* (F1 25) — https://forums.ea.com/discussions/f1-25-general-discussion-en/brake-and-differential/12278579 | player bug report |
| S16 | F1 26 complete car setup guide, community video (transcript supplied by user) | F1 26-specific community video |
| R | This repo: `protocol/layouts.py`, `reference/f1-26-udp-notes.md`, the reviewed race recording (`18-race-engine.md`) | own telemetry, highest weight for *what is observable* |

F1 26 is the *F1 2026 Season Pack* for F1 25 with reworked handling (S5, S6): the setup
screen and the Car Setups packet are unchanged in shape (the per-car struct is still 50
bytes: 1233 = 29 header + 24 × 50 + 4, R), but F1 25 baselines and some F1 25 cause/effect
claims may not transfer. Anything sourced only from F1 23–25 is capped at **medium**.

### 1.1 Confidence scale

- **High** — official source or our own telemetry, plus at least one independent guide;
  no disagreement found.
- **Medium** — two or more independent community guides agree; not verified in our data;
  or high-confidence for an earlier game version only.
- **Low** — a single source, sources disagree, or the mechanism is plausible real-car
  physics with no game-specific evidence.

The advisor only *acts* (emits a delta) on high/medium rows. Low rows can appear in the
debrief as "try this" experiments, flagged as such, and graduate via the learning loop (§6).

### 1.2 Parameter effects

`↑` = raise the value. "Surface/core" refers to the two temperatures the UDP exposes per
tyre (`tyres_surface_temperature`, `tyres_inner_temperature`); the MFD shows the same two
plus brake temperature (S3).

| Area | Parameter (↑) | Balance | Surface vs core temp | Wear | Conf. | Basis / disagreements |
|------|---------------|---------|----------------------|------|-------|-----------------------|
| Aero | Front wing ↑ | More front grip, less understeer, more rotation; too much → rear light, exit oversteer | Less front sliding → fewer front surface spikes when understeer-limited; more front load → higher front core on high-speed tracks | Front wear ↓ if wear is understeer scrub; ↑ if load-driven | Balance **high**; temp/wear **low** | S1, S2, S9, S10 agree on balance. No source measures temp/wear; the sign depends on *why* the fronts wear (§4, rule `front_wear_limited`). S16 (F1 26): the front wing costs less drag than the rear wing for the same downforce |
| Aero | Rear wing ↑ | More rear stability and traction; more understeer by balance; drag, top speed ↓ | Fewer rear surface spikes on exits | Rear wear ↓ via less wheelspin | Balance **high**; wear **medium** | S1, S2, S9, S10. F1 26: active aero/straight-line mode makes higher wings cheaper (S5, S6, medium); S5's published F1 26 setups run a large front/rear gap (40–50 / 7–10) "because 2026 cars understeer". S16 runs the front about 15 above the rear, or about 10 for a more stable car. S16 sets the total downforce level by how much straight-line mode the track allows |
| Transmission | On-throttle diff ↑ (more locked) | **Disputed.** S4, S2: more drive, but easier to spin up the inside rear and oversteer. Repo convention (`f1-26-udp-notes.md`) and S16: more stable exits, more exit understeer | More rear surface flash heat if it causes wheelspin | Rear wear ↑ with wheelspin | **Low** (sign disputed) | S5's F1 26 setups run 100 %; S5's own pre-release article predicted *lower* on-throttle for 2026. Our race: rear spin samples 3–13 %/lap at 60 %, 4–30 %/lap at 50 % — inconclusive (R). S16 runs 100 and sides with the repo convention. A locked diff spins both rears at the same rate even when inside and outside grip differ, so wheelspin is predictable. An open diff is snappy and can pitch the car into a snap. S4 and S2 still disagree and R is inconclusive, so confidence stays low. Must be settled by the learning loop per track |
| Transmission | Off-throttle diff ↑ | More stable entry, less lift-off oversteer, more entry understeer | — | Rear entry lock-ups ↓ | **Medium** | S2, S4, S6 and repo convention agree (lower = more rotation). Already used in `rules/shared.yaml` rear-lock-up line. S16 gives the mechanism: the off-throttle diff acts whenever the driver steers off throttle, because the inside wheel travels a shorter path. It acts through the braking and coasting phases of a corner. Straight-line coasting is unaffected. S16's 40–45 baseline is higher than the other sources suggest (§4.1) |
| Transmission | Engine braking ↑ | More rear braking on lift → entry rotation / rear instability | — | — | **Low** | In packet (R) and in the v1 sketch; no current guide covers it for F1 26 |
| Geometry | Camber more negative (front / rear) | More loaded cornering grip; less braking/traction contact | Inner-shoulder heat — **not observable** (UDP has one surface value per tyre, no inner/middle/outer) | Guides split: S9 says more heat and wear; S5's F1 26 esports setups run the most negative values (−3.5 / −2.0) for race and quali alike, implying little race penalty | **Low** | S2, S9. S9 also claims an inner/middle/outer HUD, contradicted by S3 and the UDP — one reason S9 is low trust. S16 says camber does nothing in F1 25 and F1 26 and runs it at the slider minimum, which matches S5's values |
| Geometry | Toe (front out / rear in) ↑ | Front toe-out: sharper turn-in; rear toe-in: stability | More constant slip → surface and core ↑ | Wear ↑, drag ↑ | **Medium** for "more toe = more heat/wear/drag"; balance **low** | S2, S9; S5's F1 26 setups sit at minimum toe (0 / 0.1). S16 says toe does nothing in F1 25 and F1 26 and runs the minimum. That disagrees with the heat, wear and drag claim from S2 and S9 |
| Suspension | Front springs ↑ (stiffer) | Sharper response; too stiff → front slides over bumps/kerbs | — | — | **Medium** | S1 (stiff = high-speed stability, soft = kerbs), S2, S5 ("very stiff" in F1 26), S16 (stiffer springs control the aero platform better) |
| Suspension | Rear springs ↑ | Less traction, more oversteer on exit | Rear surface spikes on exits | Rear wear ↑ via wheelspin | **Medium** | S2, S10 (soft rear = traction). S16: a stiffer rear also controls the aero platform better, and F1 26 costs less traction for it than F1 25 |
| Suspension | Front ARB ↑ | More turn-in; slow-corner understeer when overloaded | — | — | **Low** (disputed) | S2 says stiff front ARB → slow-corner understeer; community "inverted ARB" trend and S5 ("soft ARBs ideal in F1 26") differ |
| Suspension | Rear ARB ↑ | More rotation / oversteer; less traction | — | Rear wear ↑ | **Medium** | S2, S10, S16. S16 treats the rear ARB as the main mechanical source of rotation and runs it as high as the driver can control, roughly 13 or less |
| Suspension | Ride height ↑ (either) | Less bottoming, less downforce; rake (rear ↑) adds rotation and drag | — | — | **Medium** | S2, S5 ("more rake may aid rotation" for F1 26). No bottoming signal in UDP; ride height advice is blind beyond "if you hit kerbs/bottom". S16 runs the rear as low as possible (about 40) and raises it only for tracks with heavy kerb use. S16 says F1 26 makes the car more sensitive to pitch under braking. As the rear lifts, the diffuser loses efficiency, so a higher rear makes low-speed entry oversteer worse |
| Brakes | Brake bias ↑ (forward) | Stable under braking, more entry understeer; front lock-ups ↑ | Front brake temps ↑; whether brake heat reaches the tyre is **disputed** (S3 yes for F1 25; S8 measured "almost zero" in F1 24) | Front flat-spot risk ↑ | Balance/lock-up **high** | S2, S4, S10, ADR 0007 (R: five front lock-ups in the Brazil file). S16: move bias forward for tracks with big braking zones and rearward for tracks with small ones. If the rears lock, move it forward. If the fronts lock, move it rearward |
| Brakes | Brake pressure ↑ | Shorter stops, more lock-ups | — | Lock-up wear | **Medium** | S2, S10 (most setups at 100 %, S5). S16 runs 100 and suggests a lower value, such as 97, while the driver adapts to driving without ABS |
| Tyres | Pressure ↑ | Smaller contact patch: more response, less mechanical grip/traction, higher top speed | **Runs cooler** in F1 24/25 (inverse relation); F1 23 was the opposite | Wear ↓ slightly when it keeps the tyre in window | Direction **high** for F1 24/25, **medium** for F1 26; magnitude small | S3, S7, S2 agree on direction. S10 notes the sign flipped vs F1 23 — and S10 contradicts itself (one list says raise when cold, the text says lower). S7 measured only 5–6 °C difference min→max over 5 laps. Repo already handles the sign as `pressure_hot_sign`. S16 agrees for F1 26: higher pressure gives a smaller contact patch and a stiffer sidewall, so the tyre runs cooler. S16 also uses rear pressure to add rotation, because a higher rear pressure shrinks the rear contact patch |
| Tyres | — | — | Surface spikes = sliding/wheelspin; core moves slowly and is the target (S3) | Cold *or* hot → sliding → wear ↑ (S3 grip/wear table) | **Medium** | S3; S8's carcass telemetry. Basis for §3 |
| Fuel / ballast | Fuel load, ballast | Weight; ballast position not in packet | — | Heavier = more wear | **Medium** | R (in packet). Not a recommendation target |

Two cross-cutting findings shape the design:

1. **Balance levers are well agreed; thermal/wear effects are not.** Wing and brake bias
   directions are high-confidence. Almost every temperature or wear claim beyond tyre
   pressure is low or medium. The tables therefore attach wear/thermal symptoms to the
   balance mechanism that causes them (scrub vs load vs wheelspin), and let the learning
   loop measure the per-click effect per track.
2. **Camber and ride height are largely blind.** Without inner/middle/outer temperatures
   or a bottoming signal, the advisor cannot tune them from evidence. They stay in the
   experiment tier.

### 1.3 Parc fermé

Real F1 (S14): the car enters parc fermé when it first leaves the pit lane in qualifying
and stays there until the race start; front-wing flap angle is among the permitted changes.
The game's version, per S11 and S12 (consistent across F1 23–25): once qualifying starts,
**only front wing, on-throttle diff, brake bias and tyre pressures** stay adjustable in the
garage. Leagues often turn it off (S12); the Session packet carries `parc_ferme_rules`
(R, parsed in `layouts.py` but not yet stored in state), so the advisor reads it rather
than guessing.

On track (S4, S13, R): brake bias and on-throttle diff via MFD/bindings at any time
(the reviewed race shows bias 57 → 56 and on-throttle 60 → 50, rebroadcast in Car Setups
a few seconds later; off-throttle untouched). S4 states only on-throttle is adjustable in
a race. S16 states that off-throttle cannot change while the car is on track. Front wing can be requested for the next pit stop (S9, low trust, corroborated by
the Car Setups field `next_front_wing_value`, R). An EA forum thread (S15) reports that pressing
bias/diff bindings before opening the MFD resets the value to its minimum — an argument
for always quoting the current value in calls.

**Confidence:** garage list medium (community, earlier versions, consistent); MFD bias and
on-throttle high (R); front wing at the stop medium. The first practice → quali → race
weekend recorded with the advisor should be used to confirm the matrix: any locked field
that changes while `parc_ferme_rules = 1` after the first qualifying out-lap is a doc bug.

Session-type → allowed changes (`m_sessionType` mapping from `03-strategy.md`; sprint
shootout 10–14 follows the qualifying column, as in the rules; 18 time trial is out of
scope):

| Parameter | 1–4 practice, garage | 1–4 practice, on track | 5–9 quali, garage (PF on) | 5–9 quali, on track | 15–17 race, pre-race garage (PF on) | 15–17 race, on track (MFD) | 15–17 race, at pit stop |
|-----------|---|---|---|---|---|---|---|
| Front wing | ✔ | — | ✔ | — | ✔ | request for stop | ✔ (requested value) |
| Rear wing | ✔ | — | locked | — | locked | — | — |
| On-throttle diff | ✔ | ✔ MFD | ✔ | ✔ MFD | ✔ | ✔ | — |
| Off-throttle diff | ✔ | ? | ✔ (R) | ? | locked | ✘ (S4, S16) | — |
| Engine braking | ✔ | — | locked | — | locked | — | — |
| Camber, toe | ✔ | — | locked | — | locked | — | — |
| Springs, ARBs, ride height | ✔ | — | locked | — | locked | — | — |
| Brake pressure | ✔ | — | locked | — | locked | — | — |
| Brake bias | ✔ | ✔ MFD | ✔ | ✔ MFD | ✔ | ✔ | — |
| Tyre pressures | ✔ | — | ✔ | — | ✔ | — | unconfirmed |
| Fuel load | ✔ | — | ✔ (quali fuel) | — | ✔ | — | — |

A Q3 recording with `parc_ferme_rules = 1` shows off-throttle 30 → 25 in the garage after
the Q1 out-laps, so the quali garage cell is ✔ (R). The Car Setups packet reported the
change. The recording can't show whether the car ran with it. `setup_rules.yaml` holds this
matrix, and its `quali_locked` list drives the pre-qualifying checklist.

With `parc_ferme_rules = 0` every "locked" cell becomes ✔. "?" cells are never advised.
Before the first qualifying out-lap the real-world rule leaves the car free; whether the
game locks at session start or at first pit exit is unconfirmed, so the advisor treats
the whole of 5–9 as locked when parc fermé is on (conservative).

### 1.4 What telemetry can see

Car Setups (packet 5, 2 Hz, read-only; `CAR_SETUP_CAR` in `protocol/layouts.py`):

| Field | Type | In `SessionState` today |
|-------|------|------------------------|
| `front_wing`, `rear_wing` | u8 | `setup_front_wing`, `setup_rear_wing` |
| `on_throttle`, `off_throttle` (diff %) | u8 | `setup_on_throttle_diff`, `setup_off_throttle_diff` |
| `front_camber`, `rear_camber` | f32 | full struct in `setup` only |
| `front_toe`, `rear_toe` | f32 | `setup` only |
| `front_suspension`, `rear_suspension` | u8 | `setup` only |
| `front_anti_roll_bar`, `rear_anti_roll_bar` | u8 | `setup` only |
| `front_suspension_height`, `rear_suspension_height` | u8 | `setup` only |
| `brake_pressure` | u8 | `setup` only |
| `brake_bias` | u8 | `setup_brake_bias` (live bias also in Car Status `front_brake_bias`) |
| `engine_braking` | u8 | `setup` only |
| `rear_left/rear_right/front_left/front_right_tyre_pressure` | f32 | `setup_tyre_pressure` (Corners) |
| `ballast` | u8 | `setup` only |
| `fuel_load` | f32 | `setup_fuel_load` |
| `next_front_wing_value` (packet-level, after the 24 cars) | f32 | **not stored** |

So the advisor sees **every** setup-screen value for the player — it never recommends
blind about the *current* setting. What it cannot see: the setup-screen ranges and step
sizes (learn them as observed min/max/step per field, or configure), the in-game
"understeer/oversteer" labels, inner/middle/outer tyre temps, ride height/bottoming,
per-corner loads. Rivals' setups are expected to be blanked under restricted
multiplayer telemetry (per the EA UDP spec, not yet seen in our data); the advisor only
uses the player's own car, so this does not matter.

Related Session fields: `parc_ferme_rules`, `tyre_temperature` (surface-only vs surface &
carcass simulation — core-temp rules are disabled when the carcass isn't simulated;
value semantics to be confirmed against a recording), `track_temperature`,
`weekend_structure`.

## 2. What "balanced" means

The target is **race-stint pace**: the setup that finishes a stint fastest with the tyres
it has, not the one with the best single lap. Every signal below already exists in
`SessionState` or can be derived from existing packets at existing rates; the "new"
column marks what the advisor adds.

| Signal | Definition | Source | New? |
|--------|------------|--------|------|
| `z` (per corner) | window-relative core temp: `(T_inner − centre) / half_width`, window per compound (`pressure_window_*` today; per-compound windows from S3 as priors) | `tyre_inner_ema_slow` | derivation |
| `axle_thermal` | `mean(z_FL, z_FR) − mean(z_RL, z_RR)`; > 0 = fronts hotter relative to their window | above | yes |
| `side_thermal` | `mean(z_left) − mean(z_right)` minus the **track baseline** (clockwise/anticlockwise tracks load one side by design) | above + `model_params` | yes |
| `flash_<axle>` | per-lap p90 of `surface − inner` on that axle; surface spikes are sliding or wheelspin (S3) | `tyre_surface_ema_fast`, `tyre_inner_ema_fast` | yes |
| `wear_rate` (per corner) | %/lap over green laps of the stint | `tyres_wear`, `laps.wear_pct` | stored per stint |
| `wear_axle_ratio` | front mean rate / rear mean rate | above | yes (live version is `wear_hot_*`) |
| `wear_limit_rate` | max corner rate: the tyre that ends the stint | above | yes |
| `wear_spread` | coefficient of variation of the four rates, minus track baseline | above | yes |
| `deg_slope` | fuel-corrected ms/lap vs tyre age (`stints.deg_ms_per_lap`) | `model/deg.py` | exists |
| `repeatability` | stdev of fuel- and age-corrected green lap times in the stint | `laps` | yes |
| `lockups_<axle>` | per 10 green laps | `LockupDetector` | exists (count) |
| `snaps` | spins + saves per 10 laps (`SpinDetector`, save sideslip) | `driving.py` | exists (count) |
| `traction_exits` | per 10 laps: rear slip ratio > `trac_slip` with throttle > `trac_throttle`, 60–200 km/h, sustained `trac_min_s` (the measurement in `f1-26-udp-notes.md` promoted to a detector) | Motion Ex `wheel_slip_ratio` | yes — the missing detector `18-race-engine.md` notes |
| `slip_balance` | in steady cornering (\|lat g\| > 1.5, throttle 20–80 %, no brake): `mean(\|α_front\|) − mean(\|α_rear\|)`; > 0 understeer, < 0 oversteer. Sign convention to verify on a recording | Motion Ex `wheel_slip_angle` | yes |
| `entry_loss` | per corner, min speed minus the driver's own median min speed at that corner on the same compound this weekend, fuel/age-corrected; a setup-wide shift across many corners (not one) is an understeer proxy | Car Telemetry speed + lap distance | yes (needs corner segmentation from lap distance) |
| `driver_balance` | menu opinion "understeer"/"oversteer" (`12-driver-input.md`) | driver input | exists; **supporting evidence only**, never sole trigger |

**Balanced** = within band on every row, with bands in YAML (starting values in §4):
`|axle_thermal| ≤ 0.3`, `|side_thermal| ≤ 0.3`, `wear_axle_ratio ∈ [0.94, 1.06]`,
`|slip_balance| ≤ band`, event rates at or below the driver's own 50th percentile for the
track, and `repeatability` not worse than the previous setup.

**Stint objective** used to rank candidate changes and to grade them (§6), lower is
better, weights in YAML:

```
J = w_deg · deg_slope_norm            # 0.30  stint-long degradation
  + w_lim · wear_limit_rate_norm      # 0.25  the corner that ends the stint
  + w_sym · wear_spread               # 0.15  4-corner symmetry
  + w_th  · (|axle_thermal| + |side_thermal|)   # 0.10
  + w_ev  · (snaps + traction_exits + lockups)_norm  # 0.10  repeatability / risk
  + w_rep · repeatability_norm        # 0.05
  − w_pace· pace_gain_norm            # 0.05  single-lap grip: deliberately small
```

Normalisation is against the same track+compound history, so `J` compares setups, not
tracks. Pace enters only at matched fuel and tyre age (§6.3).

## 3. Phase-aware modes

| Mode | When | Output | Change budget | Params in scope |
|------|------|--------|---------------|-----------------|
| **Debrief** | after a practice session (1–4), before the next; also after quali when parc fermé is off | ranked setup deltas in the debrief (§07 action rules of `16-debrief-design.md`) and `pitwall setup` CLI | one *primary* change per next run, up to two alternatives listed; linked pairs (e.g. front+rear springs) count as one | all with conf ≥ medium; low-confidence ones as "experiment" |
| **Garage** | `driver_status == IN_GARAGE` in 1–9 (and 10–14) | one row in the pit board `SETUP` block + optional single radio line on entering the garage | exactly one parameter | practice: all ≥ medium; quali: parc fermé matrix only (front wing, on-throttle, bias, pressures) |
| **Race** | on track, 15–17 | radio + dashboard calls through the existing dispatcher, P3 | one per call, rate-limited by rule cooldown and per-lap budget | brake bias, on-throttle diff; front wing only as a pit-stop request when a stop is already planned |

**Debrief.** Built from SQLite only, after the session, so it can use whole stints:
long runs (≥ `setup_min_run_laps` green laps) are grouped by setup state (§5), `J` and each
signal are computed per run, and the current setup's symptoms are matched against the rule
tables. Where history holds a previous run on a *different* setup state at the same
track+compound, the debrief shows the measured before/after, e.g.:

> Fronts wearing 6 % faster than rears over the two long runs (2.34 vs 2.21 %/lap, 14 laps
> on C3). Front cores above window (z +0.4), slip balance neutral. Try **front pressures
> +0.4 psi** (27.0 → 27.4); alternative **front wing −1** (32 → 31). Last time at this
> track, +0.4 psi front cut front wear 4 %.

(The user-supplied example "−1 front wing / −0.5 psi front" is correct only when the
fronts are *under* window and the car is not understeering; with hot fronts the research
says the pressure goes *up* — S3, S7. The rules make the direction conditional, §4.)

One primary change per run follows the one-change-at-a-time method in S10 and is what
makes the learning loop able to attribute the result.

**Garage.** Same tables, evaluated on the last run of this session only, filtered by the
matrix and by `parc_ferme_rules`. The card shows current value → proposed value, the
triggering evidence, the expected trade-off, and "locked by parc fermé" for anything the
rules would otherwise have suggested (so the driver knows why it's missing). Pressure
advice from `state/pressure.py` becomes one rule family here rather than a separate path.

**Where it renders: the garage pit board, not its own screen.** The dashboard does not have a
separate car-setup page; it was dropped while the dashboard was slimmed down (M4), because
nothing yet produces full-setup advice to put on it. The pit board already
shows the current setup in game-menu order in its `SETUP` block (`web/app.js`
`SETUP_GROUPS`/`renderSetup`, `docs/15-dashboard-design.md` §10). Its rows already carry
`todo`/`done`/`no change` states for the pressure target. Garage-mode cards reuse that
block: the recommended parameter's row shows `current → proposed` plus a one-line reason, and
parc-fermé-locked rows get a `locked` tag. The full evidence payload goes in the debrief,
not on the board. The layout constraint is to fit into the existing pit-board grid (release
light, per-corner pressure, next run, setup) without adding a page or pushing those zones
off a 1080p/tablet screen. The ADR 0009 second opinion, when enabled, is a single labelled
row in the same block.

**Race.** Extends the v1 sketches and the existing lock-up lines; nothing garage-only is
ever spoken. Calls quote the live value (Car Status bias, Car Setups diff) and are
confirmed by the rebroadcast Car Setups value, so a follow-up can say "bias is on 56 now"
or stay silent. Front wing is only suggested as a request attached to an already planned
stop (`pit_plan`), confirmed via `next_front_wing_value`.

## 4. Recommendation model

Rule tables, one YAML file (`config/defaults/setup_rules.yaml`, proposed), loaded through the
layered config like `thresholds.yaml`. Evaluation is pure: `(signals, setup, mode,
session_type, parc_ferme, learned) → [Recommendation]`, no clock, no randomness, so a replay
of the same recording + DB yields the same recommendations and a fixture can pin them.

Schema:

```yaml
# setup_rules.yaml (proposed)
version: 1
defaults:
  min_run_laps: 6          # green laps per run before any symptom is judged
  min_runs: 1              # runs showing the symptom (debrief); garage uses the last run
  max_changes_per_run: 1
  confidence_floor: medium # below this → "experiment" tier, never race/garage mode

params:                     # step = one click; ranges learned or configured
  front_wing:        {step: 1,   modes: [debrief, garage, race_stop]}
  rear_wing:         {step: 1,   modes: [debrief, garage]}
  on_throttle:       {step: 5,   race_step: 10, modes: [debrief, garage, race]}
  off_throttle:      {step: 5,   modes: [debrief, garage]}
  brake_bias:        {step: 1,   modes: [debrief, garage, race]}
  front_pressure:    {step: 0.2, modes: [debrief, garage]}   # both fronts
  rear_pressure:     {step: 0.2, modes: [debrief, garage]}
  rear_suspension:   {step: 2,   modes: [debrief, garage]}
  rear_anti_roll_bar:{step: 1,   modes: [debrief, garage]}
  front_toe:         {step: 0.05, modes: [debrief]}
  rear_toe:          {step: 0.05, modes: [debrief]}

symptoms:
  front_wear_limited:
    when: "wear_axle_ratio >= 1.06 and wear_limit_axle == 'front'"
    evidence: [wear_rate, wear_axle_ratio, z, slip_balance, flash_front, run_laps, compound]
    candidates:              # evaluated in order; first whose `if` holds is primary
      - {param: front_pressure, dir: +1, mag: by_z, if: "z_front > 0.3",
         conf: high,   expect: "fronts cooler, front wear ↓", tradeoff: "less front grip in slow corners"}
      - {param: front_pressure, dir: -1, mag: by_z, if: "z_front < -0.3",
         conf: medium, expect: "fronts into window, less sliding", tradeoff: "front temp rises over the stint"}
      - {param: front_wing,     dir: +1, mag: 1,    if: "slip_balance > us_band",
         conf: medium, expect: "less understeer scrub", tradeoff: "rear stability ↓"}
      - {param: front_wing,     dir: -1, mag: 1,    if: "slip_balance <= us_band",
         conf: low,    expect: "less front load", tradeoff: "more understeer"}
    contra: ["snaps_per10 > snaps_p75"]      # don't add rotation to a nervous car

  rear_wear_limited:
    when: "wear_axle_ratio <= 0.94 and wear_limit_axle == 'rear'"
    evidence: [wear_rate, wear_axle_ratio, z, traction_exits, flash_rear]
    candidates:
      - {param: rear_pressure, dir: +1, mag: by_z, if: "z_rear > 0.3", conf: high}
      - {param: on_throttle,   dir: -1, mag: 1,    if: "traction_exits_per10 > trac_p75", conf: low}
      - {param: rear_suspension, dir: -1, mag: 1,  if: "traction_exits_per10 > trac_p75", conf: medium}
      - {param: rear_wing,     dir: +1, mag: 1,    if: "flash_rear > flash_band", conf: medium}
    contra: ["slip_balance > us_band"]       # already understeering

  traction_limited:
    when: "traction_exits_per10 >= trac_p75 and flash_rear > flash_band"
    evidence: [traction_exits, flash_rear, wear_rate, on_throttle]
    candidates:
      - {param: on_throttle, dir: -1, mag: 1, conf: low, modes: [debrief, garage, race]}
      - {param: rear_anti_roll_bar, dir: -1, mag: 1, conf: medium}

  entry_instability:
    when: "lockups_rear_per10 >= 1 or (snaps_per10 >= snaps_p75 and snap_phase == 'entry')"
    evidence: [lockups_rear, snaps, brake_bias, off_throttle]
    candidates:
      - {param: brake_bias,   dir: +1, mag: 1, conf: high, modes: [debrief, garage, race]}
      - {param: off_throttle, dir: +1, mag: 1, conf: medium}

  front_lockups:
    when: "lockups_front_per10 >= 2 and lockup_spot_laps >= 3"
    evidence: [lockups_front, lockup_spot, brake_bias]
    candidates:
      - {param: brake_bias, dir: -1, mag: 1, conf: high, modes: [debrief, garage, race]}

  understeer_balance:
    when: "slip_balance > us_band and entry_loss_corners >= 3"
    evidence: [slip_balance, entry_loss, driver_balance, z]
    candidates:
      - {param: front_wing, dir: +1, mag: 1, conf: high}
      - {param: rear_anti_roll_bar, dir: +1, mag: 1, conf: medium}
    contra: ["traction_exits_per10 > trac_p75"]

  oversteer_balance:
    when: "slip_balance < -os_band and snaps_per10 >= snaps_p50"
    evidence: [slip_balance, snaps, traction_exits, driver_balance]
    candidates:
      - {param: front_wing, dir: -1, mag: 1, conf: high}
      - {param: rear_wing,  dir: +1, mag: 1, conf: high}

  toe_heat:                  # experiment tier only
    when: "axle_thermal_abs > 0.5 and all_z > 0.3"
    candidates:
      - {param: front_toe, dir: -1, mag: 1, conf: medium, if: "front_toe > toe_min"}
      - {param: rear_toe,  dir: -1, mag: 1, conf: medium, if: "rear_toe > toe_min"}

magnitudes:
  by_z: [{z: 0.3, steps: 1}, {z: 0.6, steps: 2}, {z: 1.0, steps: 4}]  # mirrors pressure_step_small/medium/large

bands:                       # thresholds in thresholds.yaml, names only here
  us_band: setup_slip_us_deg
  os_band: setup_slip_os_deg
  flash_band: setup_flash_c
```

Proposed additions to `thresholds.yaml` (starting values, all to be tuned from recordings):

```yaml
setup_min_run_laps: 6
setup_wear_axle_ratio: 1.06
setup_z_band: 0.3
setup_slip_us_deg: 0.5
setup_slip_os_deg: 0.5
setup_flash_c: 12
trac_slip: 0.08            # values from the f1-26-udp-notes.md measurement
trac_throttle: 0.7
trac_min_s: 0.15
setup_rec_cooldown_laps: 5 # race mode
setup_grade_min_laps: 5
setup_grade_tol: 0.03      # 3 % relative change counts as "moved"
```

Semantics:

- **Mode filter first, then parc fermé, then confidence.** A candidate whose param is
  locked is dropped *but kept in the evidence* as "would have suggested X (locked)".
- **Contraindications** suppress the whole symptom, not only one candidate: if the car is
  both front-wear-limited and snapping, the advisor says so and makes no change.
- **Magnitude** is in clicks; `by_z` mirrors the existing pressure step sizes; clamped to
  the observed setup range, and "at the limit" is reported like `pressure.py` does.
- **Learned ordering.** When `model_params` holds a measured per-click effect for
  `(track, compound, rule, param)` with enough weight, candidates are re-ranked by it and a
  candidate measured `wrong` repeatedly is demoted (§6.4). Without history the YAML order
  applies.
- **Silence over guessing.** No symptom → no advice. Conflicting symptoms on the same param
  (one says +1, one −1) → no advice for that param, both listed.

Every recommendation is a record, the same shape as a call's `inputs`:

```json
{
  "rec_id": "s1234:rec:7", "rule_id": "setup.front_wear_limited", "mode": "debrief",
  "param": "front_pressure", "from": 27.0, "delta": 0.4, "to": 27.4, "conf": "high",
  "evidence": {"wear_rate": [2.20, 2.22, 2.31, 2.37], "wear_axle_ratio": 1.06,
               "z_front": 0.42, "slip_balance": 0.1, "run_laps": 14, "compound": 18},
  "setup_state": "sha1:9f2c…", "session_type": 2, "parc_ferme": 1,
  "alternatives": [{"param": "front_wing", "delta": -1, "conf": "low"}],
  "suppressed": [{"param": "rear_wing", "reason": "locked"}]
}
```

### 4.1 Starting values (S16)

This section lists the F1 26 baseline from S16. The advisor can use it as the prior for a
track with no history. Learned per-track values replace it once the learning loop has
enough weight (§6.4).

| Parameter | S16 baseline | Notes |
|-----------|--------------|-------|
| Wing gap (front minus rear) | 15 | 10 for a more stable car. S5's published setups run a much larger gap, 30 or more |
| On-throttle diff | 100 | Same as S5's F1 26 setups |
| Off-throttle diff | 40–45 | Higher than other sources, see below |
| Suspension (front / rear) | about 41 / 41 | |
| Front ARB | 4–7 | |
| Rear ARB | 6–10 | Up to about 13 if the driver can control it (§1.2) |
| Front ride height | 21–23 | |
| Rear ride height | 40 | Raise for tracks with heavy kerb use, such as Singapore |
| Brake bias | 54–56 | Forward for big braking zones (§1.2) |
| Brake pressure | 100 | Lower, such as 97, while the driver adapts to no ABS |
| Front tyre pressure | High in the range | |
| Rear tyre pressure | Depends on the track | Lower for traction tracks such as Monza. Higher for tracks limited by tyre temperature, such as Mexico and Singapore |

The off-throttle baseline is a disagreement. S16 starts at 40–45. The other community
notes in §1.2 (S2, S4, S6) say a lower value gives more rotation, and their range sits below
40–45. This document records both. The per-track learning loop decides which one works, as
it does for the on-throttle sign (§6.4).

## 5. Setup state and history (schema proposal)

Additive migration, following item 16's rule (nullable columns, new tables only):

```sql
CREATE TABLE setup_states (
    id INTEGER PRIMARY KEY,
    hash TEXT UNIQUE,        -- over all setup fields except fuel_load and next_front_wing_value
    fields TEXT              -- JSON of the Car Setups struct
);
ALTER TABLE stints ADD COLUMN setup_state_id INT;  -- state in effect for most of the stint
ALTER TABLE sessions ADD COLUMN parc_ferme INT;
CREATE TABLE setup_changes (   -- every observed change, incl. MFD changes mid-run
    id INTEGER PRIMARY KEY, session_uid INT, lap INT, session_time REAL,
    from_state INT, to_state INT
);
CREATE TABLE setup_recs (      -- one row per recommendation, JSON as in §4
    id INTEGER PRIMARY KEY, session_uid INT, rec_id TEXT UNIQUE, rule_id TEXT,
    mode TEXT, param TEXT, from_value REAL, delta REAL, conf TEXT,
    setup_state_id INT, track_id INT, compound INT, lap INT, evidence TEXT
);
```

A "run" for the advisor is a stint segment with one setup state; an in-run MFD change
splits it. Signals per run are computed at debrief time from `laps` plus the 10 Hz
downsample. The debrief today shows only lap-mean inner temperature (`16-debrief-design.md`
§7), so per-run thermal aggregates are new debrief-time work, but there is no new
live-path work beyond the traction detector, `slip_balance` accumulation and storing
`parc_ferme_rules` / `next_front_wing_value`.

## 6. Learning loop

The advisor plugs into the existing chain in `20-learning-loop.md`; nothing here changes
live decisions except through the same gates.

### 6.1 Applied?

For each `setup_recs` row, find the next run on the same track (same weekend first, then
later weekends) and compare its setup state with the rec's. Debrief advice is issued after
the session ends, so its after-run comes from a later session only. A later run in the
same session cannot be a response to it, and the row stays ungraded until the next session
on that track.

| Next run's change | Label |
|-------------------|-------|
| param moved in the recommended direction, no other field changed | → graded (6.2) |
| param moved as recommended and other fields changed too | `n/a` (`confounded`) |
| param unchanged | `ignored` |
| no qualifying run follows (too short, different compound, wet, session ended) | `censored` |

### 6.2 Graded

Compare the triggering symptom metric (from `evidence`) and `J` between the before-run and
the after-run at matched conditions (6.3):

- `good` — symptom metric improved by ≥ `setup_grade_tol` **and** `J` did not get worse
  **and** no contraindicated symptom appeared;
- `wrong` — symptom worsened by ≥ tol, or a contraindicated symptom appeared, or `J` worse
  by ≥ tol;
- `n/a` — inside tolerance (inconclusive).

Rows go into the existing `outcomes` table: `call_id = rec_id`, `rule_id`,
`metric = "setup:<symptom>"`, `predicted` = expected sign × tol, `actual` = measured
relative change, `error`, `label`, `detail`. That puts setup grades in `pitwall digest`
findings and `pitwall tune` with no new plumbing; the digest gains a `setup` block (recs,
applied, graded, open experiments).

### 6.3 Matched comparison

Only green, valid laps; fuel-corrected with the stint fit; compared over the overlapping
tyre-age range (≥ `setup_grade_min_laps` in each run); same compound; track temperature
within ±5 °C; runs with SC or weather change dropped. This is the same discipline as the
hindsight grader's `stop_cost_s`, and it is why practice long runs matter: quali and
race-only weekends rarely yield a matched pair.

### 6.4 Folding into `model_params`

Per graded rec, fold the measured effect **per click** with `Database.fold_param`
(weighted running mean, weight cap):

```
fold_param(track_id, compound, f"setup_gain:{symptom}:{param}:{sign}", value=actual/steps,
           weight=min(laps_before, laps_after) / setup_min_run_laps)
fold_param(track_id, compound, f"setup_base:{signal}", value=<signal on a balanced run>)
```

- `setup_gain:*` re-ranks candidates and replaces the YAML `conf` once its weight passes
  `setup_learned_min_weight` (a low-confidence row that keeps measuring `good` graduates
  per track — this is how the disputed on-throttle diff gets settled);
- `setup_base:*` holds the track's normal asymmetry (side_thermal, wear_spread), which is
  subtracted before a symptom is judged;
- `pitwall tune` treats `setup.*` rules like any other rule: grades → `cooldown_mult`
  (repeated `wrong` → the rule fires less and, in the debrief, its candidate drops down the
  list); caps and minimum-grade counts unchanged.

"Per player" is implicit: the SQLite file is the local player's own history (the Car
Setups struct used is always the player's car).

YAML table changes (new symptoms, new thresholds, new directions) are reviewed like any rule
change. Each rule needs a replay fixture, and `pitwall diff --corpus` must not lose
must-fire calls. The lessons ledger and automatic promotion are not built
(`20-learning-loop.md`). Until they are, promotion is a reviewed config change.

## 7. LLM decision (ADR-style)

**Status:** accepted as [ADR 0009](adr/0009-llm-chooser-with-rule-veto.md), which replaces the
decision part of ADR 0008. This section applies it to setup advice.

**Context.** ADR 0008 kept every live decision deterministic, YAML-configured and
replayable. Setup advice is where a free-form model is most dangerous. Most thermal and wear
effects in §1.2 are low or medium confidence even among human experts, and the sources
contradict each other (on-throttle diff sign, ARBs, pressure direction across game
versions). A model trained on that corpus will state the contested claims fluently. A
hallucinated "−3 rear wing" is worse than silence: it wastes a practice run, and after
qualifying it can't be undone. It is also where judgment matters: picking *which one*
change to try from several plausible symptoms is what the fixed candidate order in §4 does
least well.

**Options.**

| Role | Verdict | Why |
|------|---------|-----|
| Free-form decision (the model proposes any param or delta) | **Rejected** | Could invent illegal or out-of-range changes; can't be graded against the tables |
| Chooser among §4 candidates, rules veto | **Accepted under ADR 0009** | The model picks the primary change from the candidates that survived the mode, parc fermé, range and contraindication filters (the `alternatives` in the §4 record). Anything invalid or late falls back to the table order. Starts in shadow; promoted per decision type through the gate |
| (a) Offline table synthesis | **Accepted** | Summarises guides and past digests into *candidate* rows for a human to review into `setup_rules.yaml` via PR + fixture + gate |
| (b) Debrief narration | **Accepted, after the core ships** | Narrates the §4 records in the labelled §08 generated section of `16-debrief-design.md`; no number that isn't in the record |
| (c) Post-session Q&A | **Accepted, opt-in** | The item-21 ask box over `setup_states`, `setup_recs`, `outcomes`, stints |
| Spoken phrasing | **Unchanged** | Garage and race lines stay YAML `say` pools (ADR 0008); the model's `reason` shows only on the dashboard |

**No key, no model.** The LLM is strictly optional. With no key (the default), no provider
is constructed and no request is made. Every recommendation is the table's top candidate,
the `llm_*` fields in `setup_recs` stay null, and the second-opinion panel is hidden. A1–A4
are the complete advisor; nothing in them waits on or degrades without A5.

**Decision.** Build the deterministic advisor first (A1–A4). Then add the chooser for
`setup.debrief` and `setup.garage` in **shadow** (A5). The model's pick goes into
`setup_recs` as an extra field (`llm_choice`, `llm_reason`, `llm_log_id`), and the debrief
shows it beside the table's primary, labelled. Promotion to chooser follows ADR 0009 §6:
the table and the model are graded by the same §6 loop. A shadow pick counts only when the
driver actually ran it, because the debrief lists every candidate and the driver picks.
With `rule_id = "llm:setup.debrief"` in `outcomes`, `pitwall digest` / `tune` compare the
two directly.

**Consequences.** The model can only ever suggest a change the tables already allow, so the
worst case is a worse legal choice, which §6 measures. Replay reproduces advice from the
logged replies. Without a key the advisor behaves exactly as without the model. Because
learning (`fold_param` gains) is keyed by symptom and param, not by who chose, both the
tables and the model improve from the same outcomes.

## 8. Implementation plan (if approved)

| Phase | Scope | Exit |
|-------|-------|------|
| A1 | Store `parc_ferme_rules`, `next_front_wing_value`; `setup_states` / `setup_changes` / `stints.setup_state_id` migration; traction detector and `slip_balance` accumulator in `state/` | a recorded practice session produces runs keyed by setup state; replay test pins both detectors |
| A2 | `setup_rules.yaml` + pure evaluator; `pitwall setup <session>` CLI prints recs with evidence; pressure advice moved in as one family | golden-output tests on recorded sessions; one fixture per rule |
| A3 | Debrief §07 integration (practice), garage advice in the pit board's `SETUP` block (no separate setup page), race-mode bias/on-throttle/front-wing-at-stop calls through the dispatcher | first weekend (FP → Q → R) driven with it; parc fermé matrix confirmed or corrected |
| A4 | Grading into `outcomes`, `fold_param` gains and baselines, `pitwall tune` / digest `setup` block | a second visit to a track re-ranks candidates from the first |
| A5 (optional) | ADR 0009 chooser for `setup.debrief` / `setup.garage` in shadow; `llm_choices` log and replay; LLM narration in debrief §08 | behind the key, off by default; shadow picks graded next to the table's for ≥ `llm_promote_min_graded` decisions before any promotion |

## 9. Open questions

- When exactly the game locks the setup in qualifying (session start vs first pit exit),
  and whether tyre pressures can change at a race stop.
- Setup-screen ranges and step sizes for F1 26 (pro setups in S5 show e.g. front wing up
  to 50, on-throttle 100, off-throttle 80, camber −3.5 / −2.0, toe 0 / 0.1); learn from the
  packet stream rather than hard-coding.
- `wheel_slip_angle` sign convention and a clean steady-state cornering filter for
  `slip_balance`.
- `tyre_temperature` session setting values: confirm that core temps are meaningful only
  with carcass simulation on.
- Whether brake heat reaches the tyres in F1 26 (S3 vs S8); matters for bias → front-temp
  side effects.
