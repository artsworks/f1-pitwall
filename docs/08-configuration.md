# Configuration, profiles and race mindset

Everything a human would want to tune lives in configuration. The test for whether
something belongs here: *would you ever want it different between two sessions, two
tracks, or two moods?* If yes, it is config.

## Layers

Resolved in order, last wins:

1. **Packaged defaults** (`config/defaults/`) — in the repo, never edited by the user.
2. **User profile** (`~/.pitwall/profile.yaml`) — your persistent preferences.
3. **Track / session overlay** (`config/tracks/<id>.yaml`, `config/sessions/race.yaml`).
4. **Live overrides** — toggles from the dashboard, held for the session unless pinned.

Rules:

- Validated with pydantic on load *and* on hot reload. An invalid edit is rejected, the
  last good config stays live, and the UI shows the error. A config typo must never end a
  race.
- Hot reload is on file change, and it is atomic — the snapshot for a tick sees one
  consistent config.
- Every config resolution produces a hash, stamped into the recording header and the
  decision log, so a replay always knows which settings produced a call.
- Live overrides are written to the decision log too. "It went quiet at lap 30" is
  usually "quiet mode got toggled at lap 30".

## Settings the backend should expose

**Connection**
`udp_host` (default `127.0.0.1` when co-hosted — see `09-performance.md`), `udp_port`
20777, `send_rate_hz` (default 30; set before start, used only for health checks — see
`02-ingestion.md`), packet format fixed at 2026, `http_port` 8000, bind address,
access token, HTTPS cert paths.

**Recording and data**
recording on/off, directory, retention size, auto-tag rules, persistence on/off,
database path, downsample rate, redaction of online player names.

**Engine**
tick rate (default 10 Hz), EMA windows (fast 3 s, slow 30 s), staleness thresholds per
packet, lap-validity strictness, model on/off, minimum laps before the model speaks,
confidence floor below which the engine says "not enough data" instead of a number.

**Message policy**
verbosity preset, per-priority deadlines, global calls-per-lap budget, minimum gap
between calls, straight-only gating for priority 3, per-rule enable/disable,
cooldown and max-per-stint overrides, quiet mode, and a **mute-until-lap-N** control.

**Thresholds**
thermal windows by (actual compound, track), brake windows, wear and blister limits,
fuel margin, ERS floor, penalty-warning margin. Defaults shipped; the calibration tool
fits per-driver values from recordings and writes them into the user profile.

**Speech and display**
voice, rate, pitch, volume, language, units (°C/°F, kg/lb), driver name used on radio,
theme, font scale, layout (phone landscape / monitor), colour-blind-safe palette,
visual-only mode.

**Verbosity presets** — one control most of the time, rather than tuning six numbers:

| Preset | Speaks | Budget/lap | Typical use |
|---|---|---|---|
| Silent | nothing; screen only | 0 | streaming, or when a friend is on comms |
| Critical | priority 1 only | unlimited P1 | league races where you want no distraction |
| Normal | P1 + P2, P3 on straights | 4 | default |
| Coach | everything, including deltas and sector feedback | 8 | practice and learning a track |

## Race mindset

Yes, this is worth building — and it is worth building as a **small vector of numeric
biases**, not as parallel rule sets. Parallel rule sets would multiply the tuning surface
and drift apart. Rules reference `mode.*` parameters; a mindset is a named set of values.

The pilot ships two mindsets, **balanced** (default) and **aggressive**, tuned properly.
Defend and survive stay in the design and are added once the first two are proven in
replay — two well-tuned vectors beat four guessed ones.

### The vector

Each parameter maps to something a real race engineer changes when told "we're
attacking" versus "bring it home in the points". Values are starting points to be tuned
against recordings, not conclusions.

| Parameter | Balanced | Aggressive | What it controls | The coaching behaviour it models |
|---|---|---|---|---|
| `pit_gain_min_s` | 1.0 | 0.4 | projected net gain required before recommending a stop | an aggressive engineer takes the marginal undercut |
| `pit_confidence_min` | 0.70 | 0.55 | model confidence required to speak a pit call | acts on a less certain read |
| `position_loss_risk_max` | 0.30 | 0.55 | tolerated probability that a strategy call costs a place | accepts coming out in traffic for a chance of gaining |
| `undercut_speak_threshold_s` | 1.0 | 0.4 | smallest undercut opportunity worth saying | "there's an undercut on Norris, small but it's there" |
| `sc_stop_min_wear_pct` | 35 | 25 | wear above which a safety-car stop is recommended | takes the cheap stop earlier to get fresher tyres for the restart |
| `tyre_life_buffer_laps` | 2.0 | 1.0 | laps of margin kept before the modelled cliff | runs the stint closer to the edge |
| `push_laps_per_stint` | 2 | 4 | push laps the plan allows (in-lap, out-lap, attack) | more laps asked at full pace |
| `thermal_warn_offset_c` | 0 | +3 | shift of the "too hot" warning above the compound window | tolerates heat for pace before nagging |
| `fuel_margin_laps` | 0.30 | 0.10 | fuel kept above the target at the flag | runs lighter; more lift-and-coast risk accepted late |
| `lift_coast_trigger_laps` | 0.20 | 0.35 | projected deficit before lift-and-coast is called | calls it later, only when truly needed |
| `ers_policy` | `even` | `attack_rival` | how deployment is planned across the lap | banks energy for the car ahead rather than spreading it |
| `ers_soc_floor_pct` | 20 | 10 | battery floor the plan protects | spends deeper when there's a move on |
| `attack_window_s` | 1.0 | 1.5 | gap to the car ahead at which attack coaching starts | starts working the gap from further back |
| `overtake_call_gap_s` | 0.8 | 1.0 | gap at which "Overtake available" is spoken | calls the opportunity earlier |
| `rival_focus_ahead` | 0.5 | 0.8 | weight on the car ahead vs behind (behind = 1 − ahead) | "he's on 18-lap-old mediums, you're half a second a lap quicker" |
| `rival_info_every_n_laps` | 3 | 1 | how often the rival summary is repeated | a gap every lap when hunting |
| `call_budget_per_lap` | 4 | 5 | P2/P3 calls per lap (P1 unlimited) | slightly chattier, not twice as chatty |
| `phrasing` | `advisory` | `directive` | template set used | "consider boxing" versus "box this lap" |

```yaml
mindsets:
  balanced:
    pit_gain_min_s: 1.0
    pit_confidence_min: 0.70
    position_loss_risk_max: 0.30
    undercut_speak_threshold_s: 1.0
    sc_stop_min_wear_pct: 35
    tyre_life_buffer_laps: 2.0
    push_laps_per_stint: 2
    thermal_warn_offset_c: 0
    fuel_margin_laps: 0.30
    lift_coast_trigger_laps: 0.20
    ers_policy: even
    ers_soc_floor_pct: 20
    attack_window_s: 1.0
    overtake_call_gap_s: 0.8
    rival_focus_ahead: 0.5
    rival_info_every_n_laps: 3
    call_budget_per_lap: 4
    phrasing: advisory
  aggressive:
    inherits: balanced
    pit_gain_min_s: 0.4
    pit_confidence_min: 0.55
    position_loss_risk_max: 0.55
    undercut_speak_threshold_s: 0.4
    sc_stop_min_wear_pct: 25
    tyre_life_buffer_laps: 1.0
    push_laps_per_stint: 4
    thermal_warn_offset_c: 3
    fuel_margin_laps: 0.10
    lift_coast_trigger_laps: 0.35
    ers_policy: attack_rival
    ers_soc_floor_pct: 10
    attack_window_s: 1.5
    overtake_call_gap_s: 1.0
    rival_focus_ahead: 0.8
    rival_info_every_n_laps: 1
    call_budget_per_lap: 5
    phrasing: directive
```

Why these particular knobs correlate with the coaching styles:

- **Appetite, not information.** Both mindsets see the same projection; aggressive just
  needs less gain (`pit_gain_min_s`) and less certainty (`pit_confidence_min`) before
  acting, and tolerates more downside (`position_loss_risk_max`). That is the essential
  difference between an attacking and a measured strategist.
- **Where the margins go.** Balanced keeps margins in tyres, fuel and battery; aggressive
  converts them into pace. Each margin is one number, so the debrief can say exactly which
  margin was spent.
- **Where attention goes.** `rival_focus_ahead` and `attack_window_s` move the engineer's
  eyes forward. This is what drivers notice most: the radio talks about the car you are
  chasing.
- **Tone follows.** `phrasing` changes templates, not decisions — "box this lap" and
  "consider boxing this lap" come from the same recommendation.
- **Chattiness barely moves.** Aggressive is +1 call per lap, not double. A hunting driver
  is busier, and more talk costs attention.

Guard-rails:

1. **The driver switches the mindset; the assistant never switches it silently.** It may
   *suggest* one, as a P3 call: "you're 1.2 behind and a second a lap quicker — go
   aggressive?" An acknowledge press accepts it; a negative press dismisses it for five laps.
2. **Mindset never changes priority-1 facts.** Safety car, red flag, damage, imminent
   penalty and "box or run out of fuel" are invariant. A mindset biases *judgement*, never
   *facts*.
3. **Hard floors.** Some values are clamped regardless of mindset: never below 0 laps of
   fuel margin, never recommend running past the modelled tyre cliff, never plan the
   battery below 5%.
4. **Recorded per call.** The mindset and its hash go into the decision log, so
   `pitwall diff --b-mindset aggressive` can replay a race under the other mindset.

Switching mid-race is a wheel button (UDP Action 2) or a key, confirmed by voice
("aggressive"); the dashboard toggle is for between sessions, since clicking it would take
focus from a fullscreen game (`12-driver-input.md`).

## Weekend and season context

Config also carries the things that persist across sessions in a weekend or a career:

- weekend link (P1/P2/P3 → Q → R at one track share a context, so practice data informs
  the race);
- tyre allocation remaining across the weekend;
- in career/season play, engine-component allocation and grid-penalty state, which change
  what "push now" means;
- AI difficulty and assists, since rival pace models are only comparable within a
  difficulty.

These are read from telemetry where the packets provide them and configured where they do
not.
