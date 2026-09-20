"""Unit tests for the factory-specific validate.sh checks (F02, F03).

Each scenario copies the repo tree to a temp directory, mutates the copy,
rebuilds the Codex distributions there (any mutation under factory/ makes
plugins/factory stale, so validate.sh would fail on [C01] instead of on the
check under test without a rebuild), then runs the copy's own
scripts/validate.sh - never the real repo's.

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
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".DS_Store"
)


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


class FactoryChecksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.copy_root = copy_repo()

    def tearDown(self) -> None:
        shutil.rmtree(self.copy_root.parent, ignore_errors=True)

    def append_to(self, relative: str, text: str) -> Path:
        target = self.copy_root / relative
        with target.open("a", encoding="utf-8") as handle:
            handle.write(text)
        return target

    def assert_rebuilt_copy_fails(self, tag: str, target: Path) -> None:
        """Rebuild the mutated copy, run its validate.sh, expect `tag` at `target`."""
        rebuild(self.copy_root)
        result = run_validate(self.copy_root)
        self.assertEqual(result.returncode, 1)
        self.assertIn(tag, result.stdout)
        self.assertIn(str(target.relative_to(self.copy_root)), result.stdout)
        self.assertNotIn("[C01]", result.stdout)

    def test_unmutated_copy_passes_and_excludes_the_harness(self) -> None:
        result = run_validate(self.copy_root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        log = self.copy_root / "validate-logs" / "factory-script-tests.log"
        if log.is_file():
            contents = log.read_text(encoding="utf-8")
            self.assertIn("test_run_state", contents)
            self.assertNotIn("test_factory_checks", contents)

    def test_decision_routed_to_a_person_fails_f03(self) -> None:
        target = self.append_to(
            "factory/skills/build/SKILL.md", "\n\nAsk the user which one to pick.\n"
        )
        self.assert_rebuilt_copy_fails("[F03]", target)

    def test_human_call_wording_fails_f03(self) -> None:
        """A phrasing the first F03 pattern missed: the plural defeated `human call`."""
        target = self.append_to(
            "factory/skills/ship/SKILL.md",
            "\n\nRotation and history rewriting are both human calls.\n",
        )
        self.assert_rebuilt_copy_fails("[F03]", target)

    def test_question_left_for_the_user_fails_f03(self) -> None:
        target = self.append_to(
            "factory/skills/build/SKILL.md",
            "\n\nThen any question left for the user - a blocked gate.\n",
        )
        self.assert_rebuilt_copy_fails("[F03]", target)

    def test_prose_about_people_does_not_fire_f03(self) -> None:
        """The widened pattern must not fire on possessives or compounds."""
        self.append_to(
            "factory/skills/build/SKILL.md",
            "\n\nA user-facing change in the user's repository still needs an e2e scenario.\n",
        )
        rebuild(self.copy_root)
        result = run_validate(self.copy_root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_same_line_inside_interactive_only_passes(self) -> None:
        self.append_to(
            "factory/skills/build/SKILL.md",
            "\n\n<!-- interactive-only -->\nAsk the user which one to pick.\n<!-- /interactive-only -->\n",
        )
        rebuild(self.copy_root)
        result = run_validate(self.copy_root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_broken_self_test_regex_fails_f03(self) -> None:
        validate_sh = self.copy_root / "scripts" / "validate.sh"
        text = validate_sh.read_text(encoding="utf-8")
        # Break the F03 pattern so it no longer matches its own self-test samples.
        broken = text.replace(
            r'ASK = r"ask(?:s|ed|ing)?|prompt(?:s|ed|ing)?',
            r'ASK = r"askXXX(?:s|ed|ing)?|promptXXX(?:s|ed|ing)?',
        )
        self.assertNotEqual(text, broken, "expected the F03 pattern text to be present")
        validate_sh.write_text(broken, encoding="utf-8")
        result = run_validate(self.copy_root)
        self.assertEqual(result.returncode, 1)
        self.assertIn("[F03]", result.stdout)
        self.assertIn("pattern no longer matches", result.stdout)

    def test_missing_result_protocol_literal_fails_f02(self) -> None:
        target = self.copy_root / "factory" / "skills" / "build" / "SKILL.md"
        text = target.read_text(encoding="utf-8")
        text = text.replace("results/{phase}-{attempt}.json", "some-other-path.json")
        target.write_text(text, encoding="utf-8")
        self.assert_rebuilt_copy_fails("[F02]", target)

    def test_invoking_another_phase_fails_f02(self) -> None:
        target = self.append_to(
            "factory/skills/build/SKILL.md", "\n\nOn success, invoke $factory:ship.\n"
        )
        self.assert_rebuilt_copy_fails("[F02]", target)

    def test_unattended_wording_check_is_clean_over_the_four_phase_copies(self) -> None:
        result = run_validate(self.copy_root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("[F03]", result.stdout)

    def test_protocol_check_is_clean_over_every_phase_skill(self) -> None:
        result = run_validate(self.copy_root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("[F02]", result.stdout)

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
