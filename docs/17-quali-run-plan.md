# Qualifying run plan

`state/runplan.py` decides the plan after a hot lap crosses the line.

| Plan | When |
|---|---|
| `push` | Another hot lap is possible and cooling is not needed |
| `cool` | Battery is low or tyres are hot, with time and fuel for a cool and hot lap |
| `push_now` | Cooling is needed, but there is not enough time or fuel for it |
| `box` | The flag has fallen, the qualifying margin is safe, or fuel at the line is too low for a hot lap, including after a cool lap |

The decision uses qualifying margin, battery, hottest tyre temperature, time left, and
fuel laps. It reads limits from packaged YAML thresholds.

`RunTracker` follows hot and cool laps across line crossings. The session snapshot and
server API expose the plan and its reason. Rules in
`config/defaults/rules/qualifying.yaml` speak the plan and give cool-lap reminders.

The dashboard API includes the plan, but the web page does not yet show a plan panel.
A push call after a cool lap needs fuel for a hot lap. Another cool lap, or a retry after a deleted lap, needs fuel for a cool lap and a hot lap.
