# Architecture decision records

Write one numbered file per decision or hard-won lesson. Never rewrite a record;
supersede it with a new one. Each record has **Context** (what we saw),
**Decision** (what we do now), and **Consequences** (what to remember). Record
findings from live F1 26 sessions here so later parser, rule, and UI changes do
not repeat them.

Keep recordings (`*.f1bin`, `*.f1idx`, `recordings/`, `decisions/`) on the game
PC and git-ignored. Record findings, not data.

| # | Title |
|---|-------|
| [0001](0001-format-2026-is-the-only-header-gate.md) | `m_packetFormat == 2026` is the only header gate |
| [0002](0002-two-clock-domains.md) | Two clock domains: session time vs receive clock |
| [0003](0003-doctor-counts-raw-datagrams.md) | Doctor counts raw datagrams, not accepted ones |
| [0004](0004-udp-bind-any-and-explicit-ws-dep.md) | Bind UDP on 0.0.0.0; ship `websockets` explicitly |
| [0005](0005-recordings-never-committed.md) | Recordings never enter git; learnings do |
| [0006](0006-recording-profiles.md) | Recording profiles: lite by default, full on demand |
| [0007](0007-driving-event-signals.md) | Driving-event signals: lock-ups, boost, yellows, out-lap timing |
| [0008](0008-natural-phrasing-without-an-llm.md) | Natural phrasing without an LLM: variants, escalation, spins |
| [0009](0009-llm-chooser-with-rule-veto.md) | LLM as a gated chooser with rule veto (supersedes 0008's decision part) |
| [0010](0010-brazil-weekend-recording-review.md) | Brazil weekend recording review: rates, energy, boost and penalty calls |
