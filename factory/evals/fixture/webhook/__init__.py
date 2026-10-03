"""A tiny webhook delivery service, the factory's end-to-end fixture.

Accepts POST /webhook with a JSON body and stores each delivery in memory.
Small enough that a factory run against it costs minutes, real enough to
give build's unit and e2e layers something genuine to exercise.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

DELIVERIES: list[dict] = []


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "malformed body"}')
            return
        DELIVERIES.append(payload)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"stored": true}')

    def log_message(self, _format: str, *_args: object) -> None:
        pass  # keep the fixture's stdout quiet during a factory run


def serve(host: str = "127.0.0.1", port: int = 8765) -> HTTPServer:
    server = HTTPServer((host, port), WebhookHandler)
    return server
