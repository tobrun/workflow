"""Tests for workflow-log.py: records come from the plan's files and sync to a records repository.

Run from the repo root: python3 -m unittest discover -s dev/evals/tests -p 'test_workflow_log.py' -t .
Each test uses a scratch project repository, a local bare repository standing in for
the private records repository, a fake gh on PATH, and a fake Claude Code log tree.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "dev" / "scripts" / "workflow-log.py"
VERSION = json.loads((REPO_ROOT / "dev" / ".claude-plugin" / "plugin.json").read_text())["version"]

FAKE_GH = r'''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
if args[:2] == ["pr", "view"] and args[2] == "--json":
    print(json.dumps({"number": 7, "url": "https://github.com/tobrun/blog/pull/7"}))
elif args[:2] == ["pr", "view"]:
    print(json.dumps({"state": "MERGED", "createdAt": "2026-10-01T10:00:00Z", "mergedAt": "2026-10-01T16:00:00Z",
                      "body": "<!-- BEGIN DEV-EVIDENCE -->\n### ✅ Verified by CI at `abc`\n<!-- END DEV-EVIDENCE -->",
                      "commits": [{}, {}, {}], "reviews": [{"state": "CHANGES_REQUESTED"}, {"state": "APPROVED"}],
                      "files": [{"path": "src/cart.py"}], "baseRefName": "main", "mergeCommit": {"oid": "own"}}))
elif args[:1] == ["api"]:
    print("own\tfix: the PR's own squash merge")
    print("f1\tfix: rounding of the total")
    print("d1\tdocs: mention coupons")
else:
    sys.exit(1)
'''

SPEC = """# Coupons

Risk: medium - touches checkout

## Research

D-store: Where are codes kept? [impact: high]
  ✓ a table - one query (user, 2026-11-01)
  ✗ a config file - a redeploy per code

D-clock: Which clock decides expiry? [impact: medium]
  ✓ the server clock - one source of time (auto-applied at Confidence: 70%)
  ✗ the client clock - a user can set it back

D-cache: Cache the codes? [impact: low]
  ✓ no cache - small table (auto-applied at Confidence: 60%)
  ✗ an LRU cache - stale codes

## Scope

### Summary

Coupons take money off a total.

### Verification

- unit: yes - rules
- fuzz: no - no parser

## Change plan

1. Store
   a. `src/cart.py` - codes
   tests: [unit] valid code -> discount; [unit] expired -> refused; [e2e] checkout with code -> total shown
2. Page
   a. `src/page.py` - field
   tests: [property] any code -> never negative
"""

NOTES = """## Build run 2026-11-26

## Change set 1: Store
- Tests added: tests/test_cart.py::test_valid
- Deviations from spec: expired codes at midnight -> conservative choice made: refuse

## Change set 2: Page
- Tests added: tests/test_page.py::test_field

## Fixup: the field lost its value
- Found by: the wave gate
"""

PR = """## Summary

Coupons.

## Quality

| Kind | Result | Found | Fixed | Surviving |
| --- | --- | --- | --- | --- |
| unit | ran | 2 | 2 | 0 |
| coverage | ran | 1 | 0 | 1 |
| fuzz | skipped (no parser) | | | |

## Open calls

- Coverage under 80% on `src/page.py`.
"""


class WorkflowLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "blog"
        self.plan = self.project / ".dev" / "coupons"
        self.plan.mkdir(parents=True)
        self.git(self.project, "init", "-q", "-b", "main")
        self.git(self.project, "remote", "add", "origin", "git@github.com:tobrun/blog.git")
        (self.project / "README.md").write_text("blog\n")
        self.git(self.project, "add", "-A")
        self.git(self.project, "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "base")
        (self.plan / "spec.md").write_text(SPEC)
        (self.plan / "implementation-notes.md").write_text(NOTES)
        (self.plan / "test-evidence.json").write_text(json.dumps({"schema": "dev.test-evidence/1", "scenarios": [
            {"id": "CS1-S1", "receipt": "r.json"}, {"id": "CS1-S2", "receipt": "r.json"}, {"id": "CS1-S3"}, {"id": "CS2-S1"}]}))
        (self.plan / "pr.md").write_text(PR)
        (self.plan / "review_1.md").write_text("# Review\n\nVerdict: BLOCK\n")
        (self.plan / "review_2.md").write_text("# Review\n\nVerdict: PASS\n")
        self.records = self.tmp / "records.git"
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.records))
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(FAKE_GH)
        (bin_dir / "gh").chmod(0o755)
        self.logs = self.tmp / "claude"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CODEX_", "PI_"))}
        self.env = {**env, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "DEV_WORKFLOW_HOME": str(self.tmp / "state"),
                    "DEV_WORKFLOW_CLAUDE_LOGS": str(self.logs), "CLAUDECODE": "1"}

    def git(self, cwd: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    def log(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        result = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.project, capture_output=True, text=True,
                                env={**self.env, **env})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def queued(self) -> list[dict]:
        return [json.loads(line) for p in sorted((self.tmp / "state" / "pending").glob("*.jsonl"))
                for line in p.read_text().splitlines()]

    def session(self, *entries: tuple[str, str, int]) -> None:
        """A Claude Code session log of assistant turns: (timestamp, message id, output tokens)."""
        folder = self.logs / "-tmp-blog"
        folder.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"type": "assistant", "timestamp": ts, "cwd": str(self.project),
                             "message": {"id": mid, "model": "claude-opus-5-5",
                                         "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100,
                                                   "cache_read_input_tokens": 1000, "output_tokens": out}}})
                 for ts, mid, out in entries]
        (folder / "s.jsonl").write_text("\n".join(lines) + "\n")

    def phase(self, phase: str) -> dict:
        self.log("start", "--phase", phase, "--plan", str(self.plan))
        self.log("end", "--phase", phase, "--plan", str(self.plan))
        return self.queued()[-1]

    def test_a_scope_record_counts_what_the_spec_says(self) -> None:
        record = self.phase("scope")
        self.assertEqual(record["schema"], "dev.workflow-run/1")
        self.assertEqual((record["repo"], record["plan"], record["workflow_version"]), ("tobrun/blog", "coupons", VERSION))
        facts = record["facts"]
        self.assertEqual((facts["decisions"], facts["high_impact"], facts["settled_by_user"], facts["auto_applied"]), (3, 1, 1, 2))
        self.assertEqual(facts["confidences"], [70, 60])
        self.assertEqual(facts["scenarios"], {"unit": 2, "e2e": 1, "property": 1})
        self.assertEqual((facts["change_sets"], facts["kinds_planned"], facts["risk"]), (2, 1, "medium"))
        self.assertIsNotNone(record["duration_s"])

    def test_build_and_ship_records_read_the_notes_evidence_and_pr(self) -> None:
        build = self.phase("build")["facts"]
        self.assertEqual((build["change_sets_built"], build["deviations"], build["fixups"]), (2, 1, 1))
        self.assertEqual((build["scenarios_mapped"], build["scenarios_deferred"]), (4, 2))
        ship = self.phase("ship")["facts"]
        self.assertEqual(ship["kinds"]["coverage"], {"result": "ran", "found": 1, "fixed": 0, "surviving": 1})
        self.assertEqual(ship["kinds"]["fuzz"]["result"], "skipped")
        self.assertEqual((ship["found"], ship["surviving"], ship["open_calls"]), (3, 1, 1))
        self.assertEqual((ship["reviews"], ship["first_verdict"], ship["last_verdict"], ship["pr"]), (2, "BLOCK", "PASS", 7))

    def test_tokens_are_summed_from_the_session_log_inside_the_run_only(self) -> None:
        self.log("start", "--phase", "build", "--plan", str(self.plan))
        inside = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.session(("2020-01-01T00:00:00Z", "old", 999), (inside, "m1", 50), (inside, "m1", 50), (inside, "m2", 25))
        self.log("end", "--phase", "build", "--plan", str(self.plan))
        usage = self.queued()[-1]["usage"]
        self.assertEqual((usage["turns"], usage["output"], usage["cache_read"]), (2, 75, 2000))
        self.assertEqual(usage["models"], {"claude-opus-5-5": 2})

    def test_off_records_nothing(self) -> None:
        self.log("start", "--phase", "scope", "--plan", str(self.plan), DEV_WORKFLOW_LOG="off")
        self.log("end", "--phase", "scope", "--plan", str(self.plan), DEV_WORKFLOW_LOG="off")
        self.assertEqual(self.queued(), [])

    def test_a_missing_plan_still_records_and_never_fails(self) -> None:
        self.log("end", "--phase", "ship", "--plan", str(self.project / ".dev" / "nope"))
        record = self.queued()[-1]
        self.assertIsNone(record["duration_s"])

    def test_sync_sends_the_queue_harvests_outcomes_and_writes_the_report(self) -> None:
        self.log("setup", "--repo", str(self.records))
        self.phase("scope")
        self.phase("ship")
        result = self.log("sync")
        self.assertIn("synced 2 record(s) and 1 outcome(s)", result.stdout)
        self.assertEqual(self.queued(), [])
        clone = self.tmp / "clone"
        self.git(self.tmp, "clone", "-q", str(self.records), str(clone))
        runs = list((clone / "runs").rglob("*.jsonl"))
        self.assertEqual(len(runs), 1)
        self.assertEqual(len(runs[0].read_text().splitlines()), 2)
        outcome = json.loads((clone / "outcomes" / "tobrun__blog" / "7.json").read_text())
        self.assertEqual((outcome["state"], outcome["hours_to_merge"], outcome["changes_requested"]), ("MERGED", 6.0, 1))
        self.assertEqual((outcome["ci_verdict"], outcome["escaped"], outcome["final"]), ("verified", 1, True))
        report = (clone / "report.html").read_text()
        self.assertIn(f"<th>{VERSION}</th>", report)
        self.assertIn("Escaped defects per merged PR (matured)", report)
        self.assertIn("1 scope · 0 build · 1 ship", report)

    def test_a_second_sync_adds_only_new_records(self) -> None:
        self.log("setup", "--repo", str(self.records))
        self.phase("scope")
        self.log("sync")
        self.phase("build")
        self.log("sync")
        clone = self.tmp / "clone"
        self.git(self.tmp, "clone", "-q", str(self.records), str(clone))
        lines = [line for p in (clone / "runs").rglob("*.jsonl") for line in p.read_text().splitlines()]
        self.assertEqual([json.loads(line)["phase"] for line in lines], ["scope", "build"])

    def test_an_unreachable_repository_keeps_the_queue(self) -> None:
        self.log("setup", "--repo", str(self.tmp / "missing.git"))
        self.phase("scope")
        result = self.log("sync")
        self.assertIn("the queue waits for the next sync", result.stdout)
        self.assertEqual(len(self.queued()), 1)

    def test_without_a_repository_sync_says_how_to_set_one(self) -> None:
        self.assertIn("run `workflow-log.py setup --repo URL`", self.log("sync").stdout)


if __name__ == "__main__":
    unittest.main()
