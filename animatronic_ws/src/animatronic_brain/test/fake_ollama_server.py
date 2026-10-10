"""Minimal fake Ollama HTTP server for brain tests. Supports POST
/api/chat (with a configurable response body, status code, and
artificial delay for timeout tests) and GET /api/tags for health
checks. Not a real Ollama — just enough surface for OllamaClient.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class FakeOllamaServer:
    def __init__(
        self,
        chat_content: str | None = None,
        chat_choice: str | None = None,
        status_code: int = 200,
        delay_s: float = 0.0,
        health_status_code: int = 200,
    ):
        """Exactly one of chat_content/chat_choice should be set for a
        successful response. chat_choice is the convenience form that
        builds a valid {"choice": ...} JSON content string."""
        if chat_choice is not None:
            chat_content = json.dumps({"choice": chat_choice})
        self.state = {
            "chat_content": chat_content if chat_content is not None else json.dumps({"choice": "none"}),
            "status_code": status_code,
            "delay_s": delay_s,
            "health_status_code": health_status_code,
        }
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://127.0.0.1:{port}"

    def __enter__(self) -> "FakeOllamaServer":
        state = self.state
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path != "/api/chat":
                    self.send_response(404)
                    self.end_headers()
                    return

                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)

                if state["delay_s"]:
                    time.sleep(state["delay_s"])

                try:
                    self.send_response(state["status_code"])
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    body = json.dumps({"message": {"content": state["chat_content"]}})
                    self.wfile.write(body.encode("utf-8"))
                except (BrokenPipeError, ConnectionResetError):
                    pass  # client already gave up (e.g. timeout test)

            def do_GET(self):
                if self.path == "/api/tags":
                    self.send_response(state["health_status_code"])
                    self.end_headers()
                else:
                    self.send_response(404)
                    self.end_headers()

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return server

    def __exit__(self, *exc_info):
        self._server.shutdown()
        self._server.server_close()
