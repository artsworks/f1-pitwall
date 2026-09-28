# Qualifying run plan

The plan is implemented in `state/runplan.py`. It is decided after a hot lap, at the line.

| Plan | When |
|---|---|
| `push` | Another hot lap is possible and cooling is not needed |
| `cool` | Battery is low or tyres are hot, with time and fuel for a cool and hot lap |
| `push_now` | Cooling is needed, but there is not enough time or fuel for it |
| `box` | The flag has fallen, the qualifying margin is safe, or fuel is too low for a hot lap |

The decision uses qualifying margin, battery, hottest tyre temperature, time left, and fuel
laps. Limits are read from packaged YAML thresholds.

`RunTracker` follows hot and cool laps across line crossings. The session snapshot and server
API expose the plan and its reason. Rules in `config/defaults/rules/qualifying.yaml` speak
the plan and give cool-lap reminders.

The dashboard API includes the plan, but the web page does not yet show a plan panel.
