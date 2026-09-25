# 0001 — `m_packetFormat == 2026` is the only header gate

Status: accepted (2026-09, first live session)

## Context

The planning docs and the initial implementation required both
`m_packetFormat == 2026` and `m_gameYear == 26`. On the first live run every
one of 6609 datagrams was dropped as *unsupported* while `doctor` reported
"format 2026 only". The shipping F1 26 build sends
`m_gameYear = 25`, `m_gameMajorVersion = 1`, `m_gameMinorVersion = 26`.

## Decision

`is_supported()` checks `packet_format == 2026` and nothing else. Game year and
version are parsed and reported by `doctor` ("game reports: year 25 v1.26") but
never gate acceptance. Body size per packet id remains a gate because it
protects the layout tables.

## Consequences

- Never derive acceptance from a header field whose value we have only read in
  documentation; the wire discriminator is the format field.
- If a future patch changes sizes, `doctor` shows `size_mismatch` per id and
  observed-vs-expected bytes — fix the layout table, do not loosen the gate.
