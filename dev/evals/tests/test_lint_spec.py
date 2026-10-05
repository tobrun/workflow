"""Unit tests for lint-spec.py's echo check: a mismatched echo names what it should be.

Run from the repo root: python3 -m unittest discover -s dev/evals/tests -p 'test_lint_spec.py' -t .
Each test writes a small spec to a scratch directory and runs the script as a
subprocess, so it proves the CLI contract the scope skill loops against.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "dev" / "skills" / "scope" / "scripts" / "lint-spec.py"

CHOSEN = """D-storage: Where do files live?
  ✓ S3 bucket - durable across redeploys
  ✗ local disk - lost on redeploy
"""
OPEN = """D-storage: Where do files live? [open]
  ? S3 bucket - durable across redeploys
  ⚑ ask: expected retention?
"""
NOT_DOING = """D-storage: Where do files live?
  ✗ S3 bucket - no upload feature yet
  ⊘ not doing - nobody asked for uploads ? verify: support tickets; reopen on the first request
"""


def spec(decision: str, echo: str) -> str:
    return f"""# Title

## Research

{decision}
## Scope

Files go where D-storage ({echo}) says.
"""


class LintSpecEchoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def lint(self, decision: str, echo: str) -> subprocess.CompletedProcess:
        path = Path(self.tmp.name) / "spec.md"
        path.write_text(spec(decision, echo), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, check=False
        )

    def echo_errors(self, result: subprocess.CompletedProcess) -> str:
        return "\n".join(line for line in result.stdout.splitlines() if "echo" in line)

    def test_echo_copied_from_the_chosen_line_passes(self) -> None:
        self.assertEqual(self.echo_errors(self.lint(CHOSEN, "✓ S3")), "")

    def test_echo_not_in_the_chosen_line_names_the_chosen_text(self) -> None:
        result = self.lint(CHOSEN, "✓ cloud")
        self.assertIn("does not match its resolution", self.echo_errors(result))
        self.assertIn("s3 bucket", self.echo_errors(result))

    def test_chosen_echo_on_an_open_decision_expects_open(self) -> None:
        self.assertIn("expected (open)", self.echo_errors(self.lint(OPEN, "✓ S3")))

    def test_chosen_echo_on_a_not_doing_decision_expects_not_doing(self) -> None:
        self.assertIn("expected (⊘ not doing)", self.echo_errors(self.lint(NOT_DOING, "✓ S3")))

    def test_bare_check_mark_on_a_chosen_decision_passes(self) -> None:
        self.assertEqual(self.echo_errors(self.lint(CHOSEN, "✓")), "")

    def test_open_echo_on_a_chosen_decision_expects_the_chosen_text(self) -> None:
        result = self.lint(CHOSEN, "open")
        self.assertIn("does not match its resolution", self.echo_errors(result))
        self.assertIn("s3 bucket", self.echo_errors(result))

    def test_change_plan_linking_an_open_decision_is_flagged(self) -> None:
        path = Path(self.tmp.name) / "spec.md"
        path.write_text(
            spec(OPEN, "open") + "\n## Change plan\n\n1. Store files\n   a. `src/store.py` - write files - decisions: D-storage (open)\n",
            encoding="utf-8",
        )
        result = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, check=False)
        self.assertIn("the change plan links D-storage, which is still open or flagged", result.stdout)

    def test_scope_echo_of_an_open_decision_is_not_a_change_plan_link(self) -> None:
        self.assertNotIn("change plan links", self.lint(OPEN, "open").stdout)

    def test_change_plan_linking_an_open_unflagged_decision_is_flagged(self) -> None:
        path = Path(self.tmp.name) / "spec.md"
        open_only = OPEN.replace("  ⚑ ask: expected retention?\n", "")
        path.write_text(
            spec(open_only, "open") + "\n## Change plan\n\n1. Store files\n   a. `src/store.py` - write files - decisions: D-storage (open)\n",
            encoding="utf-8",
        )
        result = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, check=False)
        self.assertIn("the change plan links D-storage", result.stdout)


if __name__ == "__main__":
    unittest.main()
