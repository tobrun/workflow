"""Independent acceptance checks for the dashboard page (real-host task)."""
import os
import sys
import unittest

sys.path.insert(0, os.environ["BENCH_CHECKOUT"])
from dashboard import render  # noqa: E402
from webhook import Webhooks  # noqa: E402


class Acceptance(unittest.TestCase):
    def test_page_lists_processed_ids_and_ignored_badge(self):
        hooks = Webhooks()
        for delivery in ("a", "a", "b"):
            hooks.deliver(delivery, {})
        page = render(hooks)
        self.assertIn("a", page)
        self.assertIn("b", page)
        self.assertIn("1 ignored", page)
