"""Unit tests for factory/skills/run/scripts/run-state.py.

Run from the repo root: python3 -m unittest discover -s factory/evals/tests -t .
Each test runs the script as a subprocess against a scratch .dev/ directory,
so it proves the real CLI contract the run skill depends on.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "factory" / "skills" / "run" / "scripts" / "run-state.py"

SPEC_TWO_CHANGE_SETS = """# Fixture plan

## Research

D-example: An example decision?
  ✓ do it - reason
  ✗ don't - reason
  ⊘ not doing - reason one

## Scope

### Non-goals

- ⊘ something else - reason two

## Change plan

1. Change set one
   a. `a.py` - does a thing
   tests: [unit] a -> b; [unit] c -> d

2. Change set two
   a. `b.py` - does another thing
   tests: [unit] e -> f
"""


def run_state(cwd: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
    )


class RunStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)
        self.plan_dir = self.cwd / ".dev" / "fixture-plan"
        self.plan_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_spec(self, text: str = SPEC_TWO_CHANGE_SETS) -> None:
        (self.plan_dir / "spec.md").write_text(text, encoding="utf-8")

    def init_plan(self) -> None:
        result = run_state(self.cwd, "init", "fixture-plan", "--request", "do it", "--base", "main")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_init_twice_exits_1_naming_existing_state(self) -> None:
        self.init_plan()
        result = run_state(self.cwd, "init", "fixture-plan", "--request", "do it", "--base", "main")
        self.assertEqual(result.returncode, 1)
        self.assertIn("already exists", result.stdout)

    def test_handoff_records_scenarios_and_not_doing_lines(self) -> None:
        self.init_plan()
        self.write_spec()
        result = run_state(self.cwd, "handoff", "fixture-plan", "--branch", "factory/fixture-plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((self.plan_dir / "factory-run.json").read_text())
        self.assertIsNotNone(state["spec_sha256"])
        scenario_texts = state["scenario_texts"]
        self.assertEqual(sorted(scenario_texts), ["1", "2"])
        total = sum(len(v) for v in scenario_texts.values())
        self.assertEqual(total, 3)
        self.assertEqual(len(state["not_doing_lines"]), 2)

    def test_diff_spec_after_reworded_scenario_exits_1(self) -> None:
        self.init_plan()
        self.write_spec()
        run_state(self.cwd, "handoff", "fixture-plan", "--branch", "factory/fixture-plan")
        reworded = SPEC_TWO_CHANGE_SETS.replace(
            "tests: [unit] a -> b; [unit] c -> d",
            "tests: [unit] a -> z; [unit] c -> d",
        )
        self.write_spec(reworded)
        result = run_state(self.cwd, "diff-spec", "fixture-plan")
        self.assertEqual(result.returncode, 1)
        self.assertIn("change set 1", result.stdout)
        self.assertIn("a -> b", result.stdout)
        self.assertIn("a -> z", result.stdout)

    def test_diff_spec_unchanged_exits_0(self) -> None:
        self.init_plan()
        self.write_spec()
        run_state(self.cwd, "handoff", "fixture-plan", "--branch", "factory/fixture-plan")
        result = run_state(self.cwd, "diff-spec", "fixture-plan")
        self.assertEqual(result.returncode, 0)

    def test_diff_spec_after_dropped_scenario_and_not_doing_line(self) -> None:
        self.init_plan()
        self.write_spec()
        run_state(self.cwd, "handoff", "fixture-plan", "--branch", "factory/fixture-plan")
        dropped = SPEC_TWO_CHANGE_SETS.replace(
            "tests: [unit] a -> b; [unit] c -> d", "tests: [unit] c -> d"
        ).replace("- ⊘ something else - reason two\n", "")
        self.write_spec(dropped)
        result = run_state(self.cwd, "diff-spec", "fixture-plan")
        self.assertEqual(result.returncode, 1)
        self.assertIn("change set 1", result.stdout)
        self.assertIn("⊘ line dropped", result.stdout)

    def test_record_rejects_action_outside_the_four(self) -> None:
        self.init_plan()
        before = (self.plan_dir / "factory-run.json").read_text()
        result = run_state(
            self.cwd,
            "record",
            "fixture-plan",
            stdin=json.dumps({"phase": "build", "attempt": 1, "action": "park"}),
        )
        self.assertEqual(result.returncode, 1)
        after = (self.plan_dir / "factory-run.json").read_text()
        self.assertEqual(before, after)

    def test_record_a_repair_then_show(self) -> None:
        self.init_plan()
        result = run_state(
            self.cwd,
            "record",
            "fixture-plan",
            stdin=json.dumps(
                {
                    "phase": "build",
                    "attempt": 1,
                    "action": "repair",
                    "rationale": "fixed a typo",
                    "evidence": ["ran tests"],
                    "repair": {"description": "fixed a typo", "files": ["a.py"]},
                }
            ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result = run_state(self.cwd, "show", "fixture-plan")
        self.assertIn("fixed a typo", result.stdout)

    def test_show_after_two_attempts_prints_two_rows(self) -> None:
        self.init_plan()
        state = json.loads((self.plan_dir / "factory-run.json").read_text())
        state["phases"]["build"] = [{"status": "failed"}, {"status": "done"}]
        (self.plan_dir / "factory-run.json").write_text(json.dumps(state))
        result = run_state(self.cwd, "show", "fixture-plan")
        self.assertIn("build attempt 1: failed", result.stdout)
        self.assertIn("build attempt 2: done", result.stdout)

    def write_result(self, payload: dict) -> Path:
        path = self.cwd / "result.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_check_result_done_exits_0(self) -> None:
        path = self.write_result({"schema": "factory.result/1", "phase": "build", "status": "done"})
        result = run_state(self.cwd, "check-result", str(path))
        self.assertEqual(result.returncode, 0)

    def test_check_result_failed_exits_1_with_reason(self) -> None:
        path = self.write_result({"status": "failed", "reason": "no result file"})
        result = run_state(self.cwd, "check-result", str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no result file", result.stdout)

    def test_check_result_invalid_json_exits_3(self) -> None:
        path = self.cwd / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        result = run_state(self.cwd, "check-result", str(path))
        self.assertEqual(result.returncode, 3)
        self.assertIn("unparseable", result.stdout)

    def test_check_result_stopped_exits_2_with_kind(self) -> None:
        path = self.write_result({"status": "stopped", "stop": {"kind": "secret.found", "action": "purge it"}})
        result = run_state(self.cwd, "check-result", str(path))
        self.assertEqual(result.returncode, 2)
        self.assertIn("secret.found", result.stdout)

    def test_check_result_missing_file_exits_3(self) -> None:
        result = run_state(self.cwd, "check-result", str(self.cwd / "nope.json"))
        self.assertEqual(result.returncode, 3)

    def test_check_result_unknown_extra_key_exits_0(self) -> None:
        path = self.write_result({"status": "done", "totally_unknown_field": 42})
        result = run_state(self.cwd, "check-result", str(path))
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
