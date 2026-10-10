"""Minimal fake OpenAI-compatible HTTP server for TTS client tests.
Serves POST /audio/speech returning scripted raw bytes (standing in
for MP3 data) or an error status, with an optional artificial delay
for timeout tests.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeTtsServer:
    def __init__(self, audio_bytes: bytes = b"\xff\xfbfake-mp3-bytes", status_code: int = 200, delay_s: float = 0.0):
        self.audio_bytes = audio_bytes
        self.status_code = status_code
        self.delay_s = delay_s
        self.call_count = 0
        self.last_payload: bytes | None = None
        self._lock = threading.Lock()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://127.0.0.1:{port}"

    def __enter__(self) -> "FakeTtsServer":
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path != "/audio/speech":
                    self.send_response(404)
                    self.end_headers()
                    return

                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                with server._lock:
                    server.call_count += 1
                    server.last_payload = body

                if server.delay_s:
                    time.sleep(server.delay_s)

                try:
                    self.send_response(server.status_code)
                    if server.status_code == 200:
                        self.send_header("Content-Type", "audio/mpeg")
                        self.end_headers()
                        self.wfile.write(server.audio_bytes)
                    else:
                        self.end_headers()
                        self.wfile.write(b"fake error")
                except (BrokenPipeError, ConnectionResetError):
                    pass  # client already gave up (e.g. timeout test)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return server

    def __exit__(self, *exc_info):
        self._server.shutdown()
        self._server.server_close()
