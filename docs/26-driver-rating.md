# Driver rating

The driver rating summarizes clean laps, tyre management, errors, contact, overtakes,
infringements and race result.
The engine learns it after qualifying and race sessions. The rating ranges from 0 to 1.
The rating appears as the dashboard DRV chip, in the `pitwall start` banner, and in session-end output.

## Components

The engine uses valid green laps with positive lap times and no traffic. It calculates one
median clean lap time for each rival with at least three clean laps.

| Component | Measurement | Score |
| --- | --- | --- |
| Pace | Median player pace minus the field median, or the learned base pace when fewer than three rivals qualify | `0.5 - delta / (2 × 1500 ms)` |
| Consistency | Population standard deviation divided by mean lap time | `1 - CV / 0.02` |
| Degradation | Session degradation minus the learned compound prior | `0.5 - delta / (2 × 60 ms/lap)` |
| Errors | Lock-ups, spins and off-track excursions per lap | `1 - errors / 0.5` |
| Contact | Contact episodes per lap | `1 - contacts / 0.2` |
| Position | Grid position minus classified position | `0.5 + places / (2 × 10)` |
| Overtakes | On-track overtakes made minus lost per lap, from OVTK events | `0.5 + net / (2 × 0.5)` |
| Infringements | Steward warnings plus weighted penalties per lap, from PENA events | `1 - (warnings + 3 × penalties) / 0.5` |

An OVTK counts only when the other car's latest LapData shows it as active or finished.
It must not be in the pit lane or have pitted on its current lap. The car must have had no
drive-through, stop-go or time penalty in this session. Warnings count corner cuts and
track-limit warnings (infringement types 7 and 27–29). Off-track overtake warnings (8 and 9)
do not count. Penalties count PENA types 0, 1 and 4. LapData `penalties` holds seconds, so the
rating does not use it. Off-track excursions stay under Errors. Sessions without event
telemetry leave both components out.

Each score is clamped to the range 0 to 1. The rating is the mean of available component
scores. Missing measurements do not count as zero. A profile with no available scores uses
0.5.

## Session weights

Practice sessions and time trials do not update the rating. Qualifying sessions have weight
1. Race sessions have weight 5. The engine needs three clean laps before it creates a profile.
It stores each component and the composite rating for the track and for the global profile.

The global profile uses track ID `-2`. Track ID `-1` remains reserved for tune settings.
The engine uses a track rating when its weight meets `driver_min_weight`. Otherwise it uses
the global rating.

## Confidence and advice

Confidence is `weight / (weight + driver_confidence_k)`. The default `driver_confidence_k`
is 10. A race profile with weight 5 has confidence 0.33.

Ratings at or above `driver_rating_neutral` do not change pit advice. Below neutral, the
engine calculates `deficit = clamp((neutral - rating) / neutral)` and multiplies it by
confidence to get the effective deficit. It lowers confidence and adds risk to undercut,
overcut, cover and defend plans. It adds the pace cap to our undercut laps and old-tyre
overcut laps. Cover calculations do not use the pace cap.

For a rating of 0.3 and weight 10, confidence is 0.5. The deficit is 0.4 and the effective
deficit is 0.2. Advice confidence is multiplied by 0.94, plan risk rises by 0.04 and the
pace cap is 60 ms per lap.

## Thresholds

The defaults are in `src/pitwall/config/defaults/settings.yaml`.

| Threshold | Default | Purpose |
| --- | ---: | --- |
| `driver_min_laps` | 3 | Clean laps required for a profile |
| `driver_field_min_cars` | 3 | Rivals required for field-relative pace |
| `driver_pace_span_ms` | 1500 | Pace delta spanning half the rating scale |
| `driver_cv_max` | 0.02 | Coefficient of variation for a zero score |
| `driver_deg_span_ms` | 60 | Degradation delta spanning half the rating scale |
| `driver_errors_max` | 0.5 | Errors per lap for a zero score |
| `driver_contact_max` | 0.2 | Contact episodes per lap for a zero score |
| `driver_position_span` | 10 | Places gained spanning half the rating scale |
| `driver_overtake_span` | 0.5 | Net on-track places per lap spanning half the rating scale |
| `driver_infringements_max` | 0.5 | Weighted infringements per lap for a zero score |
| `driver_penalty_weight` | 3 | Warnings counted for each penalty |
| `driver_fold_weight_quali` | 1 | Qualifying profile weight |
| `driver_fold_weight_race` | 5 | Race profile weight |
| `driver_confidence_k` | 10 | Weight for half confidence |
| `driver_rating_neutral` | 0.5 | Rating that does not scale advice |
| `driver_low_confidence_factor` | 0.7 | Confidence factor at full deficit |
| `driver_risk_add` | 0.2 | Risk added at full deficit and confidence |
| `driver_pace_cap_ms` | 300 | Pace cap at full deficit and confidence |
| `driver_min_weight` | 1 | Weight required to use a learned profile |
