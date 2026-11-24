"""Unit tests for pr-evidence.py extract: the PR's Evidence shows only the scenarios picked.

Run from the repo root: python3 -m unittest discover -s dev/evals/tests -t .
"""

from __future__ import annotations

import json
import re
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
        report = tmp / "report.json"
        report.write_text(
            json.dumps(data), encoding="utf-8"
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


KINDS = (
    "unit", "functional", "component", "contract", "property", "fuzz", "formal", "static-analysis", "lint",
    "coverage", "mutation", "regression", "acceptance", "smoke", "benchmark", "performance", "ui-ux",
    "usability", "threat-model", "penetration",
)


def quality(**rows: str) -> str:
    """A complete Quality table: each kind ran unless a keyword gives its result."""
    lines = ["## Quality", "", "| Kind | Result | Found | Fixed | Surviving |", "| --- | --- | --- | --- | --- |"]
    for kind in KINDS:
        result = rows.get(kind.replace("-", "_"), "ran")
        if result is not None:
            counts = "| 0 | 0 | 0 |" if result == "ran" else "| | | |"
            lines.append(f"| {kind} | {result} {counts}")
    return "\n".join(lines) + "\n"


EVIDENCE = "## Evidence\n**Before**\n```text\npending\n```\n**After**\n```text\naccepted\n```\n"


class GateCase(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.source = self.root / "app.py"
        self.source.write_text("def total():\n    return 6\n")
        self.body = self.root / "pr.md"

    def check(self, body: str, current: bool = False, spec: str | None = None, complete: bool = True) -> subprocess.CompletedProcess[str]:
        """Check a body; unless complete is False, add the parts every body needs first, as ship writes them."""
        if complete:
            body += "" if "## Quality" in body else "\n" + quality()
            body += "" if "## Not verified" in body else "\n## Not verified\n\nNothing; every planned kind ran.\n"
            body += "" if "DEV-EVIDENCE" in body else "\n<!-- BEGIN DEV-EVIDENCE -->\n<!-- END DEV-EVIDENCE -->\n"
        self.body.write_text(body)
        if complete and "QUALITY-SUMMARY" not in body:
            subprocess.run([sys.executable, str(SCRIPT), "summarize", str(self.body)], check=True, capture_output=True)
        args = [sys.executable, str(SCRIPT), "check", str(self.body)]
        if spec is not None:
            (self.root / "spec.md").write_text(spec)
            args += ["--spec", str(self.root / "spec.md")]
        if current:
            args += ["--require-current", "--repo-root", str(self.root)]
        return subprocess.run(args, capture_output=True, text=True)

    def provenance(self) -> str:
        return "<!-- dev-evidence: " + json.dumps({
            "revision": "actual-feature-revision",
            "command": ["python3", "tests.py"],
            "sources": {"app.py": hashlib.sha256(self.source.read_bytes()).hexdigest()},
        }) + " -->\n"



class EvidenceGateTest(GateCase):
    def test_captured_heading_is_not_a_section_delimiter(self) -> None:
        result = self.check(
            "## Evidence\n**Before**\n```text\n## State\npending\n```\n"
            "**After**\n```text\n## State\naccepted\n```\n" + quality()
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

    def test_an_app_named_todo_is_not_a_placeholder(self) -> None:
        # Reproduces a real ship run on a todo CLI: the provenance marker and the captured
        # command name todo.py, which the placeholder pattern read as a TODO.
        self.source = self.root / "todo.py"
        self.source.write_text("print('todo')\n")
        marker = "<!-- dev-evidence: " + json.dumps({
            "revision": "r1", "command": ["python3", "todo.py"],
            "sources": {"todo.py": hashlib.sha256(self.source.read_bytes()).hexdigest()},
        }) + " -->\n"
        body = ("## Evidence\n" + marker + "**add** - python3 todo.py add late\n"
                "**Before**\n```json\n[]\n```\n**After**\n```json\n[{\"title\": \"late\"}]\n```\n")
        result = self.check(body, current=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_capitalized_todo_is_still_a_placeholder(self) -> None:
        result = self.check("## Evidence\n**Before**\n```\npending\n```\n**After**\n```\naccepted\n```\nTODO: add a screenshot\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("placeholder", result.stderr)

    def test_source_change_invalidates_old_evidence(self) -> None:
        body = "## Evidence\n" + self.provenance() + "![Actual UI](https://example.invalid/capture.png)\n"
        self.assertEqual(self.check(body, current=True).returncode, 0)
        self.source.write_text("def total():\n    return 3\n")
        result = self.check(body, current=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale for app.py", result.stderr)


class QualityGateTest(GateCase):
    def test_a_table_with_every_kind_passes(self) -> None:
        result = self.check(EVIDENCE + quality(fuzz="skipped (no parser or untrusted input)"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_missing_kind_is_rejected(self) -> None:
        result = self.check(EVIDENCE + quality(penetration=None))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Quality has no row for penetration", result.stderr)

    def test_a_skip_needs_its_reason(self) -> None:
        result = self.check(EVIDENCE + quality(fuzz="skipped"))
        self.assertIn("Quality row fuzz reads 'skipped'; write ran, or skipped (reason)", result.stderr)

    def test_a_body_without_a_quality_section_is_rejected(self) -> None:
        self.assertIn("no '## Quality' section", self.check(EVIDENCE, complete=False).stderr)

    def test_a_kind_the_spec_plans_may_not_be_skipped(self) -> None:
        spec = "# S\n\n## Scope\n\n### Verification\n\n- fuzz: yes - the parser\n- ui-ux: no - no UI\n\n## Change plan\n"
        result = self.check(EVIDENCE + quality(fuzz="skipped (ran out of time)", ui_ux="skipped (no UI)"), spec=spec)
        self.assertEqual(result.returncode, 1)
        self.assertIn("the spec plans fuzz (yes in its Verification block), but Quality skips it", result.stderr)
        self.assertNotIn("ui-ux", result.stderr)

    def test_the_kind_lists_agree_with_lint_spec_and_the_reference(self) -> None:
        lint = (SCRIPT.parents[2] / "scope" / "scripts" / "lint-spec.py").read_text(encoding="utf-8")
        reference = (SCRIPT.parents[3] / "references" / "verification.md").read_text(encoding="utf-8")
        table = reference[reference.index("## The kinds"):reference.index("## Scenario tags")]
        listed = re.findall(r"^\| `([a-z-]+)` \|", table, re.MULTILINE)
        self.assertEqual(tuple(listed), KINDS)
        for kind in KINDS:
            self.assertIn(f'"{kind}"', lint)
            self.assertIn(f'"{kind}"', SCRIPT.read_text(encoding="utf-8"))



ACCEPTANCE = """## Acceptance criteria

| Scenario | Criterion | Test | Status |
| --- | --- | --- | --- |
| CS1-S1 | price 3 -> total 6 | `tests.CouponTest::test_total` | awaiting CI |
"""
SPEC = "# S\n\n## Change plan\n\n1. Double\n   a. app.py - total\n   tests: [unit] price 3 -> total 6; [unit] price 0 -> total 0\n"


class BodyStructureTest(GateCase):
    def test_a_complete_body_passes(self) -> None:
        result = self.check(EVIDENCE + ACCEPTANCE)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_agent_may_not_write_inside_the_ci_block(self) -> None:
        body = EVIDENCE + "\n<!-- BEGIN DEV-EVIDENCE -->\n### ✅ Verified by CI\n<!-- END DEV-EVIDENCE -->\n"
        self.assertIn("the DEV-EVIDENCE block is not empty; only CI's ci-evidence.py writes there", self.check(body).stderr)

    def test_the_ci_markers_are_required(self) -> None:
        body = EVIDENCE + quality() + "\n## Not verified\n\nNothing.\n"
        self.assertIn("no <!-- BEGIN DEV-EVIDENCE -->", self.check(body, complete=False).stderr)

    def test_the_agent_may_not_set_an_acceptance_status(self) -> None:
        result = self.check(EVIDENCE + ACCEPTANCE.replace("awaiting CI", "✅ passed"))
        self.assertIn("Acceptance criteria CS1-S1 sets its own status '✅ passed'; write 'awaiting CI'", result.stderr)

    def test_every_spec_scenario_needs_an_acceptance_row(self) -> None:
        result = self.check(EVIDENCE + ACCEPTANCE, spec=SPEC)
        self.assertIn("Acceptance criteria has no row for CS1-S2", result.stderr)
        self.assertNotIn("CS1-S1", result.stderr)

    def test_not_verified_is_required(self) -> None:
        body = EVIDENCE + quality() + "\n## Not verified\n\n<!-- BEGIN DEV-EVIDENCE -->\n<!-- END DEV-EVIDENCE -->\n"
        self.body.write_text(body)
        subprocess.run([sys.executable, str(SCRIPT), "summarize", str(self.body)], check=True, capture_output=True)
        result = subprocess.run([sys.executable, str(SCRIPT), "check", str(self.body)], capture_output=True, text=True)
        self.assertIn("no non-empty '## Not verified' section", result.stderr)

    def test_the_summary_groups_every_kind_and_marks_open_calls_and_skips(self) -> None:
        body = EVIDENCE + quality(fuzz="skipped (no parser)").replace("| coverage | ran | 0 | 0 | 0 |", "| coverage | ran | 1 | 0 | 1 |")
        self.check(body)
        written = self.body.read_text()
        self.assertIn("| Tests | ✅ Unit · ✅ Functional", written)
        self.assertIn("✅ Static analysis", written)
        self.assertIn("⏭️ Fuzz", written)
        self.assertIn("⚠️ Coverage", written)
        self.assertIn("19 of 20 kinds ran", written)

    def test_a_hand_edited_summary_is_rejected(self) -> None:
        self.check(EVIDENCE)
        edited = self.body.read_text().replace("⏭️", "✅").replace("✅ Unit", "✅ Unit (all good)")
        self.assertIn("the QUALITY-SUMMARY block does not match the Quality table", self.check(edited).stderr)


if __name__ == "__main__":
    unittest.main()
