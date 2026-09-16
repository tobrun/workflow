"""Checkpointed subphases: reuse what is still valid, recompute what changed, publish without paid retries."""

import contextlib
import io
import json
import os
from pathlib import Path
from unittest import mock

from runner import checkpoints, cli, gates, records, supervise
from runner.model import Run
from runner.tests.helpers import (CONTRACT, E2E_DRIVER_PY, GAUNTLET, NOTES, PR_BODY, SCENARIO_MAP, SHIP_LENSES, SPEC,
                                  TESTS_PY, WEBHOOK_PY, FactoryTestCase, at_head, git, happy_scenario, make_repo,
                                  review_record)
from runner.worker import Worker


def ship_without_publication() -> dict:
    """The agent finished its analysis and wrote pr.md, but its push and `gh pr create` failed."""
    return {"files": {".dev/{plan}/review_1.md": "# Review 1\n\nVerdict: PASS\n\n## Blockers\n", ".dev/{plan}/pr.md": PR_BODY},
            "run": [at_head(".dev/{plan}/review_1.json", review_record(SHIP_LENSES)),
                    at_head(".dev/{plan}/gauntlet.json", GAUNTLET)],
            "result": {"status": "blocked", "reason": "gh pr create failed: HTTP 502", "conditions": []}}


class PublicationTests(FactoryTestCase):
    def call(self, *argv: str) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            cli.main(list(argv))
        return stdout.getvalue()

    def codex_execs(self, stage: str) -> int:
        return len([c for c in self.stub_calls("codex") if c["argv"][0] == "exec" and c["env"]["FACTORY_STAGE"] == stage])

    def test_a_transient_publication_failure_is_retried_by_the_runner_without_another_model_attempt(self):
        scenario = happy_scenario()
        scenario["ship"] = [ship_without_publication()]
        self.scenario(scenario)
        state = self.gh()
        state["fail"] = {"pr create": 1}
        self.gh_state.write_text(json.dumps(state))
        run = self.queued_run()
        Worker(self.home, run.id, grace=1, sleep=lambda seconds: None).run()
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "done", data["human"])
        self.assertEqual(self.codex_execs("ship"), 1)
        self.assertEqual(data["retries"]["used"], 0)
        self.assertEqual([(op["kind"], op["result"], op["tries"]) for op in data["operations"]],
                         [("push", "done", 1), ("pr-create", "done", 2)])
        self.assertEqual(len([m for m in self.gh()["mutations"] if m["op"] == "create"]), 1)
        shown = self.call("show", run.id)
        self.assertIn("retries    0/5 paid; 1 deterministic operation retry (not charged)", shown)
        self.assertIn("operation  pr-create done after 2 tries", shown)

    def test_a_crash_after_the_pr_is_created_never_creates_a_second_one(self):
        scenario = happy_scenario()
        scenario["ship"] = [ship_without_publication()]
        self.scenario(scenario)
        state = self.gh()
        state["fail_after"] = {"pr create": 1}
        self.gh_state.write_text(json.dumps(state))
        run = self.queued_run()
        fault = {"FACTORY_FAULT": "gate.after_pr_create", "FACTORY_FAULT_ONCE": str(self.root / "fault")}
        with mock.patch.dict(os.environ, fault), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["resume", run.id, "--detach"]), 0)
        self.wait_for(lambda: (self.root / "fault").exists() and not supervise.worker_alive(run.dir), 60, "crash")
        self.assertEqual(Run.load(run.dir).status, "running")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["resume", run.id, "--detach"]), 0)
        data = self.wait_status(run.dir, ("done", "needs-human", "cancelled"), timeout=90)
        self.assertEqual(data["status"], "done", data["human"])
        self.assertEqual(len([m for m in self.gh()["mutations"] if m["op"] == "create"]), 1)
        self.assertEqual(len(self.gh()["prs"]), 1)
        self.assertEqual(self.codex_execs("ship"), 1)


class ReuseTests(FactoryTestCase):
    def test_ship_reuses_build_verification_for_unchanged_code_and_recomputes_after_a_code_change(self):
        self.scenario(happy_scenario())
        run = self.queued_run()
        Worker(self.home, run.id, grace=1).run()
        data = json.loads((run.dir / "run.json").read_text())
        ship = [a for a in data["attempts"] if a["stage"] == "ship"][0]
        decisions = {c["subphase"]: c["decision"].split(":")[0] for c in ship["checkpoints"]}
        self.assertEqual((decisions["tests"], decisions["e2e"], decisions["validation:readme"]),
                         ("reused", "reused", "reused"))
        self.assertEqual(decisions["gauntlet:static-analysis"], "computed")

        changed = happy_scenario()
        changed["ship"][0]["run"].insert(0, ["sh", "-c", "echo '# reviewed' >> webhook.py && git commit --quiet -am tidy"])
        self.scenario(changed)
        second = self.queued_run(plan="changed")
        Worker(self.home, second.id, grace=1).run()
        data = json.loads((second.dir / "run.json").read_text())
        self.assertEqual(data["status"], "done", data["human"])
        ship = [a for a in data["attempts"] if a["stage"] == "ship"][0]
        decisions = {c["subphase"]: c["decision"] for c in ship["checkpoints"]}
        self.assertTrue(decisions["tests"].startswith("computed: inputs changed (tree)"), decisions)
        self.assertTrue(decisions["e2e"].startswith("computed: inputs changed (tree)"), decisions)


class InvalidationTests(FactoryTestCase):
    """Gate-level: tool, contract, and output changes each force recomputation, with the reason."""

    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.root)
        git(self.repo, "checkout", "--quiet", "-b", "factory/webhook")
        self.base = git(self.repo, "rev-parse", "HEAD")
        plan = self.repo / ".dev" / "webhook"
        plan.mkdir(parents=True)
        (plan / "spec.md").write_text(SPEC)
        from runner import worktree
        worktree.install_excludes(self.repo)
        for relative, content in {"webhook.py": WEBHOOK_PY, "tests/test_webhook.py": TESTS_PY, "e2e/run.py": E2E_DRIVER_PY,
                                  ".dev/webhook/scenario-map.json": json.dumps(SCENARIO_MAP),
                                  ".dev/webhook/implementation-notes.md": NOTES}.items():
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "--quiet", "-m", "build")
        self.run_dir = self.root / "run"
        self.reports = self.root / "reports"
        self.reports.mkdir()
        self.seal()

    def seal(self) -> None:
        from runner import intent
        self.approved = intent.seal(self.run_dir, run_id="t", spec=self.repo / ".dev" / "webhook" / "spec.md",
                                    contract=self.repo / ".factory" / "contract.json", request_file=None, request="x",
                                    base_sha=self.base)

    def gate(self, **overrides) -> gates.GateResult:
        fields = dict(worktree=self.repo, plan="webhook", stage="build", report_dir=self.reports,
                      attempt_dir=self.root / "attempt", branch="factory/webhook", base_sha=self.base,
                      baseline={"build_start_sha": self.base, "head": self.base}, run_dir=self.run_dir,
                      intent_sha256=self.approved["content_sha256"], require_intent=True)
        fields.update(overrides)
        ctx = gates.GateContext(**fields)
        result = gates.build_gate(ctx)
        self.assertTrue(result.passed, result.reason)
        return {c["subphase"]: c["decision"] for c in ctx.checkpoint_log}

    def test_tool_contract_and_output_changes_invalidate_exactly_their_checkpoints(self):
        first = self.gate()
        self.assertTrue(all(decision.startswith("computed: no checkpoint") for decision in first.values()), first)
        self.assertTrue(all(decision.startswith("reused") for decision in self.gate().values()))
        runner_change = self.gate(_runner_sha="0" * 64)
        self.assertTrue(all("inputs changed (runner)" in decision for decision in runner_change.values()), runner_change)
        self.assertTrue(all("inputs changed (runner)" in decision for decision in self.gate().values()))

        contract = json.loads((self.repo / ".factory" / "contract.json").read_text())
        contract["validation"] = [{"id": "readme", "run": ["test", "-s", "README.md"]}]
        (self.repo / ".factory" / "contract.json").write_text(json.dumps(contract))
        git(self.repo, "commit", "--quiet", "-am", "contract")
        self.seal()
        decisions = self.gate(baseline={"build_start_sha": self.base, "head": git(self.repo, "rev-parse", "HEAD")})
        self.assertIn("inputs changed (command, tree)", decisions["validation:readme"])

        decisions = self.gate(baseline={"build_start_sha": self.base, "head": git(self.repo, "rev-parse", "HEAD")})
        self.assertTrue(decisions["e2e"].startswith("reused"))
        published = self.reports / "webhook-e2e.json"
        published.write_text(published.read_text().replace("processed", "handled"))
        tests_checkpoint = checkpoints.path(self.run_dir, "tests")
        tests_checkpoint.write_text("{corrupt")
        decisions = self.gate(baseline={"build_start_sha": self.base, "head": git(self.repo, "rev-parse", "HEAD")})
        self.assertEqual(decisions["e2e"], "computed: output webhook-e2e.json is missing or altered")
        self.assertEqual(decisions["tests"], "computed: checkpoint is unreadable or an unsupported version")
        self.assertEqual(records.load_json_file(published, max_bytes=10**6)["scenarios"][0]["states"][0]["entity"],
                         "processed deliveries")
