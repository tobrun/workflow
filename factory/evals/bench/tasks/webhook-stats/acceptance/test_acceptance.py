"""Independent acceptance checks for delivery statistics."""
import os
import sys
import unittest

sys.path.insert(0, os.environ["BENCH_CHECKOUT"])
from webhook import Webhooks  # noqa: E402


class Acceptance(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(Webhooks().stats(), {"processed": 0, "ignored": 0})

    def test_mixed(self):
        hooks = Webhooks()
        for delivery in ("a", "a", "a", "b"):
            hooks.deliver(delivery, {})
        self.assertEqual(hooks.stats(), {"processed": 2, "ignored": 2})
