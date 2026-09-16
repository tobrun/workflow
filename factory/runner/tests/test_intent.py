"""Approved intent and attempt handoffs: sealed at handoff, drift detected mechanically, inputs retained."""

import contextlib
import io
import json
import os
import shutil
from pathlib import Path

from runner import cli, events, intent
from runner.model import Run
from runner.tests.helpers import (SCENARIO_MAP, SPEC, TESTS_PY, FactoryTestCase, build_step, happy_scenario,
                                  make_repo, review_step, scope_files)

SPEC_WITH_NON_GOAL = SPEC.replace(
    "Inputs: webhook deliveries. Outputs: each delivery id is processed once.\n",
    "Inputs: webhook deliveries. Outputs: each delivery id is processed once.\n\n"
    "⊘ replaying old deliveries - no backlog exists; reopen if a backfill is requested\n")


class IntentTestCase(FactoryTestCase):
    def call(self, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def new_run(self, spec: str = SPEC_WITH_NON_GOAL) -> Path:
        self.claude([scope_files(spec)])
        repo = make_repo(self.root, "intent")
        code, out, err = self.call("new", str(repo), "Add idempotency", "--plan", "webhook", "--yes", "--detach")
        self.assertEqual(code, 0, out + err)
        return next((self.home / "runs").glob("*-webhook"))

    def review_editing_spec(self, old: str, new: str, index: int = 1, **extra) -> dict:
        step = review_step(index=index)
        replace = (f"import pathlib, sys; p = pathlib.Path('.dev/{{plan}}/spec.md'); "
                   f"p.write_text(p.read_text().replace({old!r}, {new!r}))")
        step["run"] = [["python3", "-c", replace]]
        step.update(extra)
        return step


class HandoffTests(IntentTestCase):
    def test_handoff_seals_request_non_goals_and_scenarios_with_stable_ids(self):
        self.scenario({**happy_scenario(), "scope-review": [{"sleep": 30}]})
        run_dir = self.new_run()
        document = json.loads((run_dir / "intent" / "approved.json").read_text())
        self.assertEqual(document["version"], 1)
        self.assertEqual(document["request"], "Add idempotency")
        self.assertEqual(document["non_goals"],
                         ["replaying old deliveries - no backlog exists; reopen if a backfill is requested"])
        self.assertEqual([(s["id"], s["layer"], s["requirement"]) for s in document["scenarios"]],
                         [("S1", "unit", "repeated id -> ignored"), ("S2", "e2e", "duplicate delivery -> processed once")])
        run = Run.load(run_dir)
        self.assertEqual(run.data["intent"]["sha256"], document["content_sha256"])
        self.assertTrue((run_dir / "intent" / "versions" / "1" / "spec.md").is_file())
        self.assertTrue((run_dir / "intent" / "versions" / "1" / "contract.json").is_file())
        approved = [e for e in events.read(run_dir) if e["event"] == "intent.approved"]
        self.assertEqual(approved[0]["data"]["scenarios"], ["S1", "S2"])
        code, out, _ = self.call("intent", run.id)
        self.assertEqual(code, 0)
        self.assertIn("S2   [e2e] duplicate delivery -> processed once (approved)", out)
        self.call("cancel", run.id)


class DriftTests(IntentTestCase):
    def test_removing_weakening_or_renaming_an_approved_scenario_is_detected(self):
        cases = {
            "removed": ("; [e2e] duplicate delivery -> processed once", ""),
            "weakened": ("[e2e] duplicate delivery -> processed once", "[unit] duplicate delivery -> processed once"),
            "renamed": ("duplicate delivery -> processed once", "duplicate delivery -> handled"),
        }
        for name, (old, new) in cases.items():
            with self.subTest(name):
                self.scenario({**happy_scenario(), "scope-review": [self.review_editing_spec(old, new)] * 6})
                run = self.queued_run(plan=f"drift-{name}")
                from runner.worker import Worker
                Worker(self.home, run.id, grace=1).run()
                data = json.loads((run.dir / "run.json").read_text())
                review = [a for a in data["attempts"] if a["stage"] == "scope-review"][0]
                self.assertEqual((review["outcome"], review["code"]), ("blocked", "intent.scenario_changed"))
                self.assertIn("approved scenario S2 ([e2e] duplicate delivery -> processed once) was removed or "
                              "reworded", review["reason"])
                self.assertIn("intent/versions/1/spec.md", review["reason"])

    def test_removing_an_approved_non_goal_is_detected(self):
        self.scenario({**happy_scenario(), "scope-review": [self.review_editing_spec(
            "⊘ replaying old deliveries", "Also replay old deliveries")] * 6})
        run = self.queued_run()
        (run.plan_dir / "spec.md").write_text(SPEC_WITH_NON_GOAL)
        intent.snapshot_handoff(run)
        run.save()
        from runner.worker import Worker
        Worker(self.home, run.id, grace=1).run()
        review = [a for a in json.loads((run.dir / "run.json").read_text())["attempts"]][0]
        self.assertIn("approved non-goal was removed or reworded: replaying old deliveries", review["reason"])

    def test_refinement_within_the_intent_continues_with_an_audit_record(self):
        refined = SPEC.replace("  ✓ database table - survives restarts and the database is already there",
                               "  ✓ database table with a unique index - survives restarts; the index makes it atomic")
        refined = refined.replace("[e2e] duplicate delivery -> processed once",
                                  "[e2e] duplicate delivery -> processed once; [integration] concurrent duplicates -> "
                                  "one row")
        step = review_step()
        step["files"][".dev/{plan}/spec.md"] = refined
        step["files"][".dev/{plan}/spec-review_1.md"] = (
            "# Spec review 1\n\nVerdict: APPROVED\n\n## Escalations resolved\n### E1 - consistency - atomicity\n"
            "Question: race between check and insert - Answered: factory policy (auto-decided) - unique index\n")
        mapping = json.loads(json.dumps(SCENARIO_MAP))
        mapping["scenarios"]["S3"] = {"layer": "integration",
                                      "tests": ["tests/test_webhook.py::WebhookTests::test_concurrent_duplicates_one_row"]}
        build = build_step(tests=TESTS_PY + "\n    def test_concurrent_duplicates_one_row(self):\n"
                                            "        hooks = Webhooks()\n"
                                            "        results = {hooks.deliver('d2', {}) for _ in range(3)}\n"
                                            "        self.assertEqual(hooks.processed, ['d2'])\n",
                           scenario_map=mapping)
        self.scenario({**happy_scenario(), "scope-review": [step], "build": [build]})
        run = self.queued_run()
        from runner.worker import Worker
        Worker(self.home, run.id, grace=1).run()
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "done", data["human"])
        delta = json.loads((run.dir / "intent" / "deltas" / "scope-review-1.json").read_text())
        self.assertEqual([d["slug"] for d in delta["decisions"]["changed"]], ["D-dedup-store"])
        self.assertEqual(delta["scenarios"]["added"], ["S3"])
        self.assertEqual(delta["affected_scenarios"], ["S1", "S2", "S3"])
        self.assertEqual(len(delta["auto_decided"]), 1)
        catalog = {s["id"]: s for s in intent.catalog(run.dir)}
        self.assertEqual((catalog["S3"]["approved"], catalog["S3"]["origin"]), (False, "scope-review attempt 1"))
        code, out, _ = self.call("intent", run.id)
        self.assertIn("S3   [integration] concurrent duplicates -> one row (added by scope-review attempt 1)", out)
        self.assertIn("scope-review-1: decisions D-dedup-store; scenarios added S3; affected S1, S2, S3; 1 auto-decided",
                      out)


class AttemptSnapshotTests(IntentTestCase):
    def test_earlier_attempt_inputs_remain_after_a_later_attempt_rewrites_the_files(self):
        first = self.review_editing_spec("webhook.py - skip ids", "webhook.py - (attempt 1 edit) skip ids",
                                         result="omit")
        first["files"] = {}
        second = self.review_editing_spec("(attempt 1 edit)", "(attempt 2 edit)", index=1)
        self.scenario({**happy_scenario(), "scope-review": [first, second]})
        run = self.queued_run()
        from runner.worker import Worker
        Worker(self.home, run.id, grace=1).run()
        self.assertEqual(json.loads((run.dir / "run.json").read_text())["status"], "done")
        code, out, _ = self.call("inputs", run.id, "--stage", "scope-review", "--attempt", "1", "--file", "spec.md")
        self.assertEqual((code, out), (0, SPEC))
        code, out, _ = self.call("inputs", run.id, "--stage", "scope-review", "--attempt", "2", "--file", "spec.md")
        self.assertIn("(attempt 1 edit)", out)
        self.assertIn("(attempt 2 edit)", (run.plan_dir / "spec.md").read_text())
        code, out, _ = self.call("inputs", run.id, "--stage", "scope-review", "--attempt", "2")
        self.assertIn("spec.md", out)
        self.assertIn("changed", out)
        manifest = json.loads((run.attempt_dir("scope-review", 2) / "inputs" / "manifest.json").read_text())
        self.assertEqual(manifest["intent_sha256"], run.data["intent"]["sha256"])
        self.assertEqual(len(manifest["source_revision"]), 40)

    def test_tampered_or_missing_snapshots_block_with_a_repair(self):
        self.scenario(happy_scenario())
        run = self.queued_run()
        approved = run.dir / "intent" / "approved.json"
        document = json.loads(approved.read_text())
        document["scenarios"] = document["scenarios"][:1]
        approved.write_text(json.dumps(document))
        from runner.worker import Worker
        Worker(self.home, run.id, grace=1).run()
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "needs-human")
        self.assertEqual(data["attempts"][0]["code"], "intent.tampered")
        self.assertIn("does not match its content hash", data["human"]["reason"])
        self.assertIn(f"factory retry {run.id} --rescope", data["human"]["reason"])
        shutil.rmtree(run.dir / "intent")
        self.assertEqual(self.call("retry", run.id, "--detach")[0], 0)
        data = self.wait_status(run.dir, ("done", "needs-human", "cancelled"))
        self.assertEqual(data["attempts"][-1]["code"], "intent.missing")

    def test_rescope_reseals_a_new_version_and_keeps_scenario_ids(self):
        self.scenario({**happy_scenario(), "scope-review": [{"sleep": 30}]})
        run_dir = self.new_run(SPEC)
        run = Run.load(run_dir)
        self.assertEqual(self.call("cancel", run.id)[0], 0)
        self.wait_status(run_dir, ("cancelled",))
        extended = SPEC.replace("[e2e] duplicate delivery -> processed once",
                                "[e2e] duplicate delivery -> processed once; [unit] missing id -> rejected")
        self.claude([scope_files(extended)])
        code, out, err = self.call("retry", run.id, "--rescope", "--detach")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(Run.load(run_dir).status, "scoping")
        self.scenario(happy_scenario())
        code, out, err = self.call("scope", run.id, "--resume", "--yes", "--detach")
        self.assertEqual(code, 0, out + err)
        document = json.loads((run_dir / "intent" / "approved.json").read_text())
        self.assertEqual(document["version"], 2)
        self.assertEqual([s["id"] for s in document["scenarios"]], ["S1", "S2", "S3"])
        self.assertTrue((run_dir / "intent" / "versions" / "1" / "approved.json").is_file())
        self.assertEqual(self.call("cancel", run.id)[0], 0)
        self.wait_status(run_dir, ("cancelled",))

    def test_snapshots_survive_archiving(self):
        self.scenario(happy_scenario())
        run = self.queued_run()
        from runner.worker import Worker
        Worker(self.home, run.id, grace=1).run()
        state = self.gh()
        state["prs"][0].update({"state": "MERGED", "mergedAt": "2026-09-16T00:00:00Z"})
        self.gh_state.write_text(json.dumps(state))
        self.assertEqual(self.call("gc")[0], 0)
        archived = self.home / "archive" / run.id
        self.assertFalse(run.dir.exists())
        code, out, _ = self.call("intent", run.id)
        self.assertEqual(code, 0, out)
        self.assertIn("S1   [unit] repeated id -> ignored (approved)", out)
        code, out, _ = self.call("inputs", run.id, "--stage", "build", "--file", "spec.md")
        self.assertEqual((code, out), (0, SPEC))
        self.assertTrue((archived / "attempts" / "build-1" / "inputs" / "manifest.json").is_file())
        self.assertEqual(os.path.relpath(archived / "intent" / "approved.json", archived), "intent/approved.json")
