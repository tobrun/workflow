"""Unit tests for the two checks that keep a build fast: lint-spec.py's change-set
size limit and wave report.

Run from the repo root: python3 -m unittest discover -s dev/evals/tests -t .
Each test writes a scratch plan directory and runs the script as a subprocess, so
it proves the CLI contract the scope and build skills loop against.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LINT = REPO_ROOT / "dev" / "skills" / "scope" / "scripts" / "lint-spec.py"
CHECK = REPO_ROOT / "dev" / "skills" / "build" / "scripts" / "check-tests.py"

HEAD = """# Coupons

## Research

D-coupon-store: Where do coupons live?
  ✓ a table - one query reads them
  ✗ a config file - a redeploy per coupon

D-expiry-clock: Which clock decides expiry?
  ✓ the server clock - one source of time
  ✗ the client clock - a user can set it back

## Scope

Inputs: a coupon code. Outputs: a discounted total.

### Validation

- `npm run typecheck`
- `npm test`

## Change plan

"""


def scenarios(count: int) -> str:
    return "; ".join(f"[unit] case {n} -> outcome {n}" for n in range(1, count + 1))


def change_set(number: int, files: str, count: int = 1, decision: str = "") -> str:
    link = f" - decisions: {decision}" if decision else ""
    return f"{number}. Change set {number}\n   a. {files} - edit{link}\n   tests: {scenarios(count)}\n\n"


def run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args], check=False, capture_output=True, text=True
    )


class PlanCase(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.plan = Path(scratch.name) / "coupons"
        self.plan.mkdir()

    def write_spec(self, *change_sets: str) -> Path:
        spec = self.plan / "spec.md"
        spec.write_text(HEAD + "".join(change_sets), encoding="utf-8")
        return spec

    def write_notes(self, text: str) -> None:
        (self.plan / "implementation-notes.md").write_text(text, encoding="utf-8")


class ScenarioLimitTest(PlanCase):
    def test_change_set_at_the_limit_is_clean(self) -> None:
        spec = self.write_spec(change_set(1, "`src/a.ts`", 25))
        result = run(LINT, str(spec))
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_change_set_over_the_limit_fails_naming_it_and_its_count(self) -> None:
        spec = self.write_spec(change_set(1, "`src/a.ts`"), change_set(2, "`src/b.ts`", 26))
        result = run(LINT, str(spec))
        self.assertEqual(result.returncode, 1)
        self.assertIn("change set 2 carries 26 scenarios", result.stdout)
        self.assertNotIn("change set 1 carries", result.stdout)

    def test_oversized_change_set_already_built_is_not_resized(self) -> None:
        spec = self.write_spec(change_set(1, "`src/a.ts`", 40), change_set(2, "`src/b.ts`"))
        self.write_notes("## Change set 1: Change set 1\n- What was done: it\n")
        result = run(LINT, str(spec))
        self.assertEqual(result.returncode, 0, result.stdout)


class WaveReportTest(PlanCase):
    def waves(self, *change_sets: str) -> str:
        result = run(LINT, str(self.write_spec(*change_sets)))
        self.assertEqual(result.returncode, 0, result.stdout)
        return result.stdout

    def test_disjoint_change_sets_share_one_wave(self) -> None:
        out = self.waves(change_set(1, "`src/a.ts`"), change_set(2, "`src/b.ts`"))
        self.assertIn("2 change sets in 1 waves - [1 2]", out)
        self.assertNotIn("waits on", out)

    def test_shared_file_queues_change_sets_and_is_named(self) -> None:
        out = self.waves(
            change_set(1, "`src/a.ts`, `src/compose.ts`"),
            change_set(2, "`src/b.ts`, `src/compose.ts`"),
            change_set(3, "`src/c.ts`, `src/compose.ts`"),
        )
        self.assertIn("3 change sets in 3 waves - [1] [2] [3]", out)
        self.assertIn("  2 waits on 1: src/compose.ts", out)
        self.assertIn("  3 waits on 2: src/compose.ts", out)

    def test_later_change_set_skips_ahead_only_past_change_sets_it_shares_nothing_with(self) -> None:
        out = self.waves(
            change_set(1, "`src/a.ts`"),
            change_set(2, "`src/a.ts`, `src/b.ts`"),
            change_set(3, "`src/b.ts`"),
            change_set(4, "`src/d.ts`"),
        )
        # 3 shares nothing with 1 but must not overtake 2, which is still held back.
        self.assertIn("4 change sets in 3 waves - [1 4] [2] [3]", out)

    def test_bare_file_name_matches_the_path_that_ends_in_it(self) -> None:
        out = self.waves(change_set(1, "`src/app/compose.ts`"), change_set(2, "`compose.ts`"))
        self.assertIn("[1] [2]", out)

    def test_code_in_backticks_and_the_tests_line_are_not_files(self) -> None:
        out = self.waves(
            change_set(1, "`src/a.ts` - sets `embedder.model` and `job.stages`"),
            change_set(2, "`src/b.ts` - reads `embedder.model` and `job.stages`"),
        )
        self.assertIn("[1 2]", out)

    def test_built_change_sets_are_left_out_of_the_waves(self) -> None:
        spec = self.write_spec(
            change_set(1, "`src/a.ts`"), change_set(2, "`src/a.ts`"), change_set(3, "`src/c.ts`")
        )
        self.write_notes("## Change set 1: Change set 1\n- What was done: it\n")
        result = run(LINT, str(spec))
        self.assertIn("2 change sets in 1 waves - [2 3]", result.stdout)

    def test_single_change_set_prints_no_waves(self) -> None:
        out = self.waves(change_set(1, "`src/a.ts`"))
        self.assertNotIn("build waves", out)


class TestNameWithCommaTest(PlanCase):
    """A test name is prose; a comma in it must not send the build back around the check loop."""

    def check(self, tests_added: str) -> subprocess.CompletedProcess[str]:
        self.write_spec(change_set(1, "`src/a.ts`", 2))
        repo = self.plan.parent
        (repo / "a.test.ts").write_text(
            'test("a set name, and a longer text counts more", () => {});\n'
            'test("an empty text gives a unit vector", () => {});\n',
            encoding="utf-8",
        )
        self.write_notes(f"## Change set 1: Change set 1\n- Tests added: {tests_added}\n")
        return run(CHECK, str(self.plan), "--repo-root", str(repo), "--inventory-only")

    def test_test_name_holding_a_comma_is_one_test(self) -> None:
        result = self.check(
            "a.test.ts::a set name, and a longer text counts more, a.test.ts::an empty text gives a unit vector"
        )
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_named_test_that_is_not_in_the_file_still_fails(self) -> None:
        result = self.check("a.test.ts::a set name, and a longer text counts more, a.test.ts::never written")
        self.assertEqual(result.returncode, 1)
        self.assertIn("contains no test named 'never written'", result.stdout)


class RunAndFixupEntryTest(PlanCase):
    """The notes are written as the run goes, so they hold more than change set entries."""

    RUN = "## Build run 2026-10-02\n- Validation: `npm test`, from the spec\n- Waves: [1]\n\n"

    def check(self, entry: str, fixup: str) -> subprocess.CompletedProcess[str]:
        self.write_spec(change_set(1, "`src/a.ts`", 2))
        repo = self.plan.parent
        (repo / "a.test.ts").write_text(
            'test("reads a coupon", () => {});\ntest("rejects an expired coupon", () => {});\n',
            encoding="utf-8",
        )
        self.write_notes(
            f"{self.RUN}## Change set 1: Change set 1\n- Tests added: {entry}\n\n"
            f"## Fixup: the built CLI printed no newline\n- Found by: e2e scenario 3\n- Tests added: {fixup}\n"
        )
        return run(CHECK, str(self.plan), "--repo-root", str(repo), "--inventory-only")

    def test_run_entry_and_fixup_entry_leave_a_complete_change_set_clean(self) -> None:
        result = self.check(
            "a.test.ts::reads a coupon, a.test.ts::rejects an expired coupon", "a.test.ts::reads a coupon"
        )
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_fixup_tests_do_not_count_toward_the_change_set_above(self) -> None:
        result = self.check("a.test.ts::reads a coupon", "a.test.ts::rejects an expired coupon")
        self.assertEqual(result.returncode, 1)
        self.assertIn("change set 1: 2 scenario(s) specced, 1 test(s) named", result.stdout)

    def test_fixup_test_that_was_never_written_fails_naming_the_fixup(self) -> None:
        result = self.check(
            "a.test.ts::reads a coupon, a.test.ts::rejects an expired coupon", "a.test.ts::never written"
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("a fixup entry: a.test.ts contains no test named 'never written'", result.stdout)



if __name__ == "__main__":
    unittest.main()
