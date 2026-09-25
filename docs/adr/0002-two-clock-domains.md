# 0002 — Two clock domains: session time vs receive clock

Status: accepted (2026-09, first live dashboard)

## Context

The first live dashboard showed `packet age 153,000,000 ms` and stayed STALE,
which also silenced every rule through the staleness gate. `Snapshot.last_packet_t`
carried `header.session_time` (game clock, seconds since the session started)
while `snapshot.now` was the receive/tick clock (`time.monotonic()`); the UI
subtracted one from the other. The replay `--serve` path had the mirror-image
bug: record offsets fed into `recv_time` while the engine clock was wall time,
giving a WS latency p99 of ~2.4 million ms.

## Decision

Two domains, never mixed:

| Domain | Source | Used for |
|---|---|---|
| **session time** | `header.session_time` | EMAs, lap timing, packet-source age inside rules (`requires:`), replay seek |
| **receive/tick clock** | `recv_time` from the UDP protocol / `Engine.clock.now()` | packet freshness (LIVE/STALE), packet→snapshot and packet→WS latency, cooldowns |

`SessionState` keeps both (`last_packet_t` = session, `last_recv_wall` =
receive) and the snapshot's freshness field is the receive one. In real-time
replay the engine clock is a `ReplayClock` whose `now()` *is* record time, so
every consumer (state, dispatcher, hub, metrics) still sees a single domain.
Computed ages are clamped at zero as a last defence.

## Consequences

- Any new field that is a timestamp must say which domain it is in, in its name
  or docstring. Reviewers reject `a - b` across domains.
- The WebSocket envelope `t` is `time.time()` for the browser only; it is not a
  latency input.
- Tests: `tests/test_server.py` pins the ~100 ms live case and the stale case
  with a large session time, so the regression cannot come back silently.
