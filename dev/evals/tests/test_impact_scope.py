"""Unit tests for impact-scope.py, the check build and ship loop against to
decide whether a local gate runs at the impacted scope or in full.

Run from the repo root: python3 -m unittest discover -s dev/evals/tests -t .
Each test builds a scratch git repository and runs the script as a subprocess,
so it proves the CLI contract the skills read.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "dev" / "scripts" / "impact-scope.py"

BASE_TREE = {
    "package.json": "{}",
    "package-lock.json": "{}",
    "tsconfig.json": "{}",
    "README.md": "# repo",
    "packages/api/package.json": "{}",
    "packages/api/src/index.ts": "export {}",
    "packages/web/package.json": "{}",
    "packages/web/src/app.ts": "export {}",
    "scripts/release.sh": "echo",
    ".github/workflows/pr.yml": "on: pull_request",
}


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class ImpactScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        for path, body in BASE_TREE.items():
            self.write(path, body)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "checkout", "-q", "-b", "work")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, path: str, body: str) -> None:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

    def scope(self, *args: str) -> dict:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--json", *args],
            cwd=self.repo, check=False, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_package_source_change_is_impacted_on_that_package(self) -> None:
        self.write("packages/api/src/index.ts", "export const a = 1")
        report = self.scope()
        self.assertEqual(report["verdict"], "impacted")
        self.assertEqual(report["packages"], ["packages/api"])

    def test_untracked_file_counts(self) -> None:
        self.write("packages/web/src/new.ts", "export {}")
        report = self.scope()
        self.assertEqual(report["packages"], ["packages/web"])

    def test_file_outside_any_package_belongs_to_root(self) -> None:
        self.write("scripts/release.sh", "echo 2")
        self.assertEqual(self.scope()["packages"], ["."])

    def test_docs_only_change_owes_no_run(self) -> None:
        self.write("README.md", "# changed")
        self.write("docs/guide.md", "new")
        self.assertEqual(self.scope()["verdict"], "docs")

    def test_no_change_is_docs(self) -> None:
        self.assertEqual(self.scope()["verdict"], "docs")

    def test_lockfile_widens_to_full(self) -> None:
        self.write("package-lock.json", '{"v": 2}')
        report = self.scope()
        self.assertEqual(report["verdict"], "full")
        self.assertIn("package-lock.json", report["reasons"][0])

    def test_ci_workflow_widens_to_full(self) -> None:
        self.write(".github/workflows/pr.yml", "on: push")
        self.assertEqual(self.scope()["verdict"], "full")

    def test_root_config_and_root_manifest_widen_to_full(self) -> None:
        self.write("tsconfig.json", '{"strict": true}')
        self.assertEqual(self.scope()["verdict"], "full")
        git(self.repo, "checkout", "-q", "--", "tsconfig.json")
        self.write("package.json", '{"name": "x"}')
        self.assertEqual(self.scope()["verdict"], "full")

    def test_nested_manifest_stays_impacted(self) -> None:
        self.write("packages/web/package.json", '{"name": "web"}')
        report = self.scope()
        self.assertEqual(report["verdict"], "impacted")
        self.assertEqual(report["packages"], ["packages/web"])

    def test_base_head_scopes_to_uncommitted_fixes(self) -> None:
        self.write("packages/api/src/index.ts", "export const a = 1")
        git(self.repo, "commit", "-qam", "api")
        self.write("packages/web/src/app.ts", "export const b = 2")
        self.assertEqual(self.scope()["packages"], ["packages/api", "packages/web"])
        self.assertEqual(self.scope("--base", "HEAD")["packages"], ["packages/web"])


if __name__ == "__main__":
    unittest.main()
