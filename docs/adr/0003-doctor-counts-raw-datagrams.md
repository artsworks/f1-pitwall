# 0003 — Doctor counts raw datagrams, not accepted ones

Status: accepted (2026-09)

## Context

`pitwall doctor` reported "no datagrams in 60s" for four consecutive runs while
the game was in fact sending 200+ packets a second. It counted only packets that
survived header validation, so a wrong acceptance rule (ADR 0001) looked exactly
like a firewall or in-game settings problem. Two rounds of firewall rules and
settings screenshots were spent on the wrong hypothesis.

## Decision

Ingest counts at every stage — raw datagrams, accepted, dropped-unsupported,
dropped-malformed, dropped-size-mismatch (per packet id, with observed vs
expected size) — and `doctor` and `pitwall start` print all of them. "No
datagrams" is only reported when the raw count is zero.

## Consequences

- A diagnostic must distinguish *nothing arrived* from *everything was
  rejected*; the fix for each is on a different machine.
- The recorder sits before validation, so a session recorded while everything
  is rejected is still a full recording and can be replayed after the fix.
- When a user reports "no data", ask for the `doctor` block first; it now
  answers the transport question conclusively.
