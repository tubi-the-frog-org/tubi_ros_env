"""Minimal fake OpenAI-compatible HTTP server for decision client
tests. Serves POST /chat/completions with a scripted sequence of
responses (so a 429-then-success retry can be tested), and supports an
artificial delay for timeout tests. Not a real OpenAI API -- just
enough surface for DecisionClient.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeDecisionServer:
    def __init__(self, responses: list[dict] | None = None, delay_s: float = 0.0):
        """Each entry in `responses` is one of:
          {"status": 200, "snack": "chips", "reply": "..."}  -- valid structured content
          {"status": 200, "content": "<raw string>"}          -- custom/malformed content
          {"status": 429}  or  {"status": 500}                -- error, empty-ish body
        Consumed in request order; the last entry repeats once the list is exhausted.
        """
        self.responses = responses or [{"status": 200, "snack": "none", "reply": "ok"}]
        self.delay_s = delay_s
        self.call_count = 0
        self._lock = threading.Lock()
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://127.0.0.1:{port}"

    def _next_response(self) -> dict:
        with self._lock:
            index = min(self.call_count, len(self.responses) - 1)
            self.call_count += 1
            return self.responses[index]

    def __enter__(self) -> "FakeDecisionServer":
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path != "/chat/completions":
                    self.send_response(404)
                    self.end_headers()
                    return

                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)

                if server.delay_s:
                    time.sleep(server.delay_s)

                entry = server._next_response()
                status = entry.get("status", 200)

                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    if status == 200:
                        if "content" in entry:
                            content = entry["content"]
                        else:
                            content = json.dumps({"snack": entry.get("snack"), "reply": entry.get("reply", "")})
                        body = json.dumps({"choices": [{"message": {"content": content}}]})
                    else:
                        body = json.dumps({"error": {"message": "fake error"}})
                    self.wfile.write(body.encode("utf-8"))
                except (BrokenPipeError, ConnectionResetError):
                    pass  # client already gave up (e.g. timeout test)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return server

    def __exit__(self, *exc_info):
        self._server.shutdown()
        self._server.server_close()
