"""Shared fixtures: temporary repos with a bare origin, stub hosts, and scenario builders."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

STUBS = Path(__file__).resolve().parent / "stubs"

SPEC = """# Webhook idempotency

2026-09-15

## Research

D-dedup-store: Where are processed webhook ids remembered?
  ✓ database table - survives restarts and the database is already there
  ✗ in-memory set - lost on every restart

## Scope

Inputs: webhook deliveries. Outputs: each delivery id is processed once.

### Validation

- `readme`: `test -f README.md`

## Change plan

1. Deduplicate deliveries
   a. webhook.py - skip ids already processed - decisions: D-dedup-store (✓ database table)
   tests: [unit] repeated id -> ignored; [e2e] duplicate delivery -> processed once
"""

NOTES = """## Change set 1: Deduplicate deliveries
- What was done: skip seen ids
- Seams tested: Webhooks.deliver
- Tests added: tests/test_webhook.py::WebhookTests::test_repeated_id_ignored
"""

# The fixture repository is a tiny real application: its base has the bug, its tests assert
# behavior, its test runner reports outcomes by id, and its e2e driver captures real state.
BASE_WEBHOOK_PY = """class Webhooks:
    def __init__(self):
        self.processed = []

    def deliver(self, delivery_id, payload):
        self.processed.append(delivery_id)
        return "processed"
"""

WEBHOOK_PY = """class Webhooks:
    def __init__(self):
        self.processed = []

    def deliver(self, delivery_id, payload):
        if delivery_id in self.processed:
            return "ignored"
        self.processed.append(delivery_id)
        return "processed"
"""

TESTS_PY = """import unittest

from webhook import Webhooks


class WebhookTests(unittest.TestCase):
    def test_repeated_id_ignored(self):
        hooks = Webhooks()
        self.assertEqual(hooks.deliver("d1", {}), "processed")
        self.assertEqual(hooks.deliver("d1", {}), "ignored")
        self.assertEqual(hooks.processed, ["d1"])
"""

INERT_TESTS_PY = """import unittest


class WebhookTests(unittest.TestCase):
    def test_repeated_id_ignored(self):
        self.assertTrue(True)
"""

RUN_TESTS_PY = """\"\"\"Run unittest tests by `path::Class::method` id and report factory.test-results/1.\"\"\"
import argparse
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.getcwd())


def summary(trace):
    lines = [line for line in trace.strip().splitlines() if re.match(r"^[A-Za-z_.]*(Error|Exception)\\b", line)]
    return lines[-1] if lines else trace.strip().splitlines()[-1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True)
    parser.add_argument("ids", nargs="*")
    args = parser.parse_args()
    reported = []
    for test_id in args.ids:
        path, klass, method = test_id.split("::")
        module = path[:-3].replace("/", ".")
        try:
            suite = unittest.defaultTestLoader.loadTestsFromName(f"{module}.{klass}.{method}")
        except AttributeError:
            continue
        except Exception as error:
            reported.append({"id": test_id, "outcome": "error", "detail": f"{type(error).__name__}: {error}"})
            continue
        failed_load = [test for test in suite if type(test).__name__ == "_FailedTest"] if hasattr(suite, "__iter__") \\
            else ([suite] if type(suite).__name__ == "_FailedTest" else [])
        if failed_load:
            error = failed_load[0]._exception
            if isinstance(error, AttributeError):
                continue
            reported.append({"id": test_id, "outcome": "error", "detail": summary(str(error))})
            continue
        result = unittest.TestResult()
        suite.run(result)
        if result.errors:
            outcome, detail = "error", summary(result.errors[0][1])
        elif result.failures:
            outcome, detail = "failed", summary(result.failures[0][1])
        elif result.skipped:
            outcome, detail = "skipped", result.skipped[0][1]
        elif result.testsRun == 0:
            continue
        else:
            outcome, detail = "passed", ""
        reported.append({"id": test_id, "outcome": outcome, "detail": detail})
    with open(args.results, "w", encoding="utf-8") as handle:
        json.dump({"schema": "factory.test-results/1", "framework": "unittest", "tests": reported}, handle)
    return 0 if all(test["outcome"] == "passed" for test in reported) else 1


if __name__ == "__main__":
    sys.exit(main())
"""

E2E_DRIVER_PY = """import json
import os
import sys

sys.path.insert(0, os.getcwd())
from webhook import Webhooks

out = os.environ["FACTORY_E2E_OUT"]
hooks = Webhooks()
before = {"processed": len(hooks.processed)}
first, second = hooks.deliver("d1", {"n": 1}), hooks.deliver("d1", {"n": 1})
after = {"processed": len(hooks.processed)}
passed = after["processed"] == 1 and second == "ignored"
record = {
    "schema": "factory.e2e/1", "plan": "webhook", "kind": "non-frontend", "revision": os.environ["FACTORY_REVISION"],
    "scenarios": [{
        "id": "duplicate-delivery", "title": "a duplicate delivery is processed once",
        "given": "a delivery d1", "when": "d1 arrives twice", "then": "it is processed once",
        "status": "pass" if passed else "fail",
        "states": [{"step": "deliver d1 twice", "entity": "processed deliveries", "before": before, "after": after}],
        "assertions": [{"name": "processed once", "passed": passed, "detail": f"responses {first}, {second}"}],
        "output": f"{first} then {second}",
    }],
}
with open(os.path.join(out, "e2e.json"), "w", encoding="utf-8") as handle:
    json.dump(record, handle)
sys.exit(0 if passed else 1)
"""

SCENARIO_MAP = {"schema": "factory.scenario-map/1", "scenarios": {
    "S1": {"layer": "unit", "tests": ["tests/test_webhook.py::WebhookTests::test_repeated_id_ignored"]},
    "S2": {"layer": "e2e", "e2e_case": "duplicate-delivery"},
}}

PR_BODY = """## Summary

Deduplicate webhook deliveries.

## Evidence

**Before**

```json
{"processed": 2}
```

**After**

```json
{"processed": 1}
```
"""


def e2e_record(passed: int = 1, failed: int = 0, plan: str = "{plan}") -> str:
    """A factory.e2e/1 sidecar with `passed` passing and `failed` failing scenario records."""
    scenarios = [{"id": f"S{index + 1}", "title": f"scenario {index + 1}", "status": "pass" if index < passed else "fail",
                  "states": [{"step": "deliver twice", "entity": "deliveries", "before": {"processed": 2},
                              "after": {"processed": 1}}]}
                 for index in range(passed + failed)]
    return json.dumps({"schema": "factory.e2e/1", "plan": plan, "kind": "non-frontend", "scenarios": scenarios})


CONTRACT = {
    "schema": "factory.repo-contract/1",
    "validation": [{"id": "readme", "run": ["test", "-f", "README.md"]}],
    "tests": {"run": {"run": ["python3", "tools/run_tests.py", "--results", "{results}", "{tests}"]},
              "results": "factory.test-results/1", "layers": {"unit": ["tests/*.py"]}},
    "e2e": {"driver": {"run": ["python3", "e2e/run.py"]}},
    "ci": {"required_checks": ["ci"]},
    "gauntlet": {check: {"not_applicable": "the fixture application is a single dependency-free module"}
                 for check in ("security", "dead-code", "duplication", "complexity-coverage", "flakiness", "mutation")},
}

BASE_FILES = {
    "README.md": "# project\n",
    ".gitignore": "__pycache__/\n",
    ".factory/contract.json": json.dumps(CONTRACT, indent=2) + "\n",
    "tools/run_tests.py": RUN_TESTS_PY,
    "webhook.py": BASE_WEBHOOK_PY,
    "tests/__init__.py": "",
}


def seed_repo(repo: Path) -> None:
    """Write the fixture application's base files into a repository directory."""
    for relative, content in BASE_FILES.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: Path, name: str = "project") -> Path:
    origin = root / f"{name}-origin.git"
    repo = root / name
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(origin)], check=True)
    repo.mkdir()
    git(repo, "init", "--quiet", "-b", "main")
    git(repo, "config", "user.name", "Factory Test")
    git(repo, "config", "user.email", "factory-test@example.com")
    git(repo, "config", "commit.gpgsign", "false")
    seed_repo(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "initial")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "--quiet", "-u", "origin", "main")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    return repo


def scope_files(spec: str = SPEC) -> dict:
    return {"files": {".dev/{plan}/spec.md": spec}}


def review_record(lenses: tuple[str, ...], *, kind: str = "code", blockers: list | None = None,
                  concerns: list | None = None, revision: str | None = None) -> dict:
    """A complete factory.review/1 record, as aggregate-findings.py writes it."""
    blockers, concerns = blockers or [], concerns or []
    return {"schema": "factory.review/1", "kind": kind, "revision": revision, "diff_sha256": None,
            "expected_lenses": list(lenses), "completed_lenses": list(lenses), "missing_lenses": [],
            "invalid_results": {}, "verification": {"required": [], "missing": [], "outcomes": {}},
            "completeness": "complete", "verdict": "BLOCK" if blockers else "CONCERNS" if concerns else "PASS",
            "blockers": blockers, "concerns": concerns, "refuted": [], "nits": [], "good": {}, "tasks": {}}


SHIP_LENSES = ("correctness", "simplify", "spec-conformance", "tests")

GAUNTLET = {"schema": "factory.gauntlet/1", "revision": None, "checks": [
    {"id": "static-analysis", "applicable": True, "command": {"run": ["python3", "-m", "py_compile", "webhook.py"]},
     "tool_version": "python3 py_compile", "scope": "webhook.py", "threshold_source": "default: zero findings",
     "surviving": []},
    *({"id": check, "applicable": False, "reason": "declared not applicable in the contract"}
      for check in ("security", "dead-code", "duplication", "complexity-coverage", "flakiness", "mutation")),
    {"id": "dependency-rules", "applicable": False, "reason": "no docs/dependencies.md"},
]}


def at_head(path: str, record: dict) -> list[str]:
    """A stub `run` command writing a record whose revision is HEAD at that moment."""
    script = ("import json, subprocess, sys; record = json.loads(sys.argv[2]); "
              "record['revision'] = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True)"
              ".stdout.strip(); open(sys.argv[1], 'w').write(json.dumps(record))")
    return ["python3", "-c", script, path, json.dumps(record)]


def review_step(verdict: str = "APPROVED", deferred: str = "", index: int = 1) -> dict:
    body = (f"# Spec review {index} - webhook - 2026-09-15\n\nVerdict: {verdict}\nRounds: 1\n\n"
            f"## Refinements applied\n\n## Deferred\n{deferred}\n")
    panel = review_record(("completeness", "consistency", "feasibility", "testability"), kind="spec")
    return {"files": {f".dev/{{plan}}/spec-review_{index}.md": body,
                      f".dev/{{plan}}/spec-review_{index}.json": json.dumps(panel)}}


def build_step(*, webhook: str = WEBHOOK_PY, tests: str = TESTS_PY, scenario_map: dict | None = None) -> dict:
    return {
        "files": {
            "webhook.py": webhook,
            "tests/test_webhook.py": tests,
            "e2e/run.py": E2E_DRIVER_PY,
            ".dev/{plan}/implementation-notes.md": NOTES,
            ".dev/{plan}/scenario-map.json": json.dumps(scenario_map or SCENARIO_MAP),
        },
        "run": [["git", "add", "-A"], ["git", "commit", "--quiet", "-m", "feat(webhook): deduplicate deliveries"]],
    }


def ship_step(verdict: str = "PASS", draft: bool = False, blockers: str = "") -> dict:
    # The attempt number makes every attempt's review differ, as a real re-review would; an identical retry is
    # the no-progress case, tested on its own.
    review = f"# Review 1: webhook (ship attempt {{attempt}})\n\nVerdict: {verdict}\n\n## Blockers\n{blockers}\n"
    create = ["{gh}", "pr", "create", "--title", "Deduplicate webhooks", "--body-file", ".dev/{plan}/pr.md"]
    if draft:
        create.append("--draft")
    blocking = [{"lenses": ["security"], "file": "webhook.py", "line": 3, "title": "replay window unbounded",
                 "detail": "d", "verification": "CONFIRMED"}] if verdict == "BLOCK" else []
    concerns = [{"lenses": ["tests"], "file": "webhook.py", "line": 1, "title": "t", "detail": "d",
                 "verification": "PLAUSIBLE"}] if verdict == "CONCERNS" else []
    return {
        "files": {".dev/{plan}/review_1.md": review, ".dev/{plan}/pr.md": PR_BODY},
        "run": [
            ["git", "push", "--quiet", "origin", "HEAD"],
            ["sh", "-c", "{gh} pr list --head {branch} --json number | grep -q '\"number\"' || " + " ".join(
                f"'{part}'" for part in create)],
            at_head(".dev/{plan}/review_1.json", review_record(SHIP_LENSES, blockers=blocking, concerns=concerns)),
            at_head(".dev/{plan}/gauntlet.json", GAUNTLET),
        ],
        "result": {"draft": draft},
    }


def happy_scenario() -> dict:
    return {"scope-review": [review_step()], "build": [build_step()], "ship": [ship_step()]}


class FactoryTestCase(unittest.TestCase):
    """Isolated FACTORY_HOME, stub binaries, and a repo with a bare origin per test."""

    fast_config = {"notify": False, "stage_poll_seconds": 0.05, "heartbeat_seconds": 0.2}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="factory-test-")
        self.root = Path(os.path.realpath(self._tmp.name))
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps(self.fast_config), encoding="utf-8")
        self.log = self.root / "stub-log.jsonl"
        self.scenario_path = self.root / "scenario.json"
        self.claude_path = self.root / "claude.json"
        self.gh_state = self.root / "gh-state.json"
        self.gh_state.write_text(json.dumps({"auth": True, "prs": [], "checks": {},
                                             "default_checks": [{"name": "ci", "state": "SUCCESS", "bucket": "pass"}]}),
                                 encoding="utf-8")
        self.env = {
            "FACTORY_HOME": str(self.home),
            "FACTORY_CODEX_BIN": str(STUBS / "codex"),
            "FACTORY_CLAUDE_BIN": str(STUBS / "claude"),
            "FACTORY_GH_BIN": str(STUBS / "gh"),
            "FACTORY_STUB_LOG": str(self.log),
            "FACTORY_STUB_SCENARIO": str(self.scenario_path),
            "FACTORY_CLAUDE_SCENARIO": str(self.claude_path),
            "FACTORY_GH_STATE": str(self.gh_state),
            "FACTORY_ACLI_BIN": str(STUBS / "acli"),
            "FACTORY_ACLI_STATE": str(self.root / "acli-state.json"),
            "GIT_CONFIG_GLOBAL": str(self.root / "gitconfig"),
            "CODEX_HOME": str(self.root / "codex-home"),
            "GIT_AUTHOR_NAME": "Factory Test",
            "GIT_AUTHOR_EMAIL": "factory-test@example.com",
            "GIT_COMMITTER_NAME": "Factory Test",
            "GIT_COMMITTER_EMAIL": "factory-test@example.com",
        }
        (self.root / "gitconfig").write_text("[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n",
                                             encoding="utf-8")
        self._env_patch = mock.patch.dict(os.environ, self.env)
        self._env_patch.start()
        self.worker_messages: list[str] = []
        self._log_patch = mock.patch("runner.worker.log", side_effect=self.worker_messages.append)
        self._log_patch.start()
        self.scenario(happy_scenario())
        self.claude([scope_files()])

    def tearDown(self) -> None:
        from runner import supervise
        self._env_patch.stop()
        self._log_patch.stop()
        run_dirs = [d for d in (self.home / "runs").glob("*") if d.is_dir()]
        for run_dir in run_dirs:
            if supervise.worker_alive(run_dir):
                (run_dir / "cancel").write_text("teardown\n", encoding="utf-8")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and any(supervise.worker_alive(d) for d in run_dirs):
            time.sleep(0.05)
        self._tmp.cleanup()

    def queued_run(self, stage: str = "scope-review", plan: str = "webhook", repo: Path | None = None):
        """A run handed off after scope: worktree, committed spec, queued at `stage`, no worker."""
        from runner import worktree as wt
        from runner.model import Run
        repo = repo or make_repo(self.root, f"project-{uuid.uuid4().hex[:8]}")
        run = Run.create(self.home / "runs", repo=str(repo), request="Add idempotency", plan=plan)
        created = wt.create(repo, run.worktree, plan)
        run.data.update({"base": created.base, "base_sha": created.base_sha, "branch": created.branch})
        spec = run.plan_dir / "spec.md"
        spec.parent.mkdir(parents=True)
        spec.write_text(SPEC, encoding="utf-8")
        from runner import intent
        intent.snapshot_handoff(run)
        run.data["status"] = "queued"
        run.data["stage"] = stage
        run.data["stages"]["scope"] = {"outcome": "done", "attempts": 1, "finished_at": run.data["created_at"]}
        run.save()
        return run

    def scenario(self, data: dict) -> None:
        self.scenario_path.write_text(json.dumps(data), encoding="utf-8")

    def claude(self, launches: list) -> None:
        self.claude_path.write_text(json.dumps(launches), encoding="utf-8")

    def stub_calls(self, tool: str) -> list[dict]:
        if not self.log.exists():
            return []
        return [c for c in map(json.loads, self.log.read_text(encoding="utf-8").splitlines()) if c["tool"] == tool]

    def gh(self) -> dict:
        return json.loads(self.gh_state.read_text(encoding="utf-8"))

    def wait_for(self, predicate, timeout: float = 30, message: str = "condition") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {message}")

    def wait_status(self, run_dir: Path, statuses: tuple[str, ...], timeout: float = 60) -> dict:
        from runner import supervise
        last = {}

        def reached() -> bool:
            nonlocal last
            try:
                last = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            return last.get("status") in statuses and not supervise.worker_alive(run_dir)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if reached():
                return last
            time.sleep(0.05)
        log = (run_dir / "worker.log").read_text(encoding="utf-8") if (run_dir / "worker.log").exists() else ""
        self.fail(f"run stayed {last.get('status')} at {last.get('stage')}, wanted {statuses}\n{log[-3000:]}")
