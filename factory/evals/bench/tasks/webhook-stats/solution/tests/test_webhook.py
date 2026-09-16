import unittest

from webhook import Webhooks


class StatsTests(unittest.TestCase):
    def test_counts_processed_and_ignored(self):
        hooks = Webhooks()
        for delivery in ("a", "b", "a"):
            hooks.deliver(delivery, {})
        self.assertEqual(hooks.stats(), {"processed": 2, "ignored": 1})
