# ADR 0008: Natural phrasing without an LLM

- **Status:** accepted
- **Context:** live session, exploratory laps with repeated front lock-ups

## What we saw

On exploratory laps the driver locks up a lot. Every lock-up produced the same
sentence ("Lock-up, front left. Ease the brake pressure into the corner, that
costs tyre."), which quickly reads as a machine reading a log, not an engineer.
Replaying the Brazil recording also showed five genuine spins in two minutes,
all 140°–180° of sideslip from Motion Ex local velocity; a caught slide peaked
around 85°.

## Decision

Phrasing stays deterministic and in config; no LLM (reaffirms doc 10, angle 20).

- `say` accepts a list of variants. The first call uses the first (plain)
  variant; after that a shuffle bag uses every variant before any repeats and
  never says the same line twice in a row.
- `escalate: [{after: N, say: [...]}]` switches to a different pool once the
  same call has triggered N times inside `repeat_window_s` (default 600 s).
  Tone goes plain → dry → snarky for a repeated mistake, and resets after a
  quiet stretch. `{repeat}` is available in templates.
- Triggers are counted in the rule engine, before dispatcher cooldowns, so the
  count reflects the mistakes made, not the calls spoken.
- The shuffle is seeded by rule id: a replay says exactly what the live session said.
- Lock-up cooldowns went from 30 s to 60 s (front) and 45 s (rear).
- New `spun_rejoin` (P1): sideslip ≥ 100° above 30 km/h for 0.15 s. Spoken at
  once, while the car is turning round, so it lands before the rejoin.

## Why not an LLM

300 ms–2 s extra latency, a network or GPU dependency mid-race, non-reproducible
replays, and wording that can drift into saying something wrong. Variant pools
give most of the naturalness at zero cost. If ever added, an LLM belongs offline:
generating candidate variants that a human reviews into the YAML.

## Where else this applies

Any repeated call benefits: boost left on, wing damage and spins already have
escalation pools. Future M2/M3 calls (gap updates, pit windows, fuel) should
ship with 3+ variants from the start, and the dispatcher's text dedupe now
rarely triggers because consecutive texts differ.
