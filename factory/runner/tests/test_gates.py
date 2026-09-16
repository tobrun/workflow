import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
import uuid
from pathlib import Path

from runner import executor, gates, records
from runner.tests.helpers import (BASE_WEBHOOK_PY, CONTRACT, E2E_DRIVER_PY, GAUNTLET, INERT_TESTS_PY, NOTES, PR_BODY,
                                  SCENARIO_MAP, SHIP_LENSES, SPEC, TESTS_PY, WEBHOOK_PY, FactoryTestCase, e2e_record, git,
                                  make_repo, review_record)

PLAN = "webhook"


class GateTestCase(FactoryTestCase):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.root)
        git(self.repo, "checkout", "--quiet", "-b", "factory/webhook")
        self.base_sha = git(self.repo, "rev-parse", "HEAD")
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.plan_dir = self.repo / ".dev" / PLAN
        self.plan_dir.mkdir(parents=True)
        from runner import worktree
        worktree.install_excludes(self.repo)

    def ctx(self, stage: str, **overrides) -> gates.GateContext:
        values = dict(worktree=self.repo, plan=PLAN, stage=stage, report_dir=self.reports,
                      attempt_dir=self.root / "attempt", branch="factory/webhook", base_sha=self.base_sha,
                      poll_seconds=0.01, sleep=lambda s: None)
        values.update(overrides)
        return gates.GateContext(**values)

    def write(self, relative: str, text: str) -> Path:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def commit_all(self, message: str = "work") -> None:
        git(self.repo, "add", "-A")
        if git(self.repo, "status", "--porcelain", "--untracked-files=no"):
            git(self.repo, "commit", "--quiet", "-m", message)

    def seal(self) -> None:
        """Seal the approved intent from the current spec and contract, as the scope handoff does."""
        from runner import intent
        self.run_dir = self.root / "run"
        self.approved = intent.seal(self.run_dir, run_id="gate-test", spec=self.plan_dir / "spec.md",
                                    contract=self.repo / ".factory" / "contract.json", request_file=None,
                                    request="Add idempotency", base_sha=self.base_sha)

    def sealed(self) -> dict:
        return {"run_dir": self.run_dir, "intent_sha256": self.approved["content_sha256"], "require_intent": True}

    def result(self, stage: str, **fields) -> None:
        payload = {"schema": 2, "stage": stage, "status": "done", "reason": "ok", "conditions": []}
        payload.update(fields)
        self.write(f".dev/{PLAN}/{stage}-result.json", json.dumps(payload))


class ScopeGateTests(GateTestCase):
    def test_absent_spec_is_a_precondition_failure(self):
        gate = gates.scope_gate(self.ctx("scope"))
        self.assertFalse(gate.passed)
        self.assertTrue(gate.precondition)
        self.assertFalse(gate.retryable)

    def test_clean_spec_passes(self):
        self.write(f".dev/{PLAN}/spec.md", SPEC)
        gate = gates.scope_gate(self.ctx("scope"))
        self.assertTrue(gate.passed, gate.failures)

    def test_flag_and_open_markers_are_each_reported(self):
        spec = SPEC.replace("## Scope", "D-cache: Cache responses? [open]\n  ? redis - fast\n  ⚑ ask: sizes?\n\n## Scope")
        self.write(f".dev/{PLAN}/spec.md", spec)
        gate = gates.scope_gate(self.ctx("scope"))
        self.assertFalse(gate.passed)
        joined = "\n".join(gate.failures)
        self.assertIn("⚑", joined)
        self.assertIn("[open]", joined)

    def test_lint_failure_is_reported(self):
        self.write(f".dev/{PLAN}/spec.md", SPEC.replace("### Validation", "### Nothing"))
        gate = gates.scope_gate(self.ctx("scope"))
        self.assertTrue(any("lint-spec.py exited 1" in f for f in gate.failures), gate.failures)

    def test_result_must_say_done(self):
        self.write(f".dev/{PLAN}/spec.md", SPEC)
        self.result("scope", status="blocked", reason="user left")
        gate = gates.scope_gate(self.ctx("scope"))
        self.assertIn("scope-result.json says blocked: user left", gate.failures)

    def test_malformed_result_fails(self):
        self.write(f".dev/{PLAN}/spec.md", SPEC)
        self.write(f".dev/{PLAN}/scope-result.json", "{nope")
        self.assertFalse(gates.scope_gate(self.ctx("scope")).passed)

    def test_paths_outside_the_contract_fail(self):
        self.write(f".dev/{PLAN}/spec.md", SPEC)
        self.write("src/app.py", "print('scope should not write code')\n")
        gate = gates.scope_gate(self.ctx("scope"))
        self.assertTrue(any("src/app.py" in f for f in gate.failures))


class ScopeReviewGateTests(GateTestCase):
    def setUp(self):
        super().setUp()
        self.write(f".dev/{PLAN}/spec.md", SPEC)
        self.commit_all("spec")

    def review(self, verdict: str, deferred: str = "", index: int = 1, panel: dict | None = None) -> None:
        self.write(f".dev/{PLAN}/spec-review_{index}.md",
                   f"# Spec review {index}\n\nVerdict: {verdict}\n\n## Deferred\n{deferred}\n## Strengths\n")
        record = panel if panel is not None else review_record(records.SPEC_REVIEW_LENSES, kind="spec")
        self.write(f".dev/{PLAN}/spec-review_{index}.json", json.dumps(record))

    def test_an_incomplete_spec_review_panel_fails(self):
        incomplete = {**review_record(records.SPEC_REVIEW_LENSES[:2], kind="spec"), "completeness": "incomplete",
                      "verdict": None, "missing_lenses": list(records.SPEC_REVIEW_LENSES[2:]),
                      "expected_lenses": list(records.SPEC_REVIEW_LENSES)}
        self.review("APPROVED", panel=incomplete)
        gate = gates.scope_review_gate(self.ctx("scope-review", baseline={"spec_review_index": 0}))
        self.assertEqual(gate.code, "review.incomplete")
        self.assertIn("the review is incomplete (missing lenses: consistency, testability", gate.reason)
        self.review("APPROVED", panel=review_record(("feasibility", "completeness", "consistency"), kind="spec"))
        gate = gates.scope_review_gate(self.ctx("scope-review", baseline={"spec_review_index": 0}))
        self.assertIn("mandatory lenses without a valid result: testability", gate.reason)

    def test_no_new_review_index_fails(self):
        self.review("APPROVED", index=1)
        gate = gates.scope_review_gate(self.ctx("scope-review", baseline={"spec_review_index": 1}))
        self.assertEqual(gate.outcome, "failed")
        self.assertIn("no new spec-review_N.md", gate.reason)

    def test_approved_passes(self):
        self.review("APPROVED")
        gate = gates.scope_review_gate(self.ctx("scope-review", baseline={"spec_review_index": 0}))
        self.assertTrue(gate.passed, gate.reason)
        self.assertEqual(gate.data["spec_review"], f".dev/{PLAN}/spec-review_1.md")

    def test_unparseable_verdict(self):
        self.review("LOOKS GOOD")
        gate = gates.scope_review_gate(self.ctx("scope-review"))
        self.assertIn("no parseable Verdict", gate.reason)
        self.assertTrue(gate.retryable)

    def test_deferral_kinds(self):
        cases = {
            "premise": (False, "human must decide"),
            "new-effort": (False, "human must decide"),
            "unanswered": (True, "unanswered question"),
        }
        for kind, (retryable, text) in cases.items():
            with self.subTest(kind=kind):
                self.review("APPROVED WITH DEFERRALS", f"### D1 - feasibility - storage\nKind: {kind}\nWhich store?\n")
                gate = gates.scope_review_gate(self.ctx("scope-review"))
                self.assertFalse(gate.passed)
                self.assertEqual(gate.retryable, retryable)
                self.assertIn(text, gate.reason)

    def test_mixed_kinds_are_non_retryable(self):
        self.review("APPROVED WITH DEFERRALS",
                    "### D1 - a - one\nKind: unanswered\n\n### D2 - b - two\nKind: premise\n")
        self.assertFalse(gates.scope_review_gate(self.ctx("scope-review")).retryable)

    def test_missing_or_invalid_kind(self):
        for body in ("### D1 - a - one\nno kind here\n", "### D1 - a - one\nKind: maybe\n"):
            with self.subTest(body=body):
                self.review("APPROVED WITH DEFERRALS", body)
                gate = gates.scope_review_gate(self.ctx("scope-review"))
                self.assertIn("without a valid 'Kind", gate.reason)

    def test_lint_must_still_pass(self):
        self.review("APPROVED")
        self.write(f".dev/{PLAN}/spec.md", SPEC.replace("### Validation", "### Missing"))
        self.assertIn("lint-spec.py", gates.scope_review_gate(self.ctx("scope-review")).reason)

    def test_unexpected_paths_fail_with_list(self):
        self.review("APPROVED")
        self.write("src/code.py", "x = 1\n")
        gate = gates.scope_review_gate(self.ctx("scope-review"))
        self.assertIn("src/code.py", gate.reason)

    def test_plan_files_are_invisible_to_git(self):
        self.review("APPROVED")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(gates.scope_commit_paths(self.ctx("scope")), ([], []))

    def test_ledger_edits_are_allowed(self):
        self.review("APPROVED")
        self.write("docs/decisions.md", "# Decisions\n")
        self.assertTrue(gates.scope_review_gate(self.ctx("scope-review")).passed)


class BuildGateTests(GateTestCase):
    """Build completes only on evidence the runner executed itself: mapped tests, reproductions, and the e2e driver."""

    def setUp(self):
        super().setUp()
        self.write(f".dev/{PLAN}/spec.md", SPEC)
        self.commit_all("spec")
        self.start = git(self.repo, "rev-parse", "HEAD")
        self.seal()

    def build(self, *, webhook: str = WEBHOOK_PY, tests: str = TESTS_PY, driver: str = E2E_DRIVER_PY,
              mapping: dict | None = None) -> None:
        self.write("webhook.py", webhook)
        self.write("tests/test_webhook.py", tests)
        self.write("e2e/run.py", driver)
        self.write(f".dev/{PLAN}/implementation-notes.md", NOTES)
        self.write(f".dev/{PLAN}/scenario-map.json", json.dumps(mapping or SCENARIO_MAP))
        self.commit_all("build")

    def gate(self, **overrides) -> gates.GateResult:
        values = {**self.sealed(), "baseline": {"build_start_sha": self.start, "head": self.start}, **overrides}
        return gates.build_gate(self.ctx("build", **values))

    def test_happy_path(self):
        self.build()
        gate = self.gate()
        self.assertTrue(gate.passed, gate.reason)
        self.assertEqual(gate.data["tests"]["scenarios"],
                         {"S1": {"tests/test_webhook.py::WebhookTests::test_repeated_id_ignored": "passed"}})
        self.assertEqual(gate.data["e2e"]["coverage"], {"S2": "passed"})
        self.assertEqual(gate.data["e2e"]["counts"], {"total": 1, "passed": 1, "failed": 0})
        self.assertEqual(gate.data["e2e"]["revision"], git(self.repo, "rev-parse", "HEAD"))
        published = records.load_e2e(self.reports / f"{PLAN}-e2e.json")
        self.assertEqual(published["scenarios"][0]["states"][0]["after"], {"processed": 1})
        self.assertIn("render-e2e.py replaces this block", (self.reports / f"{PLAN}-e2e-report.html").read_text()
                      .replace("generated by render-e2e.py", "render-e2e.py replaces this block"))
        self.assertEqual([(v["id"], v["argv"], v["classification"]) for v in gate.data["validation"]],
                         [("readme", ["test", "-f", "README.md"], "succeeded")])

    def test_no_commits(self):
        self.assertIn("no commits after", self.gate().reason)

    def test_dirty_tree(self):
        self.build()
        self.write("README.md", "changed\n")
        self.assertIn("tracked worktree is dirty", self.gate().reason)

    def test_r1_names_in_comments_and_an_empty_e2e_record_are_not_evidence(self):
        comment_only = "# test_repeated_id_ignored: WebhookTests covers repeated ids\n"
        empty_driver = E2E_DRIVER_PY.replace('"scenarios": [{', '"scenarios": [], "unused": [{')
        empty_driver = empty_driver.replace('"schema": "factory.e2e/1"', '"schema": "factory.e2e/1"').replace(
            ', "unused": [{', ', "_": [{')
        self.build(tests=comment_only)
        gate = self.gate()
        self.assertEqual(gate.code, "evidence.tests_failed")
        self.assertEqual(gate.reason, "S1: tests/test_webhook.py::WebhookTests::test_repeated_id_ignored was not "
                                      "executed (no such test, or not collected)")
        driver = ("import json, os\n"
                  "record = {'schema': 'factory.e2e/1', 'plan': 'webhook', 'kind': 'non-frontend',\n"
                  "          'revision': os.environ['FACTORY_REVISION'], 'scenarios': []}\n"
                  "json.dump(record, open(os.path.join(os.environ['FACTORY_E2E_OUT'], 'e2e.json'), 'w'))\n")
        self.build(driver=driver)
        gate = self.gate()
        self.assertEqual((gate.code, gate.reason), ("e2e.failed", "S2: the driver recorded no case 'duplicate-delivery'"))

    def test_the_scenario_map_must_cover_every_scenario_exactly(self):
        self.build()
        (self.plan_dir / "scenario-map.json").unlink()
        self.assertEqual(self.gate().code, "evidence.map_missing")
        cases = [
            ({"S1": SCENARIO_MAP["scenarios"]["S1"]}, "scenarios.S2: is not mapped ([e2e] duplicate delivery -> processed once)"),
            ({**SCENARIO_MAP["scenarios"], "S9": {"layer": "unit", "tests": ["a::b"]}}, "scenarios.S9: is not a scenario"),
            ({**SCENARIO_MAP["scenarios"], "S1": {"layer": "e2e", "e2e_case": "x"}}, "scenarios.S1.layer: must be 'unit'"),
            ({**SCENARIO_MAP["scenarios"], "S1": {"layer": "unit", "tests": ["tests/test_webhook.py::WebhookTests::"
                                                                          "test_repeated_id_ignored"] * 2}},
             "lists the same test more than once"),
        ]
        for scenarios, fragment in cases:
            with self.subTest(fragment):
                self.write(f".dev/{PLAN}/scenario-map.json", json.dumps({"schema": "factory.scenario-map/1",
                                                                          "scenarios": scenarios}))
                gate = self.gate()
                self.assertEqual(gate.code, "evidence.map_invalid")
                self.assertIn(fragment, gate.reason)

    def test_skipped_failing_and_wrong_layer_tests_are_rejected(self):
        skipped = TESTS_PY.replace("    def test_repeated_id_ignored", "    @unittest.skip('later')\n"
                                                                       "    def test_repeated_id_ignored")
        self.build(tests=skipped)
        self.assertIn("S1: tests/test_webhook.py::WebhookTests::test_repeated_id_ignored was skipped", self.gate().reason)
        self.build(webhook=BASE_WEBHOOK_PY)
        gate = self.gate()
        self.assertEqual(gate.code, "evidence.tests_failed")
        self.assertIn("S1: tests/test_webhook.py::WebhookTests::test_repeated_id_ignored failed: AssertionError: "
                      "'processed' != 'ignored'", gate.reason)
        self.write("e2e/test_webhook.py", TESTS_PY)
        mapping = {"schema": "factory.scenario-map/1", "scenarios": {
            **SCENARIO_MAP["scenarios"], "S1": {"layer": "unit", "tests": ["e2e/test_webhook.py::WebhookTests::"
                                                                          "test_repeated_id_ignored"]}}}
        self.build(mapping=mapping)
        gate = self.gate()
        self.assertEqual(gate.code, "evidence.wrong_layer")
        self.assertIn("but the contract's unit tests live in tests/*.py", gate.reason)

    def test_results_come_from_a_fresh_run_every_time(self):
        self.build()
        self.assertTrue(self.gate().passed)
        self.build(tests=TESTS_PY.replace("test_repeated_id_ignored", "test_renamed"))
        gate = self.gate()
        self.assertIn("was not executed", gate.reason)
        runs = sorted((self.root / "attempt" / "gate").glob("*-tests"))
        self.assertEqual(len(runs), 2)

    def test_a_broken_implementation_fails_despite_a_green_looking_agent_report(self):
        self.build(webhook=BASE_WEBHOOK_PY)
        (self.reports / f"{PLAN}-e2e.json").write_text(e2e_record(plan=PLAN))
        self.result("build", status="done", reason="all green")
        gate = self.gate()
        self.assertFalse(gate.passed)
        self.assertEqual(gate.code, "evidence.tests_failed")
        mapping = {"schema": "factory.scenario-map/1", "scenarios": {"S1": {"layer": "unit", "tests": [
            "tests/test_webhook.py::WebhookTests::test_repeated_id_ignored"]}, "S2": SCENARIO_MAP["scenarios"]["S2"]}}
        self.build(webhook=BASE_WEBHOOK_PY, tests=INERT_TESTS_PY, mapping=mapping)
        gate = self.gate()
        self.assertEqual(gate.code, "e2e.failed")
        self.assertIn("S2: case 'duplicate-delivery' failed", gate.reason)

    def test_e2e_evidence_is_checked_against_the_revision_and_its_files(self):
        stale = E2E_DRIVER_PY.replace('os.environ["FACTORY_REVISION"]', '"0" * 40')
        self.build(driver=stale)
        self.assertIn("the e2e record names revision '0000000000000000000000000000000000000000', not the tested",
                      self.gate().reason)
        forged = E2E_DRIVER_PY.replace('"states": [', '"screenshots": [{"step": "s", "file": "shot.png", '
                                                     '"sha256": "' + "0" * 64 + '"}], "states": [').replace(
            'with open(os.path.join(out, "e2e.json")', 'open(os.path.join(out, "shot.png"), "wb").write(b"png")\n'
                                                      'with open(os.path.join(out, "e2e.json")')
        self.build(driver=forged)
        self.assertIn("evidence file shot.png does not match its sha256", self.gate().reason)
        stateless = E2E_DRIVER_PY.replace('"states": [{"step": "deliver d1 twice", "entity": "processed deliveries", '
                                          '"before": before, "after": after}],', "")
        self.build(driver=stateless)
        self.assertIn("S2: non-frontend case 'duplicate-delivery' captured no before/after state", self.gate().reason)
        self.build(driver="import sys\nprint('driver crashed')\nsys.exit(3)\n")
        gate = self.gate()
        self.assertEqual(gate.code, "e2e.missing")
        self.assertIn("the e2e driver exited 3 and wrote no factory.e2e/1 record", gate.reason)

    def test_malformed_result_fails_gate(self):
        self.build()
        self.write(f".dev/{PLAN}/build-result.json", json.dumps({"schema": 1, "stage": "ship"}))
        self.assertIn("schema 1 results are no longer read", self.gate().reason)
        self.write(f".dev/{PLAN}/build-result.json", json.dumps({"schema": 2, "stage": "ship"}))
        self.assertIn("build-result.json is malformed", self.gate().reason)


class ReproTests(GateTestCase):
    """A [repro] scenario's tests must fail on the base on an assertion and pass on the change."""

    def setUp(self):
        super().setUp()
        self.write(f".dev/{PLAN}/spec.md", SPEC.replace("[unit] repeated id", "[unit] [repro] repeated id"))
        self.commit_all("spec")
        self.start = git(self.repo, "rev-parse", "HEAD")
        self.seal()

    def build(self, tests: str, webhook: str = WEBHOOK_PY) -> gates.GateResult:
        self.write("webhook.py", webhook)
        self.write("tests/test_webhook.py", tests)
        self.write("e2e/run.py", E2E_DRIVER_PY)
        self.write(f".dev/{PLAN}/scenario-map.json", json.dumps(SCENARIO_MAP))
        self.commit_all("fix")
        values = {**self.sealed(), "baseline": {"build_start_sha": self.start, "head": self.start}}
        return gates.build_gate(self.ctx("build", **values))

    def test_a_real_reproduction_is_red_on_base_and_green_on_the_change(self):
        gate = self.build(TESTS_PY)
        self.assertTrue(gate.passed, gate.reason)
        self.assertEqual(gate.data["repro"]["outcomes_on_base"],
                         {"tests/test_webhook.py::WebhookTests::test_repeated_id_ignored": "failed"})
        self.assertEqual(git(self.repo, "worktree", "list").count("\n"), 0)

    def test_an_inert_or_setup_failing_reproduction_is_rejected(self):
        gate = self.build(TESTS_PY.replace("self.assertEqual(hooks.deliver(\"d1\", {}), \"ignored\")", "pass")
                          .replace("self.assertEqual(hooks.processed, [\"d1\"])", "pass"))
        self.assertEqual(gate.code, "evidence.repro")
        self.assertIn("passes on the base", gate.reason)
        gate = self.build(TESTS_PY.replace("from webhook import Webhooks", "from webhook import Webhooks, DEDUP_WINDOW"),
                          webhook=WEBHOOK_PY + "\n\nDEDUP_WINDOW = 60\n")
        self.assertIn("is error on the base", gate.reason)
        self.assertIn("a setup failure, not a reproduction", gate.reason)


class BoundaryTests(GateTestCase):
    """The complete Git delta of an attempt, judged against the stage's path contract."""

    def setUp(self):
        super().setUp()
        self.write("src/app.py", "print('app')\n")
        self.write("src/old_name.py", "x = 1\n")
        self.write("docs/decisions.md", "# Decisions\n")
        self.commit_all("baseline")
        self.head = git(self.repo, "rev-parse", "HEAD")

    def check(self, stage: str) -> gates.GateResult | None:
        return gates.boundary(self.ctx(stage, baseline={"head": self.head}))

    def assertViolation(self, stage: str, fragment: str, *, retryable: bool) -> None:
        gate = self.check(stage)
        self.assertIsNotNone(gate, f"{stage} should violate: {fragment}")
        self.assertIn(fragment, gate.reason)
        self.assertEqual((gate.code, gate.retryable), ("boundary.violation", retryable))

    def test_r3_a_committed_unauthorized_source_file_is_named(self):
        self.write("src/rogue.py", "print('unauthorized')\n")
        self.commit_all("rogue")
        self.assertViolation("scope-review", "scope-review changed paths outside its contract: src/rogue.py "
                             "(added, committed)", retryable=False)

    def test_every_kind_of_uncommitted_change_is_seen(self):
        cases = {
            "staged": lambda: (self.write("src/staged.py", "s\n"), git(self.repo, "add", "src/staged.py")),
            "unstaged": lambda: self.write("src/app.py", "changed\n"),
            "deleted": lambda: (self.repo / "src" / "app.py").unlink(),
            "untracked": lambda: self.write("src/new.py", "n\n"),
        }
        expected = {"staged": "src/staged.py (added)", "unstaged": "src/app.py (modified)",
                    "deleted": "src/app.py (deleted)", "untracked": "src/new.py (untracked)"}
        for name, change in cases.items():
            with self.subTest(name):
                change()
                self.assertViolation("scope-review", expected[name], retryable=True)
                git(self.repo, "reset", "--quiet", "--hard", self.head)
                git(self.repo, "clean", "-fdq", "src")
                self.assertIsNone(self.check("scope-review"))

    def test_both_sides_of_a_rename(self):
        git(self.repo, "mv", "src/old_name.py", "src/new_name.py")
        self.commit_all("rename")
        gate = self.check("scope-review")
        self.assertIn("src/new_name.py (added, committed)", gate.reason)
        self.assertIn("src/old_name.py (deleted, committed)", gate.reason)

    def test_branch_switch_and_history_replacement(self):
        git(self.repo, "checkout", "--quiet", "-b", "elsewhere")
        self.assertViolation("build", "the worktree is on elsewhere, not the run branch factory/webhook",
                             retryable=False)
        git(self.repo, "checkout", "--quiet", "factory/webhook")
        git(self.repo, "reset", "--quiet", "--hard", self.base_sha)
        self.write("src/other.py", "o\n")
        self.commit_all("replacement history")
        self.assertViolation("build", f"history was rewritten: {self.head[:12]} is no longer an ancestor of HEAD",
                             retryable=False)
        git(self.repo, "checkout", "--quiet", "--detach")
        self.assertIn("a detached HEAD", self.check("build").reason)

    def test_force_added_plan_files_are_rejected(self):
        self.write(f".dev/{PLAN}/spec.md", SPEC)
        git(self.repo, "add", "-f", f".dev/{PLAN}/spec.md")
        self.assertViolation("build", f"plan files are now tracked by Git: .dev/{PLAN}/spec.md", retryable=False)
        self.commit_all("tracked plan")
        self.assertViolation("ship", f"plan files are now tracked by Git: .dev/{PLAN}/spec.md", retryable=False)

    def test_other_plans_tracked_on_the_base_are_left_alone(self):
        self.write(".dev/old/spec.md", "# old\n")
        git(self.repo, "add", "-f", ".dev/old/spec.md")
        self.commit_all("tracked plan on the base")
        base = git(self.repo, "rev-parse", "HEAD")

        def check(stage):
            return gates.boundary(self.ctx(stage, base_sha=base, baseline={"head": base}))

        self.write("src/app.py", "print('built')\n")
        self.write(f".dev/{PLAN}/implementation-notes.md", "notes\n")
        self.assertIsNone(check("build"))
        self.write(".dev/old/spec.md", "# rewritten\n")
        gate = check("build")
        self.assertIn("build changed plan files the base tracks: .dev/old/spec.md", gate.reason)
        self.assertTrue(gate.retryable)
        self.commit_all("touch the old plan")
        gate = check("ship")
        self.assertIn("ship changed plan files the base tracks: .dev/old/spec.md", gate.reason)
        self.assertFalse(gate.retryable)

    def test_the_contract_is_locked_after_scope(self):
        self.write(".factory/contract.json", json.dumps({"schema": "factory.repo-contract/1", "ci": {"none": "probe"},
                                                        "validation": [{"id": "true", "run": ["true"]}]}))
        self.commit_all("weaken validation")
        self.assertViolation("build", ".factory/contract.json (modified, committed)", retryable=False)
        self.assertIsNone(gates.boundary(self.ctx("scope", baseline={"head": self.head})))

    def test_legitimate_changes_pass(self):
        self.write("docs/decisions.md", "# Decisions\n\nD-x: y\n")
        self.write(f".dev/{PLAN}/spec-review_1.md", "Verdict: APPROVED\n")
        self.assertIsNone(self.check("scope-review"))
        self.commit_all("ledger")
        self.assertIsNone(self.check("scope-review"))
        self.write("src/app.py", "print('built')\n")
        self.write("tests/test_app.py", "def test_app():\n    assert True\n")
        self.commit_all("build")
        self.assertIsNone(self.check("build"))
        self.write("docs/architecture.md", "# Architecture\n")
        after_build = git(self.repo, "rev-parse", "HEAD")
        self.assertIsNone(gates.boundary(self.ctx("scope", baseline={"head": after_build})))

    @unittest.skipUnless(sys.platform == "darwin" or shutil.which("bwrap"), "no sandbox enforcement here")
    def test_narrowed_git_grants_commit_and_fetch_but_protect_runner_owned_git_files(self):
        from runner import commands
        from runner import worktree as wt
        home = Path.home() / f".factory-git-grants-{uuid.uuid4().hex[:8]}"
        home.mkdir()
        self.addCleanup(shutil.rmtree, home, True)
        repo = make_repo(home, "project")
        linked = home / "linked"
        git(repo, "worktree", "add", "--quiet", "-b", "factory/probe", str(linked), "main")
        wt.configure_upstream(linked, "origin", "factory/probe")
        (wt.common_dir(repo) / "logs").mkdir(exist_ok=True)
        grants = wt.sandbox_git_dirs(linked)

        def sandboxed(*argv: str) -> subprocess.CompletedProcess:
            wrapped = commands.sandbox_wrap(list(argv), "workspace-write", writable=[linked, *grants])
            return subprocess.run(wrapped, cwd=linked, capture_output=True, text=True)

        (linked / "feature.py").write_text("x = 1\n")
        self.assertEqual(sandboxed("git", "add", "feature.py").returncode, 0)
        commit = sandboxed("git", "commit", "--quiet", "-m", "feature")
        self.assertEqual(commit.returncode, 0, commit.stderr)
        self.assertEqual(sandboxed("git", "fetch", "--quiet", "origin").returncode, 0)
        self.assertEqual(git(linked, "log", "-1", "--format=%s"), "feature")
        common = wt.common_dir(repo)
        denied = [
            sandboxed("git", "config", "core.fsmonitor", "touch /tmp/factory-pwned"),
            sandboxed("sh", "-c", f"echo evil > {common}/hooks/pre-commit"),
            sandboxed("sh", "-c", f"echo '!.dev/' >> {common}/info/exclude"),
        ]
        self.assertTrue(all(result.returncode != 0 for result in denied), [r.stderr for r in denied])
        self.assertEqual(git(linked, "config", "--default", "unset", "core.fsmonitor"), "unset")
        self.assertFalse((common / "hooks" / "pre-commit").exists())

    @unittest.skipUnless(os.environ.get("FACTORY_REAL_CODEX_SANDBOX") == "1" and shutil.which("codex"),
                         "opt-in: FACTORY_REAL_CODEX_SANDBOX=1 runs `codex sandbox` (no model call)")
    def test_real_codex_sandbox_denies_writes_to_runner_records_and_git_control_files(self):
        home = Path.home() / f".factory-codex-sandbox-{uuid.uuid4().hex[:8]}"
        (home / "run").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, home, True)
        repo = make_repo(home, "project")
        linked = home / "run" / "worktree"
        git(repo, "worktree", "add", "--quiet", "-b", "factory/probe", str(linked), "main")
        (home / "run" / "run.json").write_text("{}")
        common = repo / ".git"
        env = {k: v for k, v in os.environ.items() if not k.startswith("FACTORY_")}

        def sandboxed(script: str) -> int:
            return subprocess.run(["codex", "sandbox", "-P", ":workspace", "-C", str(linked), "--", "sh", "-c", script],
                                  capture_output=True, text=True, env=env).returncode

        self.assertEqual(sandboxed("echo ok > inside.txt"), 0)
        for script in (f"echo tampered > {home}/run/run.json", f"echo x > {home}/run/cancel",
                       "git config core.fsmonitor 'touch /tmp/factory-pwned'",
                       f"echo evil > {common}/hooks/pre-commit", f"echo x >> {common}/info/exclude"):
            self.assertNotEqual(sandboxed(script), 0, script)
        self.assertEqual((home / "run" / "run.json").read_text(), "{}")
        self.assertFalse((home / "run" / "cancel").exists())


class ValidationTests(GateTestCase):
    """Contract validation commands run guarded, under one attempt deadline, in their declared boundary."""

    def setUp(self):
        super().setUp()
        self.write(f".dev/{PLAN}/spec.md", SPEC)
        self.commit_all("spec")
        self.start = git(self.repo, "rev-parse", "HEAD")
        self.write("webhook.py", WEBHOOK_PY)
        self.write("tests/test_webhook.py", TESTS_PY)
        self.write("e2e/run.py", E2E_DRIVER_PY)
        self.write(f".dev/{PLAN}/scenario-map.json", json.dumps(SCENARIO_MAP))
        self.head = self.start
        self.seal()

    def contract(self, *commands: dict, **extra) -> None:
        """Replace and re-approve the contract before the build attempt starts; build itself may not touch it."""
        self.write(".factory/contract.json", json.dumps({**CONTRACT, "validation": list(commands), **extra}))
        self.commit_all("contract")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.seal()

    def gate(self, **overrides) -> gates.GateResult:
        values = {**self.sealed(), "baseline": {"build_start_sha": self.start, "head": self.head}, **overrides}
        return gates.build_gate(self.ctx("build", **values))

    def test_r6_nothing_starts_after_cancel_or_deadline(self):
        marker = self.root / "validation-ran"
        self.contract({"id": "touch", "run": ["touch", str(marker)]})
        gate = self.gate(cancel_requested=lambda: True)
        self.assertEqual(gate.reason, "cancelled by operator before the build gate")
        self.assertFalse(gate.retryable)
        gate = self.gate(deadline=time.time() - 1)
        self.assertEqual(gate.reason, "the attempt deadline passed before the build gate")
        self.assertFalse(marker.exists())
        ctx = self.ctx("build", baseline={"build_start_sha": self.start, "head": self.head}, **self.sealed())
        ctx.cancel_requested = lambda: any(e["label"] == "e2e" for e in ctx.executions)
        gate = gates.build_gate(ctx)
        self.assertEqual(gate.reason, "cancelled by operator before validation command touch")
        self.assertFalse(marker.exists())

    def test_cancelling_active_validation_stops_the_command_and_its_descendants(self):
        script = ("sleep 60 &\necho $! > \"$PID_FILE\"\nwait\n")
        self.write("scripts/slow.sh", script)
        pid_file = self.root / "descendant.pid"
        self.contract({"id": "slow", "script": "scripts/slow.sh", "set": {"PID_FILE": str(pid_file)}})
        cancel = self.root / "cancel"
        threading.Timer(1.0, lambda: cancel.write_text("x")).start()
        started = time.monotonic()
        gate = self.gate(cancel_file=cancel, grace=1)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(gate.reason, "cancelled by operator during validation command slow")
        descendant = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while executor.process_identity(descendant)[0] and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(executor.process_identity(descendant)[0])

    def test_commands_share_one_deadline_instead_of_each_getting_their_own(self):
        marker = self.root / "second-ran"
        self.contract({"id": "first", "run": ["sleep", "3"], "timeout_s": 1200},
                      {"id": "second", "run": ["touch", str(marker)], "timeout_s": 1200})
        started = time.monotonic()
        gate = self.gate(deadline=time.time() + 1.5, grace=1)
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(gate.reason, "the attempt deadline passed during validation command first")
        self.assertTrue(gate.retryable)
        self.assertFalse(marker.exists())

    def test_command_cap_failure_output_and_receipts_stay_on_disk(self):
        self.contract({"id": "loud", "run": ["sh", "-c", "echo checking widgets; echo widget 3 broke >&2; exit 4"]})
        gate = self.gate()
        self.assertEqual(gate.reason,
                         "Validation command loud (sh -c 'echo checking widgets; echo widget 3 broke >&2; exit 4') "
                         "exited 4: checking widgets; widget 3 broke")
        summary = gate.data["validation"][0]
        receipt = json.loads(Path(summary["receipt"]).read_text())
        self.assertEqual((receipt["classification"], receipt["exit_code"], receipt["kind"]), ("failed", 4, "validation"))
        self.assertIn("checking widgets", (Path(summary["receipt"]).parent / "stdout.log").read_text())
        self.contract({"id": "slow", "run": ["sh", "-c", "echo started; sleep 30"], "timeout_s": 1})
        started = time.monotonic()
        gate = self.gate(grace=1)
        self.assertLess(time.monotonic() - started, 8)
        self.assertIn("Validation command slow (sh -c 'echo started; sleep 30') timed out: started", gate.reason)

    def test_a_script_keeps_directory_exports_and_multiline_control_flow(self):
        self.write("tools/check.sh", """set -e
cd sub
export GREETING=hello
if [ "$(cat value.txt)" = "expected" ] &&
   [ -n "$GREETING" ]; then
  printf '%s from %s\\n' "$GREETING" \\
    "$(basename "$PWD")" > ../result.txt
else
  echo "unexpected value" >&2
  exit 3
fi
""")
        self.write("sub/value.txt", "expected")
        self.contract({"id": "check", "script": "tools/check.sh"})
        gate = self.gate()
        self.assertTrue(gate.passed, gate.reason)
        self.assertEqual((self.repo / "result.txt").read_text(), "hello from sub\n")
        (self.repo / "result.txt").unlink()
        self.write("sub/value.txt", "other")
        self.commit_all("other")
        gate = self.gate()
        self.assertIn("Validation command check (sh", gate.reason)
        self.assertIn("exited 3: unexpected value", gate.reason)

    def test_declared_environment_only(self):
        probe = self.root / "env.txt"
        self.contract({"id": "env", "run": ["sh", "-c", f"env > {probe}"], "env": ["FACTORY_TEST_INPUT"],
                       "set": {"CI": "1"}})
        base = {**os.environ, "FACTORY_TEST_UNDECLARED": "secret"}
        gate = self.gate(base_env=base)
        self.assertEqual(gate.reason, "validation command env cannot run: command env needs environment variable(s) "
                         "FACTORY_TEST_INPUT; repair: export FACTORY_TEST_INPUT before `factory resume`, or declare "
                         "them optional in .factory/contract.json")
        self.assertFalse(gate.retryable)
        gate = self.gate(base_env={**base, "FACTORY_TEST_INPUT": "value"})
        self.assertTrue(gate.passed, gate.reason)
        seen = probe.read_text()
        self.assertIn("FACTORY_TEST_INPUT=value", seen)
        self.assertIn("CI=1", seen)
        self.assertNotIn("FACTORY_TEST_UNDECLARED", seen)

    @unittest.skipUnless(sys.platform == "darwin" or shutil.which("bwrap"), "no workspace-write enforcement here")
    def test_workspace_write_boundary_limits_writes_to_the_worktree(self):
        outside = Path.home() / f".factory-boundary-probe-{uuid.uuid4().hex}"
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        self.contract({"id": "inside", "run": ["sh", "-c", "echo ok > inside.txt"]},
                      {"id": "outside", "run": ["sh", "-c", f"echo leak > {outside}"]}, boundary="workspace-write")
        gate = self.gate()
        self.assertIn("Validation command outside", gate.reason)
        self.assertEqual((self.repo / "inside.txt").read_text(), "ok\n")
        self.assertFalse(outside.exists())
        self.assertEqual([v["boundary"] for v in gate.data["validation"]], ["workspace-write", "workspace-write"])
        (self.repo / "inside.txt").unlink()
        self.contract({"id": "outside", "run": ["sh", "-c", f"echo host > {outside}"], "boundary": "host"})
        self.assertTrue(self.gate().passed)
        self.assertTrue(outside.exists())

    def test_missing_or_malformed_contract_is_not_retryable(self):
        git(self.repo, "rm", "--quiet", ".factory/contract.json")
        self.commit_all("drop contract")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.seal()
        gate = self.gate()
        self.assertIn("contract.json does not exist [record.missing]", gate.reason)
        self.assertFalse(gate.retryable)
        self.contract({"id": "bad", "run": "npm test"})
        gate = self.gate()
        self.assertIn("must be a non-empty list of non-empty strings", gate.reason)
        self.assertFalse(gate.retryable)


class ShipGateCase(GateTestCase):
    """A shipped branch with a sealed intent, the real fixture application, and helpers to publish it."""

    def setUp(self):
        super().setUp()
        self.write(f".dev/{PLAN}/spec.md", SPEC)
        self.write("webhook.py", WEBHOOK_PY)
        self.write("tests/test_webhook.py", TESTS_PY)
        self.write("e2e/run.py", E2E_DRIVER_PY)
        self.write(f".dev/{PLAN}/scenario-map.json", json.dumps(SCENARIO_MAP))
        self.commit_all("build")
        git(self.repo, "push", "--quiet", "-u", "origin", "factory/webhook")
        self.seal()

    def ship(self, verdict: str = "PASS", draft: bool = False, checks=None, blockers: str = "",
             base: str = "main") -> None:
        self.write(f".dev/{PLAN}/review_1.md", f"# Review 1: x\n\nVerdict: {verdict}\n\n## Blockers\n{blockers}\n## Concerns\n")
        self.write(f".dev/{PLAN}/pr.md", PR_BODY)
        self.commit_all("ship")
        git(self.repo, "push", "--quiet")
        self.records(verdict)
        state = self.gh()
        if checks is not None:
            state["default_checks"] = checks
        self.gh_state.write_text(json.dumps(state))
        args = [str(Path(self.env["FACTORY_GH_BIN"])), "pr", "create", "--title", "t", "--base", base,
                "--body-file", str(self.plan_dir / "pr.md")]
        if draft:
            args.append("--draft")
        subprocess.run(args, cwd=self.repo, check=True, capture_output=True)

    def records(self, verdict: str = "PASS", **review_overrides) -> None:
        """The review and gauntlet records ship writes last, at the current revision."""
        head = git(self.repo, "rev-parse", "HEAD")
        finding = {"lenses": ["security"], "file": "webhook.py", "line": 3, "title": "replay window unbounded",
                   "detail": "d", "verification": "CONFIRMED"}
        record = review_record(SHIP_LENSES, revision=head, blockers=[finding] if verdict == "BLOCK" else [],
                               concerns=[finding] if verdict == "CONCERNS" else [])
        record.update(review_overrides)
        self.write(f".dev/{PLAN}/review_1.json", json.dumps(record))
        self.write(f".dev/{PLAN}/gauntlet.json", json.dumps({**GAUNTLET, "revision": head}))

    def gate(self, **overrides) -> gates.GateResult:
        return gates.ship_gate(self.ctx("ship", **{**self.sealed(), "base": "main", **overrides}))

    def other_clone(self) -> Path:
        clone = self.root / "other-clone"
        if not clone.exists():
            subprocess.run(["git", "clone", "--quiet", "-b", "factory/webhook", str(self.root / "project-origin.git"),
                            str(clone)], check=True, capture_output=True)
        git(clone, "pull", "--quiet")
        return clone


class ShipGateTests(ShipGateCase):
    """Ship completes only for the exact revision the published PR carries, re-verified end to end."""

    def test_no_pr(self):
        self.assertIn("no open pull request", self.gate().reason)

    def test_multiple_prs_are_non_retryable(self):
        self.ship()
        subprocess.run([self.env["FACTORY_GH_BIN"], "pr", "create", "--title", "dup"], cwd=self.repo, check=True,
                       capture_output=True)
        gate = self.gate()
        self.assertIn("2 open pull requests", gate.reason)
        self.assertFalse(gate.retryable)

    def test_ready_pass_verifies_the_final_candidate_and_records_the_observation(self):
        self.ship()
        gate = self.gate()
        self.assertTrue(gate.passed, gate.reason)
        head = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(gate.data["pr"]["number"], 1)
        self.assertEqual(gate.data["pr"]["checks"]["pass"], ["ci"])
        self.assertEqual(gate.data["revision"], head)
        self.assertEqual(gate.data["e2e"]["revision"], head)
        self.assertEqual(gate.data["tests"]["scenarios"]["S1"],
                         {"tests/test_webhook.py::WebhookTests::test_repeated_id_ignored": "passed"})
        completion = gate.data["completion"]
        self.assertEqual((completion["revision"], completion["pr"]["head_sha"], completion["pr"]["base"]),
                         (head, head, "main"))

    def test_concerns_continue(self):
        self.ship("CONCERNS")
        self.assertTrue(self.gate().passed)

    def test_block_names_blockers_and_requires_draft(self):
        blockers = "[security] app.py:3 - token logged in plaintext\n"
        self.ship("BLOCK", draft=True, blockers=blockers)
        gate = self.gate()
        self.assertTrue(gate.retryable)
        self.assertIn("token logged in plaintext", gate.reason)
        self.assertNotIn("must stay draft", gate.reason)
        self.assertEqual(gate.data["pr"]["draft"], True)

    def test_block_on_ready_pr_is_called_out(self):
        self.ship("BLOCK", draft=False, blockers="[x] a.py:1 - broken\n")
        self.assertIn("must stay draft", self.gate().reason)

    def test_draft_after_pass(self):
        self.ship("PASS", draft=True)
        self.assertIn("still a draft", self.gate().reason)

    def test_r7_a_change_after_review_and_evidence_cannot_ship_on_the_old_verification(self):
        self.ship()
        self.assertTrue(self.gate().passed)
        self.write("webhook.py", BASE_WEBHOOK_PY)
        self.commit_all("late change after review")
        git(self.repo, "push", "--quiet")
        gate = self.gate()
        self.assertEqual(gate.code, "review.stale")
        self.records()
        gate = self.gate()
        self.assertEqual(gate.code, "evidence.tests_failed")
        self.assertIn("S1: tests/test_webhook.py::WebhookTests::test_repeated_id_ignored failed", gate.reason)

    def test_wrong_repository_or_base_is_not_this_run(self):
        self.ship(base="develop")
        gate = self.gate()
        self.assertEqual((gate.code, gate.retryable), ("pr.wrong_base", False))
        state = self.gh()
        state["prs"][0]["baseRefName"] = "main"
        state["repo"] = "someone-else/fork"
        self.gh_state.write_text(json.dumps(state))
        self.assertEqual(self.gate().code, "pr.wrong_repository")
        state["repo"] = "stub/repo"
        state["prs"][0]["isCrossRepository"] = True
        self.gh_state.write_text(json.dumps(state))
        self.assertEqual(self.gate().code, "pr.wrong_repository")

    def test_remote_ahead_diverged_unpushed_and_failed_refresh_are_not_synchronized(self):
        self.ship()
        clone = self.other_clone()
        (clone / "other.py").write_text("x = 1\n")
        git(clone, "add", "other.py")
        git(clone, "-c", "user.name=o", "-c", "user.email=o@e", "commit", "--quiet", "-m", "someone else")
        git(clone, "push", "--quiet")
        gate = self.gate()
        self.assertEqual(gate.code, "branch.unsynchronized")
        self.assertIn("is behind origin/factory/webhook", gate.reason)
        self.write("mine.py", "y = 2\n")
        self.commit_all("local work")
        self.records()
        self.assertIn("have diverged", self.gate().reason)
        git(self.repo, "reset", "--quiet", "--hard", "origin/factory/webhook")
        self.write("mine.py", "y = 2\n")
        self.commit_all("local only")
        self.assertIn("has 1 unpushed commit(s)", self.gate().reason)
        git(self.repo, "remote", "set-url", "origin", str(self.root / "gone.git"))
        gate = self.gate()
        self.assertEqual(gate.code, "branch.refresh_failed")
        self.assertIn("a stale remote-tracking ref proves nothing", gate.reason)

    def test_evidence_must_be_the_published_evidence(self):
        self.ship()
        stale = {**review_record(SHIP_LENSES), "revision": "0" * 40}
        self.write(f".dev/{PLAN}/review_1.json", json.dumps(stale))
        state = self.gh()
        state["prs"][0]["body"] = "## Summary\n\nDeduplicate webhook deliveries.\n"
        self.gh_state.write_text(json.dumps(state))
        gate = self.gate()
        self.assertEqual(gate.code, "evidence.unpublished")
        self.assertIn("published body has no Evidence section", gate.reason)
        state["prs"][0]["body"] = PR_BODY.replace('{"processed": 1}', '{"processed": 2}')
        self.gh_state.write_text(json.dumps(state))
        self.assertIn("published Evidence differs from .dev/webhook/pr.md", self.gate().reason)
        self.records()
        gate = self.gate()
        self.assertTrue(gate.passed, gate.reason)
        self.assertEqual([(op["kind"], op["result"]) for op in gate.data["operations"]], [("pr-edit", "done")])
        self.assertIn('{"processed": 1}', self.gh()["prs"][0]["body"])
        (self.plan_dir / "pr.md").write_text("## Summary\nno proof\n")
        self.assertEqual(self.gate().code, "evidence.unpublished")

    def test_failing_checks(self):
        self.ship(checks=[{"name": "ci", "state": "FAILURE", "bucket": "fail"}])
        self.assertIn("failing checks on", self.gate().reason)

    def test_expected_checks_that_have_not_appeared_yet_are_waited_for(self):
        self.ship()
        state = self.gh()
        state["checks_delay"] = {"1": 2}
        self.gh_state.write_text(json.dumps(state))
        sleeps = []
        gate = self.gate(deadline=time.time() + 60, sleep=sleeps.append)
        self.assertTrue(gate.passed, gate.reason)
        self.assertEqual(len(sleeps), 2)
        state = self.gh()
        state["checks_delay"] = {"1": 100}
        self.gh_state.write_text(json.dumps(state))
        gate = self.gate(deadline=time.time() + 5, poll_seconds=30)
        self.assertEqual(gate.code, "ci.missing")
        self.assertIn("ci (not reported yet)", gate.reason)

    def test_pending_checks_poll_until_green(self):
        self.ship(checks=[{"name": "ci", "state": "PENDING", "bucket": "pending"}])
        calls = []

        def settle(_seconds):
            calls.append(1)
            state = self.gh()
            state["checks"]["1"] = [{"name": "ci", "state": "SUCCESS", "bucket": "pass"}]
            self.gh_state.write_text(json.dumps(state))

        gate = self.gate(deadline=time.time() + 60, sleep=settle)
        self.assertTrue(gate.passed, gate.reason)
        self.assertEqual(len(calls), 1)

    def test_a_skipped_required_check_needs_explicit_permission(self):
        self.ship(checks=[{"name": "ci", "state": "SKIPPED", "bucket": "skipping"}])
        self.assertEqual(self.gate().code, "ci.skipped")

    def test_a_head_that_moves_while_checks_are_pending_restarts_verification(self):
        self.ship(checks=[{"name": "ci", "state": "PENDING", "bucket": "pending"}])
        clone = self.other_clone()

        def push_elsewhere(_seconds):
            (clone / "late.py").write_text("late = True\n")
            git(clone, "add", "late.py")
            git(clone, "-c", "user.name=o", "-c", "user.email=o@e", "commit", "--quiet", "-m", "late push")
            git(clone, "push", "--quiet")

        gate = self.gate(deadline=time.time() + 60, sleep=push_elsewhere)
        self.assertEqual(gate.code, "branch.unsynchronized")
        self.assertIn("the PR head moved to", gate.data["restarts"][0])

    def test_cancel_stops_pending_check_polling(self):
        self.ship(checks=[{"name": "ci", "state": "PENDING", "bucket": "pending"}])
        ctx = self.ctx("ship", deadline=time.time() + 3600, base="main", **self.sealed())
        ctx.cancel_requested = lambda: any(e["label"] == "gh-pr-checks" for e in ctx.executions)
        gate = gates.ship_gate(ctx)
        self.assertIn("cancelled while PR #1 checks were pending", gate.reason)
        self.assertFalse(gate.retryable)

    def test_pending_checks_at_deadline(self):
        self.ship(checks=[{"name": "ci", "state": "PENDING", "bucket": "pending"}])
        gate = self.gate(deadline=time.time() + 5, poll_seconds=30)
        self.assertIn("still pending", gate.reason)
        self.assertEqual(self.gate(deadline=time.time()).reason, "the attempt deadline passed before the ship gate")

    def test_a_repository_without_ci_can_complete_only_when_its_contract_says_so(self):
        self.ship(checks=[])
        gate = self.gate(deadline=time.time() + 5, poll_seconds=30)
        self.assertEqual(gate.code, "ci.missing")
        contract = json.loads((self.repo / ".factory" / "contract.json").read_text())
        contract["ci"] = {"none": "the repository has no hosted CI; the contract's validation is the merge gate"}
        self.write(".factory/contract.json", json.dumps(contract))
        self.commit_all("declare no CI")
        git(self.repo, "push", "--quiet")
        self.records()
        self.seal()
        gate = self.gate(baseline={"head": git(self.repo, "rev-parse", "HEAD")})
        self.assertTrue(gate.passed, gate.reason)

    def test_dirty_tree_and_unpushed_commits(self):
        self.ship()
        self.write("README.md", "dirty\n")
        self.assertIn("dirty", self.gate().reason)
        self.commit_all("local only")
        gate = self.gate()
        self.assertIn("1 unpushed commit", gate.reason, "with a stale review the runner must not publish")
        self.assertEqual(gate.data["publication"], {"runner": "skipped", "why": "review does not approve the current revision"})
        self.records()
        gate = self.gate()
        self.assertTrue(gate.passed, gate.reason)
        self.assertEqual([(op["kind"], op["result"]) for op in gate.data["operations"]], [("push", "done")])

    def test_result_must_match_github(self):
        self.ship()
        self.result("ship", pr_url="https://github.com/stub/repo/pull/9")
        self.assertIn("names https://github.com/stub/repo/pull/9", self.gate().reason)


class ReviewEvidenceTests(ShipGateCase):
    """Ship needs a complete review of the current revision and a gauntlet the runner re-executes."""

    def test_r5_an_incomplete_review_cannot_make_the_run_ready(self):
        self.ship()
        self.records(completeness="incomplete", verdict=None, missing_lenses=["security"],
                     expected_lenses=[*SHIP_LENSES, "security"])
        gate = self.gate()
        self.assertEqual(gate.code, "review.incomplete")
        self.assertIn("the review is incomplete (missing lenses: security", gate.reason)
        self.records(completed_lenses=["correctness", "tests"], expected_lenses=["correctness", "tests"])
        self.assertIn("mandatory lenses without a valid result: simplify, spec-conformance", self.gate().reason)
        self.records(verdict="PASS", concerns=[{"lenses": ["tests"], "file": "a", "title": "t", "detail": "d",
                                                "verification": "PLAUSIBLE"}])
        self.assertIn("verdict 'PASS' does not follow from its findings (CONCERNS)", self.gate().reason)

    def test_markdown_must_transcribe_the_record(self):
        self.ship(verdict="PASS")
        self.records(verdict="CONCERNS", concerns=[{"lenses": ["tests"], "file": "a", "title": "t", "detail": "d",
                                                    "verification": "PLAUSIBLE"}])
        self.assertIn("review_1.md says PASS but review_1.json says CONCERNS", self.gate().reason)

    def test_a_missing_or_unjustified_gauntlet_blocks(self):
        self.ship()
        (self.plan_dir / "gauntlet.json").unlink()
        self.assertEqual(self.gate().code, "gauntlet.invalid")
        head = git(self.repo, "rev-parse", "HEAD")
        without = {**GAUNTLET, "revision": head, "checks": [c for c in GAUNTLET["checks"] if c["id"] != "mutation"]}
        self.write(f".dev/{PLAN}/gauntlet.json", json.dumps(without))
        self.assertIn("mutation must appear exactly once (found 0)", self.gate().reason)
        contract = json.loads((self.repo / ".factory" / "contract.json").read_text())
        del contract["gauntlet"]["security"]
        self.write(".factory/contract.json", json.dumps(contract))
        self.commit_all("contract without a security reason")
        git(self.repo, "push", "--quiet")
        self.records()
        self.seal()
        gate = self.gate(baseline={"head": git(self.repo, "rev-parse", "HEAD")})
        self.assertEqual(gate.code, "gauntlet.unjustified")
        self.assertIn("gauntlet check security was reported inapplicable", gate.reason)

    def test_surviving_violations_and_failing_checks_block(self):
        self.ship()
        head = git(self.repo, "rev-parse", "HEAD")
        checks = json.loads(json.dumps(GAUNTLET["checks"]))
        checks[0]["surviving"] = ["webhook.py:3 unused variable"]
        self.write(f".dev/{PLAN}/gauntlet.json", json.dumps({**GAUNTLET, "revision": head, "checks": checks}))
        gate = self.gate()
        self.assertEqual(gate.code, "gauntlet.violations")
        checks[0]["surviving"] = []
        checks[0]["command"] = {"run": ["python3", "-m", "py_compile", "missing.py"]}
        self.write(f".dev/{PLAN}/gauntlet.json", json.dumps({**GAUNTLET, "revision": head, "checks": checks}))
        gate = self.gate()
        self.assertEqual(gate.code, "gauntlet.failed")
        self.assertIn("gauntlet check static-analysis (python3 -m py_compile missing.py) exited 1", gate.reason)

    def test_a_pinned_contract_command_wins_over_the_agent(self):
        contract = json.loads((self.repo / ".factory" / "contract.json").read_text())
        contract["gauntlet"]["static-analysis"] = {"command": {"run": ["sh", "-c", "echo lint found 2 issues; exit 2"]}}
        self.write(".factory/contract.json", json.dumps(contract))
        self.commit_all("pin static analysis")
        git(self.repo, "push", "--quiet")
        self.seal()
        pinned_at = git(self.repo, "rev-parse", "HEAD")
        self.ship()
        head = git(self.repo, "rev-parse", "HEAD")
        checks = json.loads(json.dumps(GAUNTLET["checks"]))
        checks[0]["command"] = {"run": ["true"]}
        self.write(f".dev/{PLAN}/gauntlet.json", json.dumps({**GAUNTLET, "revision": head, "checks": checks}))
        gate = self.gate(baseline={"head": pinned_at})
        self.assertEqual(gate.code, "gauntlet.failed")
        self.assertIn("exited 2 on", gate.reason)
        self.assertTrue(gate.reason.endswith(": lint found 2 issues"))
        self.assertTrue(gate.data["gauntlet"]["checks"]["static-analysis"]["pinned"])

    def test_the_dependency_rule_is_inapplicable_only_without_its_rules_file(self):
        self.ship()
        self.assertTrue(self.gate().passed, self.gate().reason)
        self.write("docs/dependencies.md", "# Dependencies\n")
        self.commit_all("rules")
        git(self.repo, "push", "--quiet")
        self.records()
        self.assertEqual(self.gate().code, "gauntlet.unjustified")


class MergeTests(unittest.TestCase):
    """The success / failure / condition matrix of the authoritative outcome."""

    ok = gates.passed()
    bad = gates.blocked("gate says no", code="validation.failed")
    precondition = gates.blocked("absent", precondition=True, code="input.spec_missing")
    done = {"status": "done", "reason": "fine", "conditions": []}
    stuck = {"status": "blocked", "reason": "stuck", "conditions": []}
    credentials = {"id": "C1", "code": "environment.missing_credentials", "summary": "no STRIPE_KEY", "retryable": False}
    decision = {"id": "C2", "code": "decision.verification_failed", "summary": "retry policy failed", "retryable": True}

    def test_gate_and_claims_without_conditions(self):
        outcome = gates.merge(self.ok, self.done, None)
        self.assertEqual((outcome.outcome, outcome.warning), ("done", None))
        outcome = gates.merge(self.bad, self.done, None)
        self.assertEqual((outcome.outcome, outcome.reason, outcome.code), ("blocked", "gate says no", "validation.failed"))
        outcome = gates.merge(self.ok, self.stuck, None)
        self.assertEqual(outcome.outcome, "done")
        self.assertIn("but the gate passed", outcome.warning)
        self.assertIn("no result file", gates.merge(self.ok, None, None).warning)
        outcome = gates.merge(self.bad, self.stuck, None)
        self.assertEqual((outcome.retryable, outcome.source), (True, "gate"))
        self.assertIn("skill: stuck", outcome.reason)
        self.assertFalse(gates.merge(self.precondition, self.stuck, None).retryable)

    def test_invalid_results_fail_explicitly(self):
        outcome = gates.merge(self.ok, None, "unknown condition code 'weird'")
        self.assertEqual((outcome.outcome, outcome.code, outcome.retryable), ("blocked", "result.invalid", True))
        self.assertIn("the stage result is invalid", outcome.reason)
        outcome = gates.merge(self.bad, None, "malformed")
        self.assertEqual((outcome.outcome, outcome.code), ("blocked", "validation.failed"))

    def test_unresolved_conditions_block_a_passing_gate(self):
        outcome = gates.merge(self.ok, self.done, None, [self.credentials])
        self.assertEqual((outcome.outcome, outcome.retryable, outcome.source), ("blocked", False, "condition"))
        self.assertEqual((outcome.code, outcome.conditions), ("environment.missing_credentials", ["C1"]))
        self.assertEqual(outcome.reason, "environment.missing_credentials (C1): no STRIPE_KEY")
        outcome = gates.merge(self.ok, self.stuck, None, [self.decision])
        self.assertEqual((outcome.outcome, outcome.retryable), ("blocked", True))
        outcome = gates.merge(self.ok, None, None, [self.decision, self.credentials])
        self.assertFalse(outcome.retryable)

    def test_conditions_with_a_failing_gate_keep_both_reasons(self):
        outcome = gates.merge(self.bad, self.stuck, None, [self.decision])
        self.assertEqual(outcome.reason, "decision.verification_failed (C2): retry policy failed; gate: gate says no")
        self.assertTrue(outcome.retryable)
        self.assertFalse(gates.merge(self.precondition, None, None, [self.decision]).retryable)


if __name__ == "__main__":
    unittest.main()
