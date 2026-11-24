"""Tests for ci-evidence.py, the CI-side writer of a pull request's verification evidence.

Run from the repo root: python3 -m unittest discover -s dev/evals/tests -p 'test_ci_evidence.py' -t .
Each test builds a git repository with a base commit and a feature commit in a
scratch directory, then runs the script as a subprocess, the way CI does. Test
results come from a small standard-library runner that writes JUnit, and coverage
from hand-written reports, so nothing needs installing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "dev" / "scripts" / "ci-evidence.py"

# Imports each test file given on the command line and writes one JUnit case per test function.
RUNNER = '''
import importlib.util, sys, traceback, xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0, "src")
suite = ET.Element("testsuite")
for path in sys.argv[2:]:
    module = path[:-3].replace("/", ".")
    try:
        spec = importlib.util.spec_from_file_location(module, path)
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
    except Exception as error:
        case = ET.SubElement(suite, "testcase", classname="", name=module)
        ET.SubElement(case, "error", message="collection failure").text = repr(error)
        continue
    for name in sorted(n for n in dir(loaded) if n.startswith("test_")):
        case = ET.SubElement(suite, "testcase", classname=module, name=name)
        try:
            getattr(loaded, name)()
        except Exception as error:
            ET.SubElement(case, "failure", message=repr(error)).text = traceback.format_exc()
ET.ElementTree(suite).write(sys.argv[1])
'''

BASE_CODE = "def total(prices):\n    return sum(prices)\n"
BASE_TESTS = ("from cart.pricing import total\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n"
              "    assert total([]) == 0\n")
FEATURE_CODE = ("def total(prices, code=''):\n    subtotal = sum(prices)\n    if code == 'HALF':\n"
                "        return subtotal / 2\n    if code == 'TEN':\n        return subtotal - 10\n    return subtotal\n")
FEATURE_TESTS = ("from cart.pricing import total\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n    assert total([]) == 0\n\n\n"
                 "def test_half():\n    assert total([4, 6], 'HALF') == 5\n\n\n"
                 "def test_plain_sum_still_works():\n    assert total([5]) == 5\n")
BODY = """## Summary

Discount codes.

## Acceptance criteria

| Scenario | Criterion | Test | Status |
| --- | --- | --- | --- |
| CS1-S1 | HALF halves the total | `tests.test_pricing::test_half` | awaiting CI |
| CS1-S2 | TEN takes 10 off | `tests.test_pricing::test_ten` | awaiting CI |

## Quality

| Kind | Result | Found | Fixed | Surviving |
| --- | --- | --- | --- | --- |
| unit | ran | 0 | 0 | 0 |
| coverage | ran | 0 | 0 | 0 |

<!-- BEGIN DEV-EVIDENCE -->
<!-- END DEV-EVIDENCE -->
"""


class Repo:
    def __init__(self, test: unittest.TestCase) -> None:
        scratch = tempfile.TemporaryDirectory()
        test.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.git("init", "-q", "-b", "main")
        self.write("runner.py", RUNNER)
        self.write("src/cart/__init__.py", "")
        self.write("src/cart/pricing.py", BASE_CODE)
        self.write("tests/__init__.py", "")
        self.write("tests/test_pricing.py", BASE_TESTS)
        self.commit("base")
        self.git("checkout", "-q", "-b", "feature")

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", message)

    def junit(self, name: str = "junit.xml", tests: str = "tests/test_pricing.py") -> str:
        subprocess.run([sys.executable, "runner.py", name, *tests.split()], cwd=self.root, check=True)
        return name

    def collect(self, *extra: str, body: str | None = BODY) -> tuple[subprocess.CompletedProcess, dict, str]:
        args = [sys.executable, str(SCRIPT), "collect", "--base", "main", "--out", "out", *extra]
        if body is not None:
            self.write("body.md", body)
            args += ["--body", "body.md"]
        result = subprocess.run(args, cwd=self.root, capture_output=True, text=True, check=False)
        if result.returncode not in (0, 1):
            raise AssertionError(result.stderr)
        evidence = json.loads((self.root / "out/evidence.json").read_text(encoding="utf-8"))
        return result, evidence, (self.root / "out/block.md").read_text(encoding="utf-8")


REVERT = f"{sys.executable} runner.py {{junit}} {{tests}}"


class GuardTest(unittest.TestCase):
    def test_a_removed_assertion_and_a_new_skip_are_flagged(self) -> None:
        repo = Repo(self)
        repo.write("tests/test_pricing.py", "import pytest\nfrom cart.pricing import total\n\n\n"
                   "@pytest.mark.skip(reason='flaky')\ndef test_total():\n    assert total([1, 2]) == 3\n")
        repo.commit("weaken")
        _, evidence, block = repo.collect(body=None)
        kinds = [(g["kind"], g["text"]) for g in evidence["guard"]]
        self.assertIn(("assertion removed or changed", "assert total([]) == 0"), kinds)
        self.assertIn(("test skipped or focused", "@pytest.mark.skip(reason='flaky')"), kinds)
        self.assertIn("| Test-diff guard | ⚠️ |", block)

    def test_a_moved_assertion_is_not_a_removal(self) -> None:
        repo = Repo(self)
        repo.write("tests/test_pricing.py", "from cart.pricing import total\n\n\ndef test_empty():\n"
                   "    assert total([]) == 0\n\n\ndef test_total():\n    assert total([1, 2]) == 3\n")
        repo.commit("reorder")
        _, evidence, block = repo.collect(body=None)
        self.assertEqual(evidence["guard"], [])
        self.assertIn("no test weakened, skipped, or regenerated", block)

    def test_focused_and_skipped_tests_in_other_languages_are_flagged(self) -> None:
        repo = Repo(self)
        repo.write("web/cart.test.ts", "it.only('adds', () => { expect(add(1, 2)).toBe(3) })\n")
        repo.write("svc/cart_test.go", "func TestAdd(t *testing.T) {\n\tt.Skip(\"later\")\n}\n")
        repo.commit("other languages")
        _, evidence, _ = repo.collect(body=None)
        where = {g["where"] for g in evidence["guard"] if g["kind"] == "test skipped or focused"}
        self.assertEqual(where, {"web/cart.test.ts:1", "svc/cart_test.go:2"})

    def test_a_regenerated_snapshot_a_deleted_test_and_a_lowered_threshold_are_flagged(self) -> None:
        repo = Repo(self)
        repo.git("checkout", "-q", "main")
        repo.write("tests/__snapshots__/view.snap", "old\n")
        repo.write("tests/test_other.py", "def test_x():\n    assert True\n")
        repo.write("pyproject.toml", "[tool.coverage.report]\nfail_under = 90\n")
        repo.commit("more base")
        repo.git("checkout", "-q", "feature")
        repo.git("merge", "-q", "main")
        repo.write("tests/__snapshots__/view.snap", "new\n")
        (repo.root / "tests/test_other.py").unlink()
        repo.write("pyproject.toml", "[tool.coverage.report]\nfail_under = 70\n")
        repo.commit("loosen")
        _, evidence, _ = repo.collect(body=None)
        found = {(g["kind"], g["where"]) for g in evidence["guard"]}
        self.assertIn(("snapshot regenerated", "tests/__snapshots__/view.snap"), found)
        self.assertIn(("test file deleted", "tests/test_other.py"), found)
        self.assertIn(("threshold lowered", "pyproject.toml:2"), found)


class CoverageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repo(self)
        self.repo.write("src/cart/pricing.py", FEATURE_CODE)
        self.repo.write("tests/test_pricing.py", FEATURE_TESTS)
        self.repo.commit("feature")

    def test_lcov_changed_lines_are_counted_and_gaps_named(self) -> None:
        lcov = "SF:src/cart/pricing.py\n" + "".join(f"DA:{n},{0 if n in (5, 6) else 1}\n" for n in range(1, 8)) + "end_of_record\n"
        self.repo.write("lcov.info", lcov)
        _, evidence, block = self.repo.collect("--coverage", "lcov.info", body=None)
        self.assertEqual((evidence["coverage"]["covered"], evidence["coverage"]["total"]), (5, 7))
        self.assertIn("| Changed-line coverage | ⚠️ | 71% of 7 changed lines (threshold 80%) |", block)
        self.assertIn("- `src/cart/pricing.py`: 5-6", block)

    def test_cobertura_and_go_profiles_are_read(self) -> None:
        cobertura = ('<coverage><sources><source>src</source></sources><packages><package><classes>'
                     '<class filename="cart/pricing.py"><lines>'
                     + "".join(f'<line number="{n}" hits="1"/>' for n in range(1, 8))
                     + "</lines></class></classes></package></packages></coverage>")
        self.repo.write("coverage.xml", cobertura)
        _, evidence, _ = self.repo.collect("--coverage", "coverage.xml", body=None)
        self.assertEqual(evidence["coverage"]["percent"], 100.0)
        self.repo.write("cover.out", "mode: set\nexample.com/x/src/cart/pricing.py:1.1,4.10 2 1\n"
                                     "example.com/x/src/cart/pricing.py:5.1,7.10 1 0\n")
        _, evidence, _ = self.repo.collect("--coverage", "cover.out", body=None)
        self.assertEqual(evidence["coverage"]["uncovered"], {"src/cart/pricing.py": [5, 6, 7]})

    def test_a_changed_file_the_report_misses_is_listed(self) -> None:
        self.repo.write("lcov.info", "SF:src/other.py\nDA:1,1\nend_of_record\n")
        _, evidence, block = self.repo.collect("--coverage", "lcov.info", body=None)
        self.assertEqual(evidence["coverage"]["unmeasured"], ["src/cart/pricing.py"])
        self.assertIn("Changed files the coverage report does not cover", block)


class RevertTest(unittest.TestCase):
    def test_new_tests_must_fail_on_the_base_and_the_ones_that_pass_are_named(self) -> None:
        repo = Repo(self)
        repo.write("src/cart/pricing.py", FEATURE_CODE)
        repo.write("tests/test_pricing.py", FEATURE_TESTS)
        repo.commit("feature")
        _, evidence, block = repo.collect("--revert-cmd", REVERT, body=None)
        self.assertEqual(evidence["revert"]["new_failing"], ["tests.test_pricing::test_half"])
        self.assertEqual(evidence["revert"]["new_passing"], ["tests.test_pricing::test_plain_sum_still_works"])
        self.assertIn("| Fails on revert | ⚠️ | 1 of 2 new tests fail on base", block)
        self.assertIn("- `tests.test_pricing::test_plain_sum_still_works`", block)
        self.assertEqual(repo.git("worktree", "list").count("\n"), 1, "the base worktree is removed")

    def test_a_test_file_that_cannot_load_on_the_base_is_reported_apart(self) -> None:
        repo = Repo(self)
        repo.write("src/cart/discount.py", "def half(x):\n    return x / 2\n")
        repo.write("tests/test_discount.py", "from cart.discount import half\n\n\ndef test_half():\n    assert half(4) == 2\n")
        repo.commit("new module")
        _, evidence, block = repo.collect("--revert-cmd", REVERT, body=None)
        self.assertEqual(evidence["revert"]["unloadable"], ["::tests.test_discount"])
        self.assertIn("1 test file(s) do not load on base", block)

    def test_without_a_command_it_says_so(self) -> None:
        repo = Repo(self)
        repo.write("tests/test_pricing.py", FEATURE_TESTS)
        repo.commit("tests")
        _, _, block = repo.collect(body=None)
        self.assertIn("| Fails on revert | ➖ | not run | no --revert-cmd configured |", block)


class ClaimsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repo(self)
        self.repo.write("src/cart/pricing.py", FEATURE_CODE)
        self.repo.write("tests/test_pricing.py", FEATURE_TESTS)
        self.repo.commit("feature")

    def test_acceptance_status_comes_from_ci_and_an_unknown_test_is_a_mismatch(self) -> None:
        _, evidence, block = self.repo.collect("--junit", self.repo.junit())
        self.assertIn("| CS1-S1 | `tests.test_pricing::test_half` | ✅ passed |", block)
        self.assertIn("| CS1-S2 | `tests.test_pricing::test_ten` | ❓ not in any report |", block)
        self.assertIn("CS1-S2 names `tests.test_pricing::test_ten`, which no CI report contains", evidence["mismatches"])

    def test_a_clean_claim_against_failing_ci_is_a_mismatch_and_fails_the_verdict(self) -> None:
        self.repo.write("tests/test_pricing.py", FEATURE_TESTS + "\n\ndef test_broken():\n    assert total([1]) == 2\n")
        self.repo.commit("broken")
        result, evidence, block = self.repo.collect("--junit", self.repo.junit(), "--strict")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(evidence["verdict"], "failed")
        self.assertIn("unit claims a clean run, but CI has 1 failing test(s)", evidence["mismatches"])
        self.assertIn("### ❌ CI found failures at", block)

    def test_a_clean_change_is_verified_and_the_predicate_says_passed(self) -> None:
        body = BODY.replace("| CS1-S2 | TEN takes 10 off | `tests.test_pricing::test_ten` | awaiting CI |\n", "")
        self.repo.write("lcov.info", "SF:src/cart/pricing.py\n" + "".join(f"DA:{n},1\n" for n in range(1, 8)) + "end_of_record\n")
        result, evidence, block = self.repo.collect("--junit", self.repo.junit(), "--coverage", "lcov.info",
                                                    "--run-url", "https://ci.example/run/7", "--workflow", "verify.yml",
                                                    "--strict", body=body)
        self.assertEqual(result.returncode, 0, block)
        self.assertEqual(evidence["verdict"], "verified")
        self.assertIn("### ✅ Verified by CI at `", block)
        self.assertIn("[CI run](https://ci.example/run/7) · `verify.yml`", block)
        predicate = json.loads((self.repo.root / "out/predicate.json").read_text(encoding="utf-8"))
        self.assertEqual(predicate["result"], "PASSED")
        self.assertEqual(predicate["configuration"][0]["digest"]["gitCommit"], evidence["head"])
        self.assertIn("tests.test_pricing::test_half", predicate["passedTests"])


class MissingReportTest(unittest.TestCase):
    def test_a_test_step_that_wrote_no_report_still_gets_a_failing_block(self) -> None:
        repo = Repo(self)
        repo.write("tests/test_pricing.py", FEATURE_TESTS)
        repo.commit("tests")
        _, evidence, block = repo.collect("--junit", "reports/junit/*.xml", body=None)
        self.assertEqual(evidence["verdict"], "failed")
        self.assertIn("| Tests | ❌ | no JUnit report | nothing matched reports/junit/*.xml |", block)


class BodyTest(unittest.TestCase):
    def run_script(self, *args: str) -> None:
        subprocess.run([sys.executable, str(SCRIPT), *args], check=True, capture_output=True)

    def test_publish_replaces_only_the_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body, block = Path(tmp, "body.md"), Path(tmp, "block.md")
            body.write_text("## Summary\n\nkeep me\n\n<!-- BEGIN DEV-EVIDENCE -->\nforged by an agent\n<!-- END DEV-EVIDENCE -->\n")
            block.write_text("<!-- BEGIN DEV-EVIDENCE -->\n### ✅ Verified by CI at `abc1234`\n<!-- END DEV-EVIDENCE -->\n")
            self.run_script("publish", "--block", str(block), "--body-file", str(body))
            text = body.read_text()
            self.assertIn("keep me", text)
            self.assertIn("Verified by CI at `abc1234`", text)
            self.assertNotIn("forged", text)
            self.run_script("pending", "--sha", "def5678", "--body-file", str(body))
            self.assertIn("### ⏳ Verifying `def5678`", body.read_text())
            self.assertNotIn("abc1234", body.read_text())

    def test_splice_keeps_the_current_block_when_the_agent_rewrites_the_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            new, current = Path(tmp, "pr.md"), Path(tmp, "current.md")
            new.write_text("## Summary\n\nupdated\n\n<!-- BEGIN DEV-EVIDENCE -->\n<!-- END DEV-EVIDENCE -->\n")
            current.write_text("## Summary\n\nold\n\n<!-- BEGIN DEV-EVIDENCE -->\n### ✅ Verified by CI at `abc1234`\n<!-- END DEV-EVIDENCE -->\n")
            self.run_script("splice", str(new), "--from", str(current))
            text = new.read_text()
            self.assertIn("updated", text)
            self.assertIn("Verified by CI at `abc1234`", text)
            self.assertEqual(text.count("BEGIN DEV-EVIDENCE"), 1)


if __name__ == "__main__":
    unittest.main()
