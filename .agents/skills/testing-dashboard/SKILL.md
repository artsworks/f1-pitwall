---
name: pitwall-replay-ui-testing
description: Exercise dashboard, menu, radio synchronization, responsive layouts, and telemetry takeovers with actual replay recordings.
---

# Replay-backed dashboard checks

Use this browser and screenshot procedure only for PRs that change `web/` or the
server endpoints that the dashboard uses. For backend-only changes, run pytest
and check endpoints with FastAPI `TestClient`.

Run from the repository root with `uv sync`, then
`uv run pitwall replay /absolute/path/to/recording.f1bin --serve --speed 1`.
Open `/` and `/radio` on localhost:8000 in separate tabs. The replay command may
perform a recording pre-pass before listening; use server readiness output rather
than assuming the port is immediately available. Restart after Python changes;
reload after frontend changes.

## Devin Secrets Needed

None for a local unauthenticated replay. Obtain any external recordings through
the lead; do not substitute frontend state mocks.

## UI controls and evidence

- Seek through the review transport into a lap containing useful rivals and plan
  data. Preserve the original recording. Hard-reload after repeated seeks if call
  IDs already retained by the frontend prevent fresh call-motion observations.
- Focus non-control page content before keyboard tests. `P` cycles backend pages;
  Up/Down open or navigate the driver menu, Enter confirms, Escape closes.
  Input/button focus can intentionally exclude menu key handling.
- At 1x, measure the six-second menu timeout with a read-only DOM observer and
  corroborate visible opening/closing with video. Do not time it at accelerated
  replay speed. Menu and radio replies are shared across already-open tabs.
- Verify the three-row radio cap against both current and previous banner calls,
  not merely the number of DOM rows.
- In race duel mode, check both rival cards when the fixture supplies both rivals,
  populated plan/footer fields, and hidden duplicate strategy ahead/behind rows.
- Capture exact 1920x1080, 2560x1440, and 430x932 viewports. CDP device metrics and
  screenshot capture can supplement native UI controls. Inspect visible pixels;
  scroll to the transport/footer on portrait rather than relying on DOM text.
- For genuine stale greying, terminate only the replay process you started after
  saving a live screenshot. Review pause can preserve a live clock and is not an
  equivalent disconnect test.

## Replay tests for session-derived models

Replay writes laps to the database. Copy the user's database and pass the copy with `--seed-db`. Do not pass the original.

The review pre-pass in `ReviewController._build_timeline` can write the whole race into the database before visible playback starts. Before you test cold-start model values, isolate the pre-pass database or skip the timeline step in a temporary helper outside the repo. Do not change engine calculations or recorded packets.

Play the replay in order when a model uses this session's completed laps. A review seek can skip laps that the model needs.

To take repeatable screenshots, pause the clock at checkpoints. This does not change the model.

One more packet batch can arrive between a pause request and the paused frame. Record exact values from engine samples, not from screenshots.

## Laps that depend on earlier laps

Native review seeks start a fresh engine and skip earlier packets. Because of this, a seek resets lap history, such as the cool-lap extend latch.
To test a late lap, run every earlier packet at full speed through the production replay sink. Use a temporary checkpoint runner outside the repository. Then pause the replay clock at the checkpoint.

## Driver presses on a paused clock

Spacebar presses use the engine clock, not wall time. Two presses at the same paused timestamp hit the debounce and do not count as NEG.
Between the two presses, advance recorded time by more than `bounce_ms` (60) and less than `double_press_ms` (350). After an engine reset, reload the dashboard, because call IDs start again.

## Qualifying panel on a flying lap

On a flying lap, the qualifying panel shows the lap projection (PROJ) ahead of the plan tile. A `push` plan therefore shows PROJ, not PUSH. Check the plan in the payload, and check the takeover on screen.

## Matching takeover fixtures

Do not assume a race pit stop produces the same payload as a garage/pitting
takeover. Inspect recording session types and player status with RecordingReader
and protocol decoding before selecting a fixture. Existing practice recordings
with garage/pitting can exercise the pit board; an on-track cool-lap recording is
needed for cool-down takeover. Explicitly leave unavailable states untested.

Temporary capture tooling may use `uv run --with websocket-client` and
`uv run --with pillow`; these are not application dependencies. Keep helpers,
screenshots, and any approved generated packet fixtures outside the repository.

## Speech audio checks

Run these checks with a WebSocket client on the dashboard socket, or in the browser console.

- Check that `hello.payload.audio` is `true` only when the selected engine streams audio. Piper and OmniVoice stream. `null` does not.
- After ENABLE SOUND, decode one `audio` frame payload from base64. Open it with Python `wave`. Check that it has 1 channel, a sample width of 2 and a frame rate above 0.
- Check that each call in the radio log moves from `dispatched` to `started`. A cancelled call moves to `interrupted` if it had started, else to `dropped`. An engine drop also shows `dropped`.
- Check that the `latency` block in the state payload has `tts_p99_ms`, `queue_count`, `queue_oldest_s`, `queue_wait_p99_ms` and `packet_to_ws_p99_ms`.
- In replay with a streaming engine, seek, then check that `audio` frames continue. No frame may arrive for a call id from before the seek.
