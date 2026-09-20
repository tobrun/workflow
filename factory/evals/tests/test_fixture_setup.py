"""Unit tests for factory/evals/fixture/setup.sh - offline and deterministic.

setup.sh is the fixture's own carrier of the .dev/ never-committed invariant
(the run skill's preflight is the carrier in any other consuming repository).
The two paid host runs and the planted-secret variant stay a recorded manual
procedure in factory/evals/README.md; no mocked layer can drive a real host.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SETUP_SH = REPO_ROOT / "factory" / "evals" / "fixture" / "setup.sh"


class FixtureSetupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="fixture-setup-test-"))
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)
        dest = self.scratch / "fixture"
        result = subprocess.run(
            ["bash", str(SETUP_SH), str(dest)],
            capture_output=True,
            text=True,
            check=True,
        )
        self.dest = Path(result.stdout.strip())

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self.dest, check=False, capture_output=True, text=True
        )

    def test_setup_produces_a_main_repo_with_one_commit_and_a_bare_origin(self) -> None:
        branch = self.git("branch", "--show-current")
        self.assertEqual(branch.stdout.strip(), "main")

        log = self.git("log", "--oneline")
        self.assertEqual(len(log.stdout.strip().splitlines()), 1)

        gitignore = (self.dest / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".dev/", gitignore)

        ls_remote = self.git("ls-remote", "origin")
        self.assertEqual(ls_remote.returncode, 0, ls_remote.stderr)
        self.assertIn("refs/heads/main", ls_remote.stdout)

    def test_push_after_writing_dev_plan_file_leaves_no_dev_path(self) -> None:
        plan_dir = self.dest / ".dev" / "plan"
        plan_dir.mkdir(parents=True)
        (plan_dir / "spec.md").write_text("spec\n", encoding="utf-8")
        with (self.dest / "README.md").open("a", encoding="utf-8") as handle:
            handle.write("\nextra\n")

        add = self.git("add", "-A")
        self.assertEqual(add.returncode, 0, add.stderr)
        commit = self.git("commit", "-m", "test commit")
        self.assertEqual(commit.returncode, 0, commit.stderr)
        push = self.git("push", "origin", "main")
        self.assertEqual(push.returncode, 0, push.stderr)

        log = self.git("log", "--name-only", "--pretty=format:")
        self.assertNotIn(".dev/", log.stdout)


if __name__ == "__main__":
    unittest.main()
