# Architecture decision records

One file per decision or hard-won lesson, numbered, never rewritten — supersede
with a new record instead. Each has **Context** (what we saw), **Decision**
(what we do now), **Consequences** (what to remember). Anything learnt from a
live F1 26 session belongs here so the next parser, rule or UI change does not
repeat it.

Recordings (`*.f1bin`, `*.f1idx`, `recordings/`, `decisions/`) stay on the game
PC and are git-ignored; the ADR records the finding, not the data.

| # | Title |
|---|-------|
| [0001](0001-format-2026-is-the-only-header-gate.md) | `m_packetFormat == 2026` is the only header gate |
| [0002](0002-two-clock-domains.md) | Two clock domains: session time vs receive clock |
| [0003](0003-doctor-counts-raw-datagrams.md) | Doctor counts raw datagrams, not accepted ones |
| [0004](0004-udp-bind-any-and-explicit-ws-dep.md) | Bind UDP on 0.0.0.0; ship `websockets` explicitly |
| [0005](0005-recordings-never-committed.md) | Recordings never enter git; learnings do |
