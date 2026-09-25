# 0005 — Recordings never enter git; learnings do

Status: accepted (2026-09)

## Context

`.f1bin` recordings are 20–30 MB per practice session, contain participant
names (lobby/participants packets) and the session UID, and grow with every
run. They are the primary debugging artefact, so the temptation is to commit
the one that reproduced a bug.

## Decision

- `recordings/`, `*.f1bin`, `*.f1idx`, `*.f1bin.zst` and `decisions/` are
  git-ignored. Recordings live on the game PC (and are shared ad hoc, e.g. a
  zip in a conversation) — never in the repository.
- What a recording taught us is written up as an ADR in `docs/adr/`, with the
  observed values (sizes, rates, header fields, what the game actually sends)
  so the fact survives without the file.
- Regression tests use synthetic packets from `tests/synth.py`, never real
  captures. If a real capture is needed to reproduce a bug, `pitwall trim`
  the relevant seconds with participant redaction and keep it outside git.

## Consequences

- Facts learnt from the first session, now recorded here: format 2026 with
  game year 25 / v1.26; all 17 packet sizes match the layout tables; 30 Hz
  send rate is sustained with zero drops over 5 minutes; car damage arrives in
  the Car Damage packet and was previously parsed but not surfaced.
- A future contributor can trust the ADRs as the ground truth for what the
  live game does, independent of anyone's local recordings.
