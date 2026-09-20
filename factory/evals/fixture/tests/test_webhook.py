"""Two unit tests for the webhook fixture: run with python3 -m unittest."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

import webhook
from webhook import serve


class WebhookTest(unittest.TestCase):
    def setUp(self) -> None:
        webhook.DELIVERIES.clear()
        self.server = serve(port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def post(self, body: bytes) -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/webhook", data=body, method="POST"
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_a_delivery_is_stored(self) -> None:
        status, _ = self.post(json.dumps({"event": "ping"}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(webhook.DELIVERIES, [{"event": "ping"}])

    def test_a_malformed_body_returns_400(self) -> None:
        status, body = self.post(b"not json")
        self.assertEqual(status, 400)
        self.assertIn(b"malformed body", body)
        self.assertEqual(webhook.DELIVERIES, [])


if __name__ == "__main__":
    unittest.main()
