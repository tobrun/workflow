import unittest

from webhook import Webhooks


class WebhookTests(unittest.TestCase):
    def test_repeated_id_ignored(self):
        hooks = Webhooks()
        self.assertEqual(hooks.deliver("d1", {}), "processed")
        self.assertEqual(hooks.deliver("d1", {}), "ignored")
        self.assertEqual(hooks.processed, ["d1"])
