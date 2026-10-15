# Configuration, profiles and race mindset

Put human-tuned values in configuration. Ask: *would you want this to differ between
sessions, tracks, or moods?* If yes, make it configurable.

## Layers

Resolved in order, last wins:

1. **Packaged defaults.** They live in `config/defaults/` and users never edit them.
2. **User profile.** It stores your persistent preferences in `~/.pitwall/profile.yaml`.
3. **Track / session overlay** (`config/tracks/<id>.yaml`, `config/sessions/race.yaml`).
4. **Live overrides.** Dashboard toggles stay active for the session unless pinned.

Rules:

- Validated with pydantic on load *and* on hot reload. An invalid edit is rejected, the
  last good config stays live, and the UI shows the error. A config typo must never end a
  race.
- Hot reload applies file changes atomically. Each tick's snapshot sees one consistent
  config.
- Every config resolution produces a hash, stamped into the recording header and the
  decision log, so a replay always knows which settings produced a call.
- Live overrides are written to the decision log too. "It went quiet at lap 30" is
  usually "quiet mode got toggled at lap 30".

## Settings the backend should expose

**Connection**
Set `udp_host` to `127.0.0.1` when co-hosted. See `09-performance.md`.
The `udp_port` default is 20777. `send_rate_hz` defaults to 30. Set it before start.
Pitwall uses it only for health checks. See `02-ingestion.md`.
The packet format is fixed at 2026. Set `http_port` to 8000.
Expose the bind address, access token, and HTTPS certificate paths.

**Recording and data**
recording on/off, directory, retention size, auto-tag rules, persistence on/off,
database path, automatic refit and cooldown tuning (`learning.auto_calibrate`), downsample rate, redaction of online player names.

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

**Speech engine.** `speech.engine` selects an engine id from the registry in `src/pitwall/audio/speaker.py`. If the engine fails to load, Pitwall tries the fallback id. An unknown id prints a warning, and Pitwall uses `auto`.

| Engine id | Fields | Fallback |
|---|---|---|
| `auto` | none | Resolves to `piper` if the Piper voice file exists, else `null`. |
| `piper` | `piper_voice`, `piper_speed`, `voices_dir`, `tone_urgent_speed`, `tone_urgent_expression`, `tone_calm_speed`, `tone_calm_expression` | `null` |
| `omnivoice` | `omnivoice_url`, `omnivoice_timeout_s`, `omnivoice_wait_s`, and the Piper fields for cold lines | `piper` |
| `null` | none | none |

The OmniVoice server owns the voice, seed, temperatures and steps. Pitwall sends only the text. Pitwall ignores the older `omnivoice_voice`, `omnivoice_seed`, `omnivoice_language`, `omnivoice_voice_version`, `omnivoice_ref_wav` and `omnivoice_ref_text` fields. The phrase cache key covers only the text, the server model id and `radio_click`. After you change the voice, seed or steps on the server, delete `voices_dir/.phrases`.

`omnivoice_timeout_s` (default 2.5) is the timeout for the `/v1/models` check at startup. `omnivoice_wait_s` (default 5.0) is how long a cold line waits for OmniVoice audio before Piper plays. Set `omnivoice_wait_s` to 0 to play Piper at once. The call still drops if its age passes `max_call_age_s` during the wait.

All engines use `enabled`, `rate`, `volume` and `radio_click`. `warm_phrases` sets how many phrases `pitwall voice warm` renders. A new engine registers its id with `register_engine()`. See `.agents/skills/tts-engine-contract/SKILL.md`.

**Verbosity presets** let one control replace six separate settings:

| Preset | Speaks | Budget/lap | Typical use |
|---|---|---|---|
| Silent | nothing; screen only | 0 | streaming, or when a friend is on comms |
| Critical | priority 1 only | unlimited P1 | league races where you want no distraction |
| Normal | P1 + P2, P3 on straights | 4 | default |
| Coach | everything, including deltas and sector feedback | 8 | practice and learning a track |

## Race mindset

Build mindsets as a **small set of numeric biases**, not parallel rule sets. Parallel
rule sets multiply the tuning surface and drift apart. Rules reference `mode.*`
parameters; each mindset names a set of values.

The pilot ships two tuned mindsets: **balanced** (default) and **aggressive**. Keep defend
and survive in the design. Add them after replay proves the first two, rather than shipping
four untested sets.

### The vector

Each parameter maps to something a race engineer changes when told "we're attacking" or
"bring it home in the points". Tune these starting values against recordings.

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
| `call_budget_per_lap` | 12 | 15 | P2/P3 calls per lap (P1 unlimited); raised for debugging, the driver menu's "Radio calls" item overrides it live | slightly chattier, not twice as chatty |
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
    call_budget_per_lap: 12
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
    call_budget_per_lap: 15
    phrasing: directive
```

Why these particular knobs correlate with the coaching styles:

- **Appetite, not information.** Both mindsets use the same projection. Aggressive needs
  less gain (`pit_gain_min_s`) and certainty (`pit_confidence_min`) before acting. It also
  tolerates more downside (`position_loss_risk_max`). These differences define an
  attacking or measured strategy.
- **Where the margins go.** Balanced keeps margins in tyres, fuel, and battery. Aggressive
  converts them into pace. Each margin is one number, so the debrief can report what was
  spent.
- **Where attention goes.** `rival_focus_ahead` and `attack_window_s` focus the engineer
  ahead. Drivers notice this because the radio talks about the car they are chasing.
- **Tone follows.** `phrasing` changes templates, not decisions. "Box this lap" and
  "consider boxing this lap" use the same recommendation.
- **Chattiness barely moves.** Aggressive adds +1 call per lap, not twice as many. A
  hunting driver is busier, but more talk costs attention.

Guard-rails:

1. **The driver switches the mindset; the assistant never switches it silently.** It may
   *suggest* one, as a P3 call: "you're 1.2 behind and a second a lap quicker. Go
   aggressive?" An acknowledge press accepts it; a negative press dismisses it for five laps.
2. **Mindset never changes priority-1 facts.** Safety car, red flag, damage, imminent
   penalty and "box or run out of fuel" are invariant. A mindset biases *judgement*, never
   *facts*.
3. **Hard floors.** Some values are clamped regardless of mindset: never below 0 laps of
   fuel margin, never recommend running past the modelled tyre cliff, never plan the
   battery below 5%.
4. **Recorded per call.** The mindset and its hash go into the decision log, so
   `pitwall diff --b-mindset aggressive` can replay a race under the other mindset.

Switch mindsets mid-race with a wheel button (UDP Action 2) or key, then confirm by voice
("aggressive"). Use the dashboard toggle between sessions because clicking it takes focus
from a fullscreen game (`12-driver-input.md`).

## Weekend and season context

The engine uses the weekend link for degradation priors. Fitted practice stints can inform
a race on the same track and compound. Nonzero links must match. If either link is zero,
the UTC session dates must match. The engine needs at least `weekend_min_laps` fitted laps.
