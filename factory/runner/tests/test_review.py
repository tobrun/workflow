"""The review aggregator: completeness apart from verdict, strict task results, and bounded retries."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runner import FACTORY_ROOT, records

SCRIPT = FACTORY_ROOT / "skills" / "ship" / "scripts" / "aggregate-findings.py"
LENSES = ("correctness", "security")


def finding(severity: str, line: int, title: str = "issue") -> dict:
    entry = {"severity": severity, "file": "webhook.py", "line": line, "title": title, "detail": "evidence"}
    if severity == "BLOCK":
        entry["scenario"] = "a duplicate arrives during a retry"
    return entry


class AggregatorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.lenses = self.dir / "lenses"
        self.verifiers = self.dir / "verifiers"
        for batch in (self.lenses, self.verifiers):
            (batch / "prompts").mkdir(parents=True)
            (batch / "results").mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def lens(self, name: str, *findings: dict, raw: str | None = None) -> None:
        (self.lenses / "prompts" / f"{name}.md").write_text(f"You are the {name} lens.\n")
        payload = raw if raw is not None else json.dumps({"verdict": "PASS", "findings": list(findings), "good": []})
        (self.lenses / "results" / f"{name}.json").write_text(payload)

    def verifier(self, name: str, *verdicts: dict) -> None:
        (self.verifiers / "prompts" / f"{name}.md").write_text("Refute these.\n")
        (self.verifiers / "results" / f"{name}.json").write_text(json.dumps(list(verdicts)))

    def run_script(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True)

    def aggregate(self, expected=LENSES) -> dict:
        out = self.dir / "review_1.json"
        result = self.run_script("aggregate", str(self.lenses), str(self.verifiers), "--expected", ",".join(expected),
                                 "--revision", "a" * 40, "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(out.read_text()), json.loads(result.stdout))
        return json.loads(out.read_text())

    def test_r5_missing_lenses_make_the_review_incomplete_with_no_verdict(self):
        record = self.aggregate()
        self.assertEqual((record["completeness"], record["verdict"]), ("incomplete", None))
        self.assertEqual(record["missing_lenses"], ["correctness", "security"])
        with self.assertRaises(records.RecordError) as caught:
            records.load_review(self.dir / "review_1.json")
        self.assertEqual(caught.exception.code, "review.incomplete")
        self.assertIn("missing lenses: correctness, security", str(caught.exception))

    def test_a_missing_verifier_never_demotes_a_blocker(self):
        self.lens("correctness", finding("BLOCK", 3, "replay window unbounded"))
        self.lens("security")
        record = self.aggregate()
        self.assertEqual((record["completeness"], record["verdict"]), ("incomplete", None))
        self.assertEqual(record["verification"]["missing"], ["f1"])
        self.assertEqual([(b["severity"], b["verification"]) for b in record["blockers"]], [("BLOCK", "MISSING")])
        self.assertEqual(record["concerns"], [])

    def test_verifier_outcomes_decide_the_documented_verdicts(self):
        self.lens("correctness", finding("BLOCK", 3, "confirmed"), finding("BLOCK", 9, "plausible"))
        self.lens("security", finding("CONCERN", 12, "refuted"), finding("NIT", 1))
        self.verifier("v1", {"id": "f1", "status": "CONFIRMED", "reason": "reproduced"},
                      {"id": "f2", "status": "PLAUSIBLE", "reason": "could not rule it out"})
        self.verifier("v2", {"id": "f3", "status": "REFUTED", "reason": "already handled in deliver()"})
        record = self.aggregate()
        self.assertEqual((record["completeness"], record["verdict"]), ("complete", "BLOCK"))
        self.assertEqual([b["title"] for b in record["blockers"]], ["confirmed"])
        self.assertEqual([(c["title"], c["reportedSeverity"], c["verification"]) for c in record["concerns"]],
                         [("plausible", "BLOCK", "PLAUSIBLE")])
        self.assertEqual([r["title"] for r in record["refuted"]], ["refuted"])
        self.assertEqual(len(record["nits"]), 1)
        records.load_review(self.dir / "review_1.json", required_lenses=LENSES)
        (self.verifiers / "results" / "v1.json").write_text(json.dumps([
            {"id": "f1", "status": "REFUTED", "reason": "not reachable"},
            {"id": "f2", "status": "PLAUSIBLE", "reason": "could not rule it out"}]))
        self.assertEqual(self.aggregate()["verdict"], "CONCERNS")
        self.lens("correctness")
        self.lens("security")
        self.assertEqual(self.aggregate()["verdict"], "PASS")

    def test_malformed_and_duplicate_results_are_rejected_by_task(self):
        self.lens("correctness", raw='{"verdict": "PASS", "findings": [{"severity": "SEVERE", "file": "a"}]}')
        self.lens("security", finding("CONCERN", 2))
        self.verifier("v1", {"id": "f1", "status": "CONFIRMED", "reason": "yes"})
        self.verifier("v2", {"id": "f1", "status": "REFUTED", "reason": "no"}, {"id": "f9", "status": "CONFIRMED",
                                                                              "reason": "x"})
        record = self.aggregate()
        self.assertEqual(record["completeness"], "incomplete")
        self.assertEqual(record["missing_lenses"], ["correctness"])
        self.assertIn("findings[0].severity must be BLOCK, CONCERN, or NIT", record["invalid_results"]["correctness"])
        verifier_problems = " ".join(record["invalid_results"]["v2"])
        self.assertIn("entry 1 names no finding id this panel produced", verifier_problems)
        self.assertIn("f1: conflicting verdicts from more than one verifier", verifier_problems)

    def test_pending_reruns_only_missing_invalid_or_stale_tasks(self):
        self.lens("correctness")
        self.lens("security", raw="the agent crashed")
        (self.lenses / "prompts" / "tests.md").write_text("You are the tests lens.\n")
        self.aggregate(expected=("correctness", "security", "tests"))
        pending = json.loads(self.run_script("pending", str(self.lenses), "--expected",
                                             "correctness,security,tests").stdout)
        self.assertEqual([(p["task"], p["why"]) for p in pending],
                         [("security", "unparseable result"), ("tests", "no result")])
        self.lens("security")
        self.lens("tests")
        self.assertEqual(json.loads(self.run_script("pending", str(self.lenses), "--expected",
                                                    "correctness,security,tests").stdout), [])
        self.aggregate(expected=("correctness", "security", "tests"))
        (self.lenses / "prompts" / "correctness.md").write_text("You are the correctness lens, with a new diff.\n")
        pending = json.loads(self.run_script("pending", str(self.lenses), "--expected",
                                             "correctness,security,tests").stdout)
        self.assertEqual(pending, [{"task": "correctness", "why": "the prompt changed since this result was recorded"}])

    def test_render_transcribes_the_record(self):
        self.lens("correctness", finding("CONCERN", 4, "retries are unbounded"))
        self.lens("security")
        self.verifier("v1", {"id": "f1", "status": "CONFIRMED", "reason": "seen"})
        self.aggregate()
        rendered = self.run_script("render", str(self.dir / "review_1.json")).stdout
        self.assertIn("Verdict: CONCERNS\nCompleteness: complete\nPanel: correctness, security", rendered)
        self.assertIn("[correctness] webhook.py:4 - retries are unbounded (CONFIRMED)", rendered)


if __name__ == "__main__":
    unittest.main()
