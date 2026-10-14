import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import numpy as np
import pytest

from pitwall.audio.wav_speaker import to_wav

SMALL_WAV = to_wav(np.zeros(240, dtype=np.float32), 24_000)


@dataclass
class FakeServerState:
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    speech_status: int = 200
    speech_delay_s: float = 0.0


@pytest.fixture
def fake_server() -> tuple[str, FakeServerState]:
    state = FakeServerState()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

        def do_GET(self) -> None:
            state.requests.append((self.path, {}))
            self._send(200, b'{"object":"list","data":[{"id":"voice-model.gguf"}]}')

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.loads(body)
            state.requests.append((self.path, payload))
            if self.path == "/v1/audio/speech":
                if state.speech_delay_s:
                    time.sleep(state.speech_delay_s)
                if state.speech_status != 200:
                    self._send(state.speech_status, b'{"error":"synthesis failed"}')
                else:
                    self._send(200, SMALL_WAV, "audio/wav")
            else:
                self._send(404, b'{"error":"not found"}')

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield url, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
