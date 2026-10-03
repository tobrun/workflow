"""Change set 3f and 4: a stub pipeline driving run-state.py and factory-config.py.

Each stub in factory/evals/stubs/ is a script taking the result path as argv[1].
These tests prove the bookkeeping only - attempts, timestamps, terminality, budgets,
drift - never the orchestrator's prose loop, which only a paid per-host run proves
(D-loop-driver).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
STUBS = REPO_ROOT / "factory" / "evals" / "stubs"
RUN_STATE = REPO_ROOT / "factory" / "skills" / "run" / "scripts" / "run-state.py"
FACTORY_CONFIG = REPO_ROOT / "factory" / "scripts" / "factory-config.py"


def sh(cwd: Path, *argv: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, input=stdin, capture_output=True, text=True, check=False)


class StubPipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)
        (self.cwd / ".dev" / "p").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def rs(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        return sh(self.cwd, sys.executable, str(RUN_STATE), *args, stdin=stdin)

    def state(self) -> dict:
        return json.loads((self.cwd / ".dev" / "p" / "factory-run.json").read_text())

    def result_path(self, phase: str, attempt: int = 1) -> Path:
        return self.cwd / ".dev" / "p" / "results" / f"{phase}-{attempt}.json"

    def init(self, phases: str, *extra: str) -> None:
        result = self.rs("init", "p", "--request", "r", "--base", "main", "--phases", phases, *extra)
        self.assertEqual(result.returncode, 0, result.stdout)

    def run_stub(self, stub: str, phase: str, attempt: int = 1) -> Path:
        path = self.result_path(phase, attempt)
        sh(self.cwd, sys.executable, str(STUBS / stub), str(path))
        return path

    def launch(self, phase: str, type_: str = "stub") -> None:
        result = self.rs("attempt", "p", phase, "--type", type_, "--skill", f"/stubs/{phase}")
        self.assertEqual(result.returncode, 0, result.stdout)

    def advance(self, phase: str) -> None:
        payload = json.dumps({"action": "advance", "phase": phase, "attempt": 1})
        self.assertEqual(self.rs("record", "p", stdin=payload).returncode, 0)

    def drive(self, phase: str, stub: str = "stub-done.py") -> Path:
        """One launched phase, in the orchestrator's order: launch, run, check, close."""
        self.launch(phase)
        path = self.run_stub(stub, phase)
        self.assertEqual(self.rs("check-result", str(path)).returncode, 0)
        closed = self.rs("attempt", "p", phase, "--status", "done", "--result", str(path))
        self.assertEqual(closed.returncode, 0, closed.stdout)
        return path

    def test_each_conforming_stub_writes_a_file_check_result_accepts(self) -> None:
        self.assertEqual(self.rs("check-result", str(self.run_stub("stub-done.py", "a"))).returncode, 0)
        failed = self.rs("check-result", str(self.run_stub("stub-failed.py", "b")))
        self.assertEqual(failed.returncode, 1)
        self.assertIn("stub failure", failed.stdout)

    def test_the_silent_stub_writes_nothing(self) -> None:
        path = self.run_stub("stub-silent.py", "a")
        self.assertFalse(path.exists())
        result = self.rs("check-result", str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no result file", result.stdout)

    def test_three_stubs_are_recorded_in_order_with_type_path_and_timestamps(self) -> None:
        self.init("one,two,three")
        for phase in ("one", "two", "three"):
            self.drive(phase)
        self.advance("three")
        state = self.state()
        self.assertEqual(list(state["phases"]), ["one", "two", "three"])
        for phase in ("one", "two", "three"):
            (attempt,) = state["phases"][phase]
            self.assertEqual(attempt["status"], "done")
            self.assertEqual((attempt["type"], attempt["skill"]), ("stub", f"/stubs/{phase}"))
            self.assertIn("opened", attempt)
            self.assertIn("closed", attempt)
        self.assertIn("finished: yes", self.rs("show", "p").stdout)

    def test_a_silent_stub_closes_its_attempt_failed_with_no_result_file(self) -> None:
        self.init("one")
        self.launch("one")
        path = self.run_stub("stub-silent.py", "one")
        self.assertEqual(self.rs("check-result", str(path)).returncode, 1)
        self.assertEqual(self.rs("attempt", "p", "one", "--status", "failed").returncode, 0)
        record = self.state()["phases"]["one"][0]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["result"]["reason"], "no result file")

    def test_an_attempt_left_launched_is_closed_by_the_orchestrator_and_the_next_is_numbered_n_plus_1(
        self,
    ) -> None:
        self.init("one")
        self.launch("one")
        self.assertEqual(self.state()["phases"]["one"][0]["status"], "launched")
        self.assertEqual(self.rs("attempt", "p", "one", "--status", "failed").returncode, 0)
        self.assertEqual(self.state()["phases"]["one"][0]["result"]["reason"], "no result file")
        again = self.rs("attempt", "p", "one")
        self.assertIn("one attempt 2: launched", again.stdout)

    def test_an_interactive_first_phase_is_bracketed_like_a_launched_one(self) -> None:
        self.init("interview,build")
        self.launch("interview", "interview")
        path = self.run_stub("stub-done.py", "interview")
        closed = self.rs("attempt", "p", "interview", "--status", "done", "--result", str(path))
        self.assertEqual(closed.returncode, 0)
        record = self.state()["phases"]["interview"][0]
        self.assertEqual((record["type"], record["skill"]), ("interview", "/stubs/interview"))
        self.assertIn("opened", record)
        self.assertIn("closed", record)
        self.assertIn("interview attempt 1: done", self.rs("show", "p").stdout)

    def test_a_ceiling_is_reported_exhausted_with_every_phases_attempts_never_refused(self) -> None:
        self.init("one,two", "--ceiling", "2")
        for phase in ("one", "two", "two"):
            self.assertEqual(self.rs("attempt", "p", phase).returncode, 0)
        shown = self.rs("show", "p").stdout
        self.assertIn("total: 3/2 attempts (exhausted)", shown)
        self.assertIn("one: 1/3 attempts", shown)
        self.assertIn("two: 2/3 attempts", shown)

    def test_the_resolved_pipeline_from_factory_config_is_sealed_at_handoff(self) -> None:
        shown = sh(self.cwd, sys.executable, str(FACTORY_CONFIG), "--repo-root", str(self.cwd),
                   "show", "--resolved", "--json")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        (self.cwd / "pipeline.json").write_text(shown.stdout)
        self.init("scope,scope-review,build,ship")
        handoff = self.rs("handoff", "p", "--branch", "b", "--pipeline", "pipeline.json")
        self.assertEqual(handoff.returncode, 0, handoff.stdout)
        sealed = [phase["id"] for phase in self.state()["pipeline"]]
        expected = [phase["id"] for phase in json.loads(shown.stdout)["phases"]]
        self.assertEqual(sealed, expected)


if __name__ == "__main__":
    unittest.main()
