# 0007 — Driving-event signals: lock-ups, boost, yellows, out-lap timing

Status: accepted (2026-09)

## Context

Live-driving feedback asked for four calls: a useful out-lap tyre update,
yellow flags relative to the player, boost left on, and lock-ups. Checked
against the first live recording (Brazil practice, 305 s, full profile):

- **Out-lap.** Every tyre is cold at pit exit, so a "front left cold" call on
  leaving the pits says nothing the driver doesn't know.
- **Lock-ups.** Motion Ex (id 13) carries `wheel_slip_ratio` per wheel
  (RL, RR, FL, FR). Braking lock-ups show as slip ≤ −0.3 for 0.3–1 s; normal
  braking stays above −0.15. Five front lock-ups and no rear ones in the lap
  set.
- **Boost.** Car Status `ers_deploy_mode == 3` ran for 5–11 s at a time, always
  at full throttle, and was switched off before the braking zone every time.
  Car Telemetry 2 `overtake_active` read 1 for most of the session, so it is
  not a useful "boost on" signal.
- **Yellows.** The Session packet (2 Hz) has marshal zones (`zone_start` as a
  lap fraction, `zone_flag` 3 = yellow) and sector 2/3 start distances. Every
  yellow in the recording went up with the player inside that zone: the
  player's own off.

## Decision

- Out-lap tyre call fires once on entering **sector 3** of the out-lap, from
  the coldest inner temperature latched at sector-3 entry: either "still
  coming in" (with coldest tyre, front and rear averages) or "tyres are in".
- Lock-up = any wheel slip ≤ −0.3, braking ≥ 10 %, speed ≥ 50 km/h, for
  ≥ 0.25 s. Reported after the wheel releases, grouped by axle: front →
  degradation warning; rear → "move the brake bias forward" with current bias.
- Boost warning (P1) once boost has been on ≥ 3 s **and** the driver lifts
  (throttle ≤ 50 %) or brakes, or ≥ 12 s regardless. Once per activation.
- Yellow granularity is the marshal zone (~200–400 m). Ahead = the way
  forward to the zone is shorter than the way back; P1 inside 800 m with
  sector and distance. A yellow that appears behind is a P3 "you're clear".
  Zones that go yellow with the player inside them are ignored.
- `lite` and `minimal` recording profiles keep Motion Ex at 10 Hz so lock-up
  calls replay (a lite trim of the Brazil file fires the same five calls).

## Consequences

- An incident by another car inside the player's own marshal zone at onset is
  treated as the player's and not called; the driver can see it.
- ABS-on setups will rarely reach −0.3 slip, so lock-up calls go quiet; that
  is correct.
- Thresholds live in `thresholds.yaml`; wording in `rules/shared.yaml`.
- Rear lock-ups and boost-into-corner are covered by synthetic tests only
  until a live recording contains one.
