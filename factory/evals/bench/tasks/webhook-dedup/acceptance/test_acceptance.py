"""Independent acceptance checks for webhook deduplication; never copied into the target repository."""
import os
import sys
import unittest

sys.path.insert(0, os.environ["BENCH_CHECKOUT"])
from webhook import Webhooks  # noqa: E402


class Acceptance(unittest.TestCase):
    def test_a_repeated_delivery_is_ignored(self):
        hooks = Webhooks()
        self.assertEqual(hooks.deliver("a", {}), "processed")
        self.assertEqual(hooks.deliver("a", {"retry": True}), "ignored")

    def test_distinct_deliveries_are_all_processed_in_order(self):
        hooks = Webhooks()
        for delivery in ("a", "b", "c"):
            self.assertEqual(hooks.deliver(delivery, {}), "processed")
        self.assertEqual(hooks.processed, ["a", "b", "c"])

    def test_interleaved_duplicates_keep_one_entry_each(self):
        hooks = Webhooks()
        for delivery in ("a", "b", "a", "c", "b", "a"):
            hooks.deliver(delivery, {})
        self.assertEqual(hooks.processed, ["a", "b", "c"])
