# 0004 — Bind UDP on 0.0.0.0; ship `websockets` explicitly; pin Python 3.12

Status: accepted (2026-09)

## Context

Three environment failures on the first Windows install, none visible in tests:

1. The UDP listener bound `127.0.0.1`; the game was configured to send to the
   PC's LAN address, so nothing arrived.
2. uvicorn logged `No supported WebSocket library detected` — the dashboard
   connected over HTTP but every WS upgrade failed and the page sat STALE.
   `uvicorn` alone does not pull a WebSocket implementation.
3. `uv` picked Python 3.13 on the game PC; the project targets 3.12
   (`pywin32` wheels and the tested matrix).

## Decision

- `udp.bind` defaults to `0.0.0.0`; the doctor prints the LAN address so the
  in-game IP can be either `127.0.0.1` or the LAN IP.
- `websockets` is a direct runtime dependency, not an extra.
- `.python-version` and `requires-python` pin 3.12.

## Consequences

- Working in-game settings: UDP On, Broadcast Off, IP `127.0.0.1`, port
  `20777`, 30 Hz, format `2026`. Record them in the README.
- `uvicorn` "Invalid HTTP request" / `WinError 10054` lines are browser probes
  (HTTPS, favicon) and are noise, not faults.
- Anything the dashboard needs at runtime must be an explicit dependency; the
  Linux dev box had it transitively and hid the gap.
