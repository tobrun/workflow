"""Unit tests for pr-evidence.py extract: the PR's Evidence shows only the scenarios picked.

Run from the repo root: python3 -m unittest discover -s dev/evals/tests -t .
"""

from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "dev" / "skills" / "ship" / "scripts" / "pr-evidence.py"
PIXEL = "data:image/png;base64,iVBORw0KGgo="


def scenario(scenario_id: str, status: str = "pass") -> dict:
    return {
        "id": scenario_id,
        "title": f"Scenario {scenario_id}",
        "status": status,
        "given": "g",
        "when": "w",
        "then": "t",
        "screenshots": [{"step": f"{scenario_id} step", "caption": "c", "dataUri": PIXEL}],
    }


class ScenarioSelectionTest(unittest.TestCase):
    def extract(self, *picked: str) -> tuple[subprocess.CompletedProcess[str], Path]:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        data = {"kind": "frontend", "planName": "plan", "generatedAt": "now",
                "scenarios": [scenario("login"), scenario("coupon"), scenario("regress", "fail")]}
        report = tmp / "report.html"
        report.write_text(
            f"/* E2E_DATA_START */ const E2E_DATA = {json.dumps(data)}; /* E2E_DATA_END */", encoding="utf-8"
        )
        args = [sys.executable, str(SCRIPT), "extract", str(report), "--out", str(tmp / "out"),
                "--url-template", "https://x/{path}"]
        for wanted in picked:
            args += ["--scenario", wanted]
        return subprocess.run(args, capture_output=True, text=True), tmp / "out"

    def test_without_a_pick_every_scenario_is_kept(self) -> None:
        result, out = self.extract()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["images"]), 3)

    def test_only_the_picked_scenarios_reach_the_evidence(self) -> None:
        result, out = self.extract("coupon")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["images"]), 1)
        text = (out / "evidence.md").read_text(encoding="utf-8")
        self.assertIn("Scenario coupon", text)
        self.assertNotIn("Scenario login", text)
        self.assertIn("1/1 scenarios passed", text)

    def test_an_unknown_scenario_id_fails_naming_the_known_ones(self) -> None:
        result, _ = self.extract("nope")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no scenario with id nope", result.stderr)
        self.assertIn("login, coupon, regress", result.stderr)


class EvidenceGateTest(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.source = self.root / "app.py"
        self.source.write_text("def total():\n    return 6\n")
        self.body = self.root / "pr.md"

    def check(self, body: str, current: bool = False) -> subprocess.CompletedProcess[str]:
        self.body.write_text(body)
        args = [sys.executable, str(SCRIPT), "check", str(self.body)]
        if current:
            args += ["--require-current", "--repo-root", str(self.root)]
        return subprocess.run(args, capture_output=True, text=True)

    def provenance(self) -> str:
        return "<!-- dev-evidence: " + json.dumps({
            "revision": "actual-feature-revision",
            "command": ["python3", "tests.py"],
            "sources": {"app.py": hashlib.sha256(self.source.read_bytes()).hexdigest()},
        }) + " -->\n"

    def test_captured_heading_is_not_a_section_delimiter(self) -> None:
        result = self.check(
            "## Evidence\n**Before**\n```text\n## State\npending\n```\n"
            "**After**\n```text\n## State\naccepted\n```\n## Quality\nRequired checks passed.\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_empty_or_unclosed_blocks_do_not_count_as_proof(self) -> None:
        for body in [
            "## Evidence\n**Before**\n```\n```\n**After**\n```\n```\n",
            "## Evidence\n**Before**\n```\npending\n```\n**After**\n```\naccepted\n",
        ]:
            with self.subTest(body=body):
                self.assertNotEqual(self.check(body).returncode, 0)

    def test_known_bad_feature_revision_is_a_valid_labeled_pair(self) -> None:
        result = self.check(
            "## Evidence\n**On known-bad revision** a1\n```\nAssertionError: 3 != 6\n```\n"
            "**On corrected revision** b2\n```\n1 test passed\n```\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_provenance_cannot_pass_current_evidence_gate(self) -> None:
        result = self.check("## Evidence\n![Actual UI](https://example.invalid/capture.png)\n", current=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("provenance", result.stderr)

    def test_source_change_invalidates_old_evidence(self) -> None:
        body = "## Evidence\n" + self.provenance() + "![Actual UI](https://example.invalid/capture.png)\n"
        self.assertEqual(self.check(body, current=True).returncode, 0)
        self.source.write_text("def total():\n    return 3\n")
        result = self.check(body, current=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale for app.py", result.stderr)


if __name__ == "__main__":
    unittest.main()
