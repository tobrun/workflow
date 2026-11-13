"""Retired generated distributions cannot silently remain installed."""
import argparse
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_codex_plugin as generator


class RetiredDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plugins = self.root / "plugins"
        self.plugins.mkdir()
        self.retired = self.plugins / "retired"
        (self.retired / ".codex-plugin").mkdir(parents=True)
        (self.retired / ".codex-plugin" / "plugin.json").write_text("{}")
        (self.retired / "README.md").write_text(
            "Generated from `retired/` by `scripts/build_codex_plugin.py`."
        )

    def run_generator(self, check):
        with (
            patch.object(generator, "ROOT", self.root),
            patch.object(generator, "PLUGINS", {}),
            patch.object(generator, "parse_args",
                         return_value=argparse.Namespace(check=check, plugin=None)),
            redirect_stdout(io.StringIO()) as output,
        ):
            status = generator.main()
        return status, output.getvalue()

    def test_check_reports_retired_distribution_without_removing_it(self):
        status, output = self.run_generator(check=True)
        self.assertEqual(status, 1)
        self.assertIn("retired: plugins/retired", output)
        self.assertTrue(self.retired.exists())

    def test_build_removes_retired_distribution_then_check_passes(self):
        status, _ = self.run_generator(check=False)
        self.assertEqual(status, 0)
        self.assertFalse(self.retired.exists())
        self.assertEqual(self.run_generator(check=True)[0], 0)

    def test_unowned_directories_and_symlinks_are_preserved(self):
        local = self.plugins / "local"
        (local / ".codex-plugin").mkdir(parents=True)
        (local / ".codex-plugin" / "plugin.json").write_text("{}")
        (local / "README.md").write_text("Hand maintained")
        external = self.root / "external"
        (external / ".codex-plugin").mkdir(parents=True)
        (external / ".codex-plugin" / "plugin.json").write_text("{}")
        (external / "README.md").write_text(
            "Generated from `linked/` by `scripts/build_codex_plugin.py`."
        )
        link = self.plugins / "linked"
        link.symlink_to(external, target_is_directory=True)
        self.run_generator(check=False)
        self.assertTrue(local.exists())
        self.assertTrue(link.is_symlink())
        self.assertTrue(external.exists())


if __name__ == "__main__":
    unittest.main()
