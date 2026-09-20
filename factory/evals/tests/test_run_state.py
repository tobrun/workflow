"""Unit tests for factory/skills/run/scripts/run-state.py.

Run from the repo root: python3 -m unittest discover -s factory/evals/tests -t .
Each test runs the script as a subprocess against a scratch .dev/ directory,
so it proves the real CLI contract the run skill depends on.
"""

from __future__ import annotations

import json
import os
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


SPEC_NUMBERED_PREAMBLE = """# Fixture plan

7. A numbered line before any section
   tests: [unit] preamble -> ignored

## Change plan

1. Change set one
   a. `a.py` - does a thing
   tests: [unit] a -> b; [unit] c -> d

2. Change set two
   a. `b.py` - does another thing
   tests: [unit] e -> f
"""


SPEC_CITING_THE_CHARACTER = """# Fixture plan

## Research

D-example: An example decision?
  ✓ do it - the state records every `⊘` line at handoff
  ⊘ not doing - reason one

## Scope

### Non-goals

- ⊘ something else - reason two

## Change plan

1. Change set one
   a. `a.py` - does a thing
   tests: [unit] a -> b
"""


def run_state(
    cwd: Path, *args: str, stdin: str | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        check=False,
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
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

    def handoff_state(self) -> dict:
        """Run a successful handoff on the fixture plan and return the state it wrote."""
        result = run_state(self.cwd, "handoff", "fixture-plan", "--branch", "factory/fixture-plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((self.plan_dir / "factory-run.json").read_text())

    def test_init_twice_exits_1_naming_existing_state(self) -> None:
        self.init_plan()
        result = run_state(self.cwd, "init", "fixture-plan", "--request", "do it", "--base", "main")
        self.assertEqual(result.returncode, 1)
        self.assertIn("already exists", result.stdout)

    def test_handoff_records_scenarios_and_not_doing_lines(self) -> None:
        self.init_plan()
        self.write_spec()
        state = self.handoff_state()
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

    def test_check_result_invalid_json_is_a_failed_attempt(self) -> None:
        path = self.cwd / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        result = run_state(self.cwd, "check-result", str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn("unparseable", result.stdout)
        self.assertTrue(result.stdout.startswith("failed:"), result.stdout)

    def test_check_result_stopped_exits_2_with_kind(self) -> None:
        path = self.write_result({"status": "stopped", "stop": {"kind": "secret.found", "action": "purge it"}})
        result = run_state(self.cwd, "check-result", str(path))
        self.assertEqual(result.returncode, 2)
        self.assertIn("secret.found", result.stdout)

    def test_check_result_missing_file_is_a_failed_attempt(self) -> None:
        result = run_state(self.cwd, "check-result", str(self.cwd / "nope.json"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no result file", result.stdout)

    def test_check_result_unknown_extra_key_exits_0(self) -> None:
        path = self.write_result({"status": "done", "totally_unknown_field": 42})
        result = run_state(self.cwd, "check-result", str(path))
        self.assertEqual(result.returncode, 0)

    def test_check_result_unrecognized_status_exits_1(self) -> None:
        path = self.write_result({"status": "in-progress"})
        result = run_state(self.cwd, "check-result", str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn("unrecognized status", result.stdout)

    def test_init_creates_the_whole_dev_tree(self) -> None:
        with tempfile.TemporaryDirectory() as bare:
            cwd = Path(bare)
            result = run_state(cwd, "init", "fresh-plan", "--request", "do it", "--base", "main")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((cwd / ".dev" / "fresh-plan" / "factory-run.json").is_file())

    def test_state_file_is_written_as_indented_json(self) -> None:
        self.init_plan()
        text = (self.plan_dir / "factory-run.json").read_text(encoding="utf-8")
        self.assertIn('\n  "plan": "fixture-plan"', text)
        self.assertIn('\n    "scope": []', text)
        self.assertTrue(text.endswith("}\n"))

    def test_handoff_ignores_numbered_lines_outside_the_change_plan(self) -> None:
        self.init_plan()
        self.write_spec(SPEC_NUMBERED_PREAMBLE)
        state = self.handoff_state()
        self.assertEqual(sorted(state["scenario_texts"]), ["1", "2"])
        self.assertEqual(sum(len(v) for v in state["scenario_texts"].values()), 3)

    def test_handoff_without_a_spec_exits_1(self) -> None:
        self.init_plan()
        result = run_state(self.cwd, "handoff", "fixture-plan", "--branch", "factory/fixture-plan")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no spec.md", result.stdout)

    def test_handoff_without_dirty_flag_records_git_status_paths(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.cwd, check=True, capture_output=True)
        (self.cwd / "foo.py").write_text("x = 1\n", encoding="utf-8")
        self.init_plan()
        self.write_spec()
        state = self.handoff_state()
        self.assertEqual(sorted(state["dirty_files"]), [".dev/", "foo.py"])

    def test_handoff_records_no_dirty_files_when_git_fails(self) -> None:
        bin_dir = self.cwd / "fakebin"
        bin_dir.mkdir()
        fake_git = bin_dir / "git"
        fake_git.write_text('#!/bin/sh\necho " M fake.py"\nexit 128\n', encoding="utf-8")
        fake_git.chmod(0o755)
        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        self.init_plan()
        self.write_spec()
        result = run_state(
            self.cwd, "handoff", "fixture-plan", "--branch", "factory/fixture-plan", env=env
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((self.plan_dir / "factory-run.json").read_text())
        self.assertEqual(state["dirty_files"], [])

    def test_record_rejects_malformed_stdin_json(self) -> None:
        self.init_plan()
        before = (self.plan_dir / "factory-run.json").read_text()
        result = run_state(self.cwd, "record", "fixture-plan", stdin="{not json")
        self.assertEqual(result.returncode, 3)
        self.assertIn("invalid JSON", result.stdout)
        self.assertEqual(before, (self.plan_dir / "factory-run.json").read_text())

    def test_check_result_stopped_with_a_string_stop_still_exits_2(self) -> None:
        path = self.write_result({"status": "stopped", "stop": "secret.found"})
        result = run_state(self.cwd, "check-result", str(path))
        self.assertEqual(result.returncode, 2)
        self.assertIn("secret.found", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_check_result_stopped_without_a_stop_object_still_exits_2(self) -> None:
        path = self.write_result({"status": "stopped", "stop": None, "reason": "destructive"})
        result = run_state(self.cwd, "check-result", str(path))
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)

    def test_attempt_records_a_launch_then_its_result(self) -> None:
        self.init_plan()
        launched = run_state(self.cwd, "attempt", "fixture-plan", "build")
        self.assertEqual(launched.returncode, 0, launched.stderr)
        state = json.loads((self.plan_dir / "factory-run.json").read_text())
        self.assertEqual(state["phases"]["build"], [{"status": "launched", "result": None}])
        path = self.write_result({"schema": "factory.result/1", "status": "done"})
        closed = run_state(
            self.cwd, "attempt", "fixture-plan", "build", "--status", "done", "--result", str(path)
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)
        state = json.loads((self.plan_dir / "factory-run.json").read_text())
        self.assertEqual(state["phases"]["build"][0]["status"], "done")
        self.assertEqual(state["phases"]["build"][0]["result"]["status"], "done")
        shown = run_state(self.cwd, "show", "fixture-plan")
        self.assertIn("build attempt 1: done", shown.stdout)

    def test_attempt_without_a_launch_to_close_exits_3(self) -> None:
        self.init_plan()
        result = run_state(self.cwd, "attempt", "fixture-plan", "build", "--status", "failed")
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("Traceback", result.stderr)

    def test_a_line_citing_the_not_doing_character_is_not_an_entry(self) -> None:
        self.init_plan()
        self.write_spec(SPEC_CITING_THE_CHARACTER)
        state = self.handoff_state()
        self.assertEqual(len(state["not_doing_lines"]), 2)
        reworded = SPEC_CITING_THE_CHARACTER.replace(
            "the state records every `⊘` line at handoff", "the state records the `⊘` lines"
        )
        self.write_spec(reworded)
        result = run_state(self.cwd, "diff-spec", "fixture-plan")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_diff_spec_without_a_state_file_exits_3(self) -> None:
        self.write_spec()
        result = run_state(self.cwd, "diff-spec", "fixture-plan")
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("Traceback", result.stderr)

    def test_diff_spec_with_a_truncated_state_file_exits_3(self) -> None:
        self.init_plan()
        self.write_spec()
        (self.plan_dir / "factory-run.json").write_text('{"plan": "fix', encoding="utf-8")
        result = run_state(self.cwd, "diff-spec", "fixture-plan")
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("Traceback", result.stderr)

    def test_diff_spec_without_a_spec_exits_3(self) -> None:
        self.init_plan()
        result = run_state(self.cwd, "diff-spec", "fixture-plan")
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("Traceback", result.stderr)

    def test_record_with_a_corrupt_state_file_exits_3(self) -> None:
        self.init_plan()
        (self.plan_dir / "factory-run.json").write_text("{", encoding="utf-8")
        result = run_state(
            self.cwd,
            "record",
            "fixture-plan",
            stdin=json.dumps({"phase": "build", "attempt": 1, "action": "advance"}),
        )
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("Traceback", result.stderr)

    def test_show_with_a_missing_state_file_exits_3(self) -> None:
        result = run_state(self.cwd, "show", "fixture-plan")
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("Traceback", result.stderr)

    def test_handoff_with_a_corrupt_state_file_exits_3(self) -> None:
        self.init_plan()
        self.write_spec()
        (self.plan_dir / "factory-run.json").write_text('{"plan": "fix', encoding="utf-8")
        result = run_state(self.cwd, "handoff", "fixture-plan", "--branch", "factory/fixture-plan")
        self.assertEqual(result.returncode, 3)
        self.assertIn("unusable state file", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_handoff_with_a_non_object_state_file_exits_3(self) -> None:
        self.init_plan()
        self.write_spec()
        (self.plan_dir / "factory-run.json").write_text("[]", encoding="utf-8")
        result = run_state(self.cwd, "handoff", "fixture-plan", "--branch", "factory/fixture-plan")
        self.assertEqual(result.returncode, 3)
        self.assertIn("not a JSON object", result.stdout)

    def test_record_rejects_stdin_json_that_is_not_an_object(self) -> None:
        self.init_plan()
        before = (self.plan_dir / "factory-run.json").read_text()
        result = run_state(self.cwd, "record", "fixture-plan", stdin=json.dumps([{"action": "advance"}]))
        self.assertEqual(result.returncode, 3)
        self.assertIn("not a decision object", result.stdout)
        self.assertEqual(before, (self.plan_dir / "factory-run.json").read_text())

    def test_check_result_with_a_json_array_is_a_failed_attempt(self) -> None:
        path = self.cwd / "array.json"
        path.write_text(json.dumps([{"status": "done"}]), encoding="utf-8")
        result = run_state(self.cwd, "check-result", str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a JSON object", result.stdout)

    def test_check_result_stopped_without_a_kind_prints_an_empty_kind(self) -> None:
        path = self.write_result({"status": "stopped", "stop": ["secret.found"]})
        result = run_state(self.cwd, "check-result", str(path))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "stopped: \n")

    def test_attempt_with_a_corrupt_state_file_exits_3(self) -> None:
        self.init_plan()
        (self.plan_dir / "factory-run.json").write_text('{"plan": "fix', encoding="utf-8")
        result = run_state(self.cwd, "attempt", "fixture-plan", "build")
        self.assertEqual(result.returncode, 3)
        self.assertIn("unusable state file", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_attempt_with_a_non_object_state_file_exits_3(self) -> None:
        self.init_plan()
        (self.plan_dir / "factory-run.json").write_text('"not a state"', encoding="utf-8")
        result = run_state(self.cwd, "attempt", "fixture-plan", "build", "--status", "done")
        self.assertEqual(result.returncode, 3)
        self.assertIn("not a JSON object", result.stdout)

    def test_check_result_on_a_directory_is_a_failed_attempt(self) -> None:
        target = self.cwd / "results"
        target.mkdir()
        result = run_state(self.cwd, "check-result", str(target))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no result file", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_attempt_with_an_unknown_phase_exits_3(self) -> None:
        self.init_plan()
        result = run_state(self.cwd, "attempt", "fixture-plan", "deploy")
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("Traceback", result.stderr)

    def test_attempt_with_an_unknown_status_exits_3(self) -> None:
        self.init_plan()
        result = run_state(self.cwd, "attempt", "fixture-plan", "build", "--status", "weird")
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("Traceback", result.stderr)

    def test_an_unknown_subcommand_exits_3(self) -> None:
        result = run_state(self.cwd, "frobnicate", "fixture-plan")
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("Traceback", result.stderr)

    def write_state(self, state: dict) -> None:
        (self.plan_dir / "factory-run.json").write_text(json.dumps(state), encoding="utf-8")

    def test_show_with_a_list_phases_state_exits_3(self) -> None:
        self.init_plan()
        state = json.loads((self.plan_dir / "factory-run.json").read_text())
        state["phases"] = []
        self.write_state(state)
        result = run_state(self.cwd, "show", "fixture-plan")
        self.assertEqual(result.returncode, 3)
        self.assertIn("unusable state file", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_attempt_with_a_null_phases_state_exits_3(self) -> None:
        self.init_plan()
        state = json.loads((self.plan_dir / "factory-run.json").read_text())
        state["phases"] = None
        self.write_state(state)
        result = run_state(self.cwd, "attempt", "fixture-plan", "build")
        self.assertEqual(result.returncode, 3)
        self.assertIn("unusable state file", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_diff_spec_with_a_null_scenario_texts_state_exits_3_not_1(self) -> None:
        self.init_plan()
        self.write_spec()
        state = json.loads((self.plan_dir / "factory-run.json").read_text())
        state["scenario_texts"] = None
        self.write_state(state)
        result = run_state(self.cwd, "diff-spec", "fixture-plan")
        self.assertEqual(result.returncode, 3)
        self.assertIn("unusable state file", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_attempt_with_a_phase_that_is_not_a_list_exits_3(self) -> None:
        self.init_plan()
        state = json.loads((self.plan_dir / "factory-run.json").read_text())
        state["phases"]["build"] = {}
        self.write_state(state)
        result = run_state(self.cwd, "attempt", "fixture-plan", "build")
        self.assertEqual(result.returncode, 3)
        self.assertIn("not a list of attempts", result.stdout)
        self.assertNotIn("Traceback", result.stderr)

    def test_show_with_a_non_object_attempt_entry_exits_3(self) -> None:
        self.init_plan()
        state = json.loads((self.plan_dir / "factory-run.json").read_text())
        state["phases"]["build"] = ["done"]
        self.write_state(state)
        result = run_state(self.cwd, "show", "fixture-plan")
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("Traceback", result.stderr)

    def test_check_result_failed_status_with_a_stop_kind_still_exits_2(self) -> None:
        path = self.write_result({"status": "failed", "stop": {"kind": "secret.found"}})
        result = run_state(self.cwd, "check-result", str(path))
        self.assertEqual(result.returncode, 2)
        self.assertIn("secret.found", result.stdout)

    def test_check_result_done_status_with_a_stop_kind_still_exits_2(self) -> None:
        path = self.write_result({"status": "done", "stop": {"kind": "action.destructive"}})
        result = run_state(self.cwd, "check-result", str(path))
        self.assertEqual(result.returncode, 2)
        self.assertIn("action.destructive", result.stdout)

    def test_check_result_done_with_an_empty_stop_object_exits_0(self) -> None:
        path = self.write_result({"status": "done", "stop": {}})
        result = run_state(self.cwd, "check-result", str(path))
        self.assertEqual(result.returncode, 0)

    def test_attempt_records_a_second_attempt_on_the_same_phase(self) -> None:
        self.init_plan()
        run_state(self.cwd, "attempt", "fixture-plan", "build")
        first = self.write_result({"status": "failed", "reason": "flaky"})
        run_state(
            self.cwd, "attempt", "fixture-plan", "build", "--status", "failed", "--result", str(first)
        )
        relaunched = run_state(self.cwd, "attempt", "fixture-plan", "build")
        self.assertEqual(relaunched.returncode, 0, relaunched.stderr)
        self.assertIn("build attempt 2: launched", relaunched.stdout)
        second = self.cwd / "result-2.json"
        second.write_text(json.dumps({"status": "done"}), encoding="utf-8")
        closed = run_state(
            self.cwd, "attempt", "fixture-plan", "build", "--status", "done", "--result", str(second)
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertIn("build attempt 2: done", closed.stdout)
        attempts = json.loads((self.plan_dir / "factory-run.json").read_text())["phases"]["build"]
        self.assertEqual([a["status"] for a in attempts], ["failed", "done"])
        self.assertEqual(attempts[0]["result"]["reason"], "flaky")
        self.assertEqual(attempts[1]["result"]["status"], "done")

    def test_attempt_refuses_to_reclose_a_closed_attempt(self) -> None:
        self.init_plan()
        run_state(self.cwd, "attempt", "fixture-plan", "build")
        path = self.write_result({"status": "failed", "reason": "flaky"})
        run_state(
            self.cwd, "attempt", "fixture-plan", "build", "--status", "failed", "--result", str(path)
        )
        before = (self.plan_dir / "factory-run.json").read_text()
        again = run_state(self.cwd, "attempt", "fixture-plan", "build", "--status", "done")
        self.assertEqual(again.returncode, 3)
        self.assertIn("already closed", again.stdout)
        self.assertEqual(before, (self.plan_dir / "factory-run.json").read_text())

    def test_attempt_with_a_missing_result_file_records_a_failed_attempt(self) -> None:
        self.init_plan()
        run_state(self.cwd, "attempt", "fixture-plan", "build")
        closed = run_state(
            self.cwd,
            "attempt",
            "fixture-plan",
            "build",
            "--status",
            "done",
            "--result",
            str(self.cwd / "nope.json"),
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertIn("no result file", closed.stdout)
        attempt = json.loads((self.plan_dir / "factory-run.json").read_text())["phases"]["build"][0]
        self.assertEqual(attempt["status"], "failed")
        self.assertEqual(attempt["result"]["status"], "failed")
        self.assertIn("no result file", attempt["result"]["reason"])

    def test_attempt_with_a_json_array_result_file_records_a_failed_attempt(self) -> None:
        self.init_plan()
        run_state(self.cwd, "attempt", "fixture-plan", "build")
        path = self.cwd / "array.json"
        path.write_text(json.dumps([{"status": "done"}]), encoding="utf-8")
        closed = run_state(
            self.cwd, "attempt", "fixture-plan", "build", "--status", "done", "--result", str(path)
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)
        attempt = json.loads((self.plan_dir / "factory-run.json").read_text())["phases"]["build"][0]
        self.assertEqual(attempt["status"], "failed")
        self.assertIn("not a JSON object", attempt["result"]["reason"])

    def test_show_exits_0(self) -> None:
        self.init_plan()
        result = run_state(self.cwd, "show", "fixture-plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scope: no attempts", result.stdout)


if __name__ == "__main__":
    unittest.main()
