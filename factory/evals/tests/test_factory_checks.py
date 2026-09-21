"""Unit tests for the factory-specific validate.sh checks (F02, F03).

Each scenario copies the repo tree to a temp directory, mutates the copy,
rebuilds the Codex distributions there (any mutation under factory/ makes
plugins/factory stale, so validate.sh would fail on [C01] instead of on the
check under test without a rebuild), then runs the copy's own
scripts/validate.sh - never the real repo's.

Running that gate costs about 7 seconds, and most of it is the copy's own
check_factory_script re-running the other 60 unit tests, which say nothing
about F02 or F03. The scenarios are independent processes in separate temp
directories, so SCENARIOS below names each distinct one once and setUpClass
runs them all concurrently; every test then asserts against its own finished
run. Two savings come for free: the scenarios that differ only in what they
assert about one clean run now share that single run, and no scenario is
executed twice. Ordering is not a factor - nothing is shared between runs but
the read-only source tree.

This file is excluded by name from check_factory_script's own discovery
(D-nested-validate): it runs validate.sh itself, so without the exclusion
the gate would re-enter itself once per nesting level and never terminate.
Run from the repo root: python3 -m unittest discover -s factory/evals/tests -t .
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".DS_Store"
)
# The gate is subprocess-bound, so threads are enough; the cap keeps a laptop
# from running more full validate.sh runs at once than it has cores to spend.
WORKERS = min(8, os.cpu_count() or 4)


def copy_repo() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="factory-checks-"))
    dest = tmp / "repo"
    shutil.copytree(REPO_ROOT, dest, ignore=IGNORE)
    return dest


def rebuild(copy_root: Path) -> None:
    subprocess.run(
        [sys.executable, "scripts/build_codex_plugin.py"],
        cwd=copy_root,
        check=True,
        capture_output=True,
        text=True,
    )


def run_validate(copy_root: Path) -> subprocess.CompletedProcess:
    full_env = dict(os.environ)
    full_env["VALIDATE_LOGS"] = str(copy_root / "validate-logs")
    return subprocess.run(
        ["bash", "scripts/validate.sh"],
        cwd=copy_root,
        check=False,
        capture_output=True,
        text=True,
        env=full_env,
    )


@dataclass(frozen=True)
class Outcome:
    """One finished validate.sh run, detached from its (deleted) temp copy."""

    returncode: int
    stdout: str
    stderr: str
    target: str | None
    script_log: str | None

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


def append(relative: str, text: str) -> Callable[[Path], str]:
    """A mutation that appends `text` to `relative` and names it as the target."""

    def mutate(copy_root: Path) -> str:
        with (copy_root / relative).open("a", encoding="utf-8") as handle:
            handle.write(text)
        return relative

    return mutate


def drop_one_close_marker(copy_root: Path) -> str:
    relative = "factory/skills/run/SKILL.md"
    target = copy_root / relative
    text = target.read_text(encoding="utf-8")
    if "<!-- /interactive-only -->" not in text:
        raise AssertionError("expected a close marker in run/SKILL.md")
    target.write_text(
        text.replace("<!-- /interactive-only -->", "", 1)
        + "\n\nAsk the user which one to pick.\n",
        encoding="utf-8",
    )
    return relative


def rename_result_literal(copy_root: Path) -> str:
    relative = "factory/skills/build/SKILL.md"
    target = copy_root / relative
    text = target.read_text(encoding="utf-8")
    target.write_text(
        text.replace("results/{phase}-{attempt}.json", "some-other-path.json"),
        encoding="utf-8",
    )
    return relative


def break_f03_self_test(copy_root: Path) -> str:
    relative = "scripts/validate.sh"
    target = copy_root / relative
    text = target.read_text(encoding="utf-8")
    broken = text.replace(
        r'ASK = (r"ask|prompt|poll',
        r'ASK = (r"askXXX|promptXXX|pollXXX',
    )
    if text == broken:
        raise AssertionError("expected the F03 pattern text to be present")
    target.write_text(broken, encoding="utf-8")
    return relative


PERSON_ROUTING_SHAPES = (
    "Surface it to the person and wait for the answer.",
    "Escalate the threshold to the requester.",
    "Hand it to the owner for a decision.",
    "Ask someone on the team which one to pick.",
    "Raising the gate is a human decision.",
    "Get approval before opening the PR.",
    "Check with them before proceeding.",
    "In the end it is their call.",
    "Seek sign-off from the maintainer.",
    "This seam is worth agreeing on with the user.",
)

BUILD_SKILL = "factory/skills/build/SKILL.md"
SHIP_SKILL = "factory/skills/ship/SKILL.md"
RUN_SKILL = "factory/skills/run/SKILL.md"

# name -> (mutation or None, rebuild the Codex copies before validating)
SCENARIOS: dict[str, tuple[Callable[[Path], str] | None, bool]] = {
    # One clean run, shared by every scenario that only asserts about a clean gate.
    "clean": (None, False),
    "person_routed": (append(BUILD_SKILL, "\n\nAsk the user which one to pick.\n"), True),
    "human_calls": (
        append(SHIP_SKILL, "\n\nRotation and history rewriting are both human calls.\n"),
        True,
    ),
    "question_left": (
        append(BUILD_SKILL, "\n\nThen any question left for the user - a blocked gate.\n"),
        True,
    ),
    "prose_about_people": (
        append(
            BUILD_SKILL,
            "\n\nA user-facing change in the user's repository still needs an e2e scenario.\n",
        ),
        True,
    ),
    "marker_in_run_skill": (
        append(
            RUN_SKILL,
            "\n\n<!-- interactive-only -->\nAsk the user which one to pick.\n"
            "<!-- /interactive-only -->\n",
        ),
        True,
    ),
    "marker_in_phase_copy": (
        append(
            BUILD_SKILL,
            "\n\n<!-- interactive-only -->\nAsk the user which one to pick.\n"
            "<!-- /interactive-only -->\n",
        ),
        True,
    ),
    "marker_unclosed": (
        append(RUN_SKILL, "\n\n<!-- interactive-only -->\nAsk the user which one to pick.\n"),
        True,
    ),
    "marker_close_dropped": (drop_one_close_marker, True),
    "marker_stray_close": (append(RUN_SKILL, "\n\n<!-- /interactive-only -->\n"), True),
    "marker_shares_line": (
        append(BUILD_SKILL, "\n\n<!-- interactive-only --> Ask the user which one to pick.\n"),
        True,
    ),
    "broken_self_test": (break_f03_self_test, False),
    "result_literal_renamed": (rename_result_literal, True),
    "phase_invocation": (append(BUILD_SKILL, "\n\nOn success, invoke $factory:ship.\n"), True),
    **{
        f"shape_{index}": (append(BUILD_SKILL, f"\n\n{shape}\n"), True)
        for index, shape in enumerate(PERSON_ROUTING_SHAPES)
    },
}


def run_scenario(name: str) -> Outcome:
    mutate, needs_rebuild = SCENARIOS[name]
    copy_root = copy_repo()
    try:
        target = mutate(copy_root) if mutate else None
        if needs_rebuild:
            rebuild(copy_root)
        result = run_validate(copy_root)
        log = copy_root / "validate-logs" / "factory-script-tests.log"
        return Outcome(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            target=target,
            script_log=log.read_text(encoding="utf-8") if log.is_file() else None,
        )
    finally:
        shutil.rmtree(copy_root.parent, ignore_errors=True)


_LOCK = threading.Lock()
_FUTURES: dict[str, Future] = {}


def scenario_outcome(name: str) -> Outcome:
    """The finished run for `name`, starting every scenario on first call.

    The futures live at module scope rather than in a class fixture on purpose.
    A shuffling runner (tools/harden/flaky.py) interleaves these tests with
    other classes, and unittest re-runs setUpClass every time execution crosses
    a class boundary - so a class fixture would re-launch the whole fan-out
    many times per pass. Module scope makes it once per process, whatever order
    the tests run in.
    """
    with _LOCK:
        if not _FUTURES:
            pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="gate")
            _FUTURES.update({name: pool.submit(run_scenario, name) for name in SCENARIOS})
            # Threads are released once every scenario is done; the pool is not
            # shut down here because the futures outlive this call.
            pool.shutdown(wait=False)
    return _FUTURES[name].result()


class FactoryChecksTest(unittest.TestCase):
    """The scenarios that run the full gate, each asserted against its own run."""

    def outcome(self, name: str) -> Outcome:
        return scenario_outcome(name)

    def assert_gate_fails(self, name: str, tag: str) -> Outcome:
        """The gate failed with `tag` at the scenario's own target, not on staleness."""
        outcome = self.outcome(name)
        self.assertEqual(outcome.returncode, 1, outcome.output)
        self.assertIn(tag, outcome.stdout)
        self.assertIsNotNone(outcome.target)
        self.assertIn(outcome.target, outcome.stdout)
        self.assertNotIn("[C01]", outcome.stdout)
        return outcome

    def test_unmutated_copy_passes_and_excludes_the_harness(self) -> None:
        outcome = self.outcome("clean")
        self.assertEqual(outcome.returncode, 0, outcome.output)
        if outcome.script_log is not None:
            self.assertIn("test_run_state", outcome.script_log)
            self.assertIn("test_stub_pipeline", outcome.script_log)
            self.assertIn("test_factory_config", outcome.script_log)
            self.assertNotIn("test_factory_checks", outcome.script_log)

    def test_decision_routed_to_a_person_fails_f03(self) -> None:
        self.assert_gate_fails("person_routed", "[F03]")

    def test_human_call_wording_fails_f03(self) -> None:
        """A phrasing the first F03 pattern missed: the plural defeated `human call`."""
        self.assert_gate_fails("human_calls", "[F03]")

    def test_question_left_for_the_user_fails_f03(self) -> None:
        self.assert_gate_fails("question_left", "[F03]")

    def test_prose_about_people_does_not_fire_f03(self) -> None:
        """The widened pattern must not fire on possessives or compounds."""
        outcome = self.outcome("prose_about_people")
        self.assertEqual(outcome.returncode, 0, outcome.output)

    def test_same_line_inside_interactive_only_passes(self) -> None:
        """run/SKILL.md is the one file whose pre-go lines may reach a person."""
        outcome = self.outcome("marker_in_run_skill")
        self.assertEqual(outcome.returncode, 0, outcome.output)

    def test_interactive_only_marker_in_a_phase_copy_fails_f03(self) -> None:
        """A phase copy has nothing to route, so it may not use the escape hatch."""
        self.assert_gate_fails("marker_in_phase_copy", "[F03]")

    def test_unclosed_interactive_only_block_fails_f03(self) -> None:
        """An unclosed marker used to silence every line after it, to end of file."""
        outcome = self.assert_gate_fails("marker_unclosed", "[F03]")
        self.assertIn("never closed", outcome.stdout)

    def test_deleting_a_close_marker_fails_f03(self) -> None:
        """Dropping one close marker leaves the rest of run/SKILL.md unscanned."""
        self.assert_gate_fails("marker_close_dropped", "[F03]")

    def test_stray_close_marker_fails_f03(self) -> None:
        self.assert_gate_fails("marker_stray_close", "[F03]")

    def test_marker_sharing_its_line_does_not_silence_the_line(self) -> None:
        """Only a marker alone on its line opens a block; anything else is prose."""
        self.assert_gate_fails("marker_shares_line", "[F03]")

    def test_newly_covered_person_routing_shapes_fail_f03(self) -> None:
        """Shapes the round-1 pattern missed: person, requester, owner, someone."""
        for index, shape in enumerate(PERSON_ROUTING_SHAPES):
            with self.subTest(shape=shape):
                outcome = self.outcome(f"shape_{index}")
                self.assertEqual(outcome.returncode, 1, outcome.stdout)
                self.assertIn("[F03]", outcome.stdout)
                self.assertIn(BUILD_SKILL, outcome.stdout)

    def test_broken_self_test_regex_fails_f03(self) -> None:
        outcome = self.outcome("broken_self_test")
        self.assertEqual(outcome.returncode, 1)
        self.assertIn("[F03]", outcome.stdout)
        self.assertIn("pattern no longer matches", outcome.stdout)

    def test_missing_result_protocol_literal_fails_f02(self) -> None:
        self.assert_gate_fails("result_literal_renamed", "[F02]")

    def test_invoking_another_phase_fails_f02(self) -> None:
        self.assert_gate_fails("phase_invocation", "[F02]")

    def test_unattended_wording_check_is_clean_over_the_four_phase_copies(self) -> None:
        outcome = self.outcome("clean")
        self.assertEqual(outcome.returncode, 0, outcome.output)
        self.assertNotIn("[F03]", outcome.stdout)

    def test_protocol_check_is_clean_over_every_phase_skill(self) -> None:
        outcome = self.outcome("clean")
        self.assertEqual(outcome.returncode, 0, outcome.output)
        self.assertNotIn("[F02]", outcome.stdout)


class RepoToolsTest(unittest.TestCase):
    """Checks that read a copy or the repo itself without running the gate."""

    def setUp(self) -> None:
        self.copy_root = copy_repo()

    def tearDown(self) -> None:
        shutil.rmtree(self.copy_root.parent, ignore_errors=True)

    def test_build_codex_plugin_check_reports_up_to_date(self) -> None:
        rebuild(self.copy_root)
        result = subprocess.run(
            [sys.executable, "scripts/build_codex_plugin.py", "--check", "--plugin", "factory"],
            cwd=self.copy_root,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("up to date", result.stdout)

    def test_architecture_check_is_clean_on_the_updated_overview(self) -> None:
        result = subprocess.run(
            [sys.executable, "dev/scripts/architecture-check.py", "docs/architecture.md", "--root", "."],
            cwd=self.copy_root,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class PhaseCopyDriftTest(unittest.TestCase):
    """Reads the real tree only; no copy, no gate."""

    def test_phase_copies_stay_within_five_lines_of_dev(self) -> None:
        for skill in ("scope", "scope-review", "build", "ship"):
            dev_lines = len(
                (REPO_ROOT / "dev" / "skills" / skill / "SKILL.md")
                .read_text(encoding="utf-8")
                .splitlines()
            )
            factory_lines = len(
                (REPO_ROOT / "factory" / "skills" / skill / "SKILL.md")
                .read_text(encoding="utf-8")
                .splitlines()
            )
            with self.subTest(skill=skill):
                self.assertLessEqual(
                    abs(factory_lines - dev_lines),
                    5,
                    f"{skill}: factory copy is {factory_lines} lines, dev is {dev_lines}",
                )


if __name__ == "__main__":
    unittest.main()
