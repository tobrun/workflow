import contextlib
import dataclasses
import io
import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from runner import cli, events, slots, supervise
from runner.model import Run
from runner.pipeline import PIPELINE
from runner.tests.helpers import (FactoryTestCase, build_step, git, happy_scenario, make_repo, review_step, scope_files,
                                  ship_step)
from runner.worker import Worker


class WorkerTestCase(FactoryTestCase):
    def cli(self, *argv: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main(list(argv))

    def work(self, run: Run, **kwargs) -> dict:
        code = Worker(self.home, run.id, grace=kwargs.pop("grace", 1), **kwargs).run()
        self.assertIn(code, (0,))
        return json.loads((run.dir / "run.json").read_text())

    def attempts(self, data: dict, stage: str | None = None) -> list[dict]:
        return [a for a in data["attempts"] if stage is None or a["stage"] == stage]

    def event_names(self, run: Run) -> list[str]:
        return [e["event"] for e in events.read(run.dir)]

    def wait_event(self, run: Run, name: str, timeout: float = 20) -> None:
        self.wait_for(lambda: name in self.event_names(run), timeout, f"event {name}")


class HappyPathTests(WorkerTestCase):
    def test_full_happy_path(self):
        scenario = happy_scenario()
        scenario["scope-review"][0]["files"]["docs/decisions.md"] = "# Decisions\n\nD-dedup-store: database table\n"
        self.scenario(scenario)
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["retries"]["used"], 0)
        self.assertEqual([a["stage"] for a in self.attempts(data)], ["scope-review", "build", "ship"])
        self.assertEqual(data["pr"]["url"], "https://github.com/stub/repo/pull/1")
        self.assertFalse(data["pr"]["draft"])
        head = git(run.worktree, "rev-parse", "HEAD")
        self.assertEqual((data["completion"]["revision"], data["completion"]["pr"]["head_sha"]), (head, head))
        self.assertEqual(data["pr"]["head_sha"], head)
        self.assertEqual(data["tokens_total"], 3300)
        self.assertEqual(data["attempts"][0]["session_id"], "stub-scope-review-1")
        self.assertIn("docs(scope-review): record refined decisions for webhook", git(run.worktree, "log", "--format=%s"))
        self.assertEqual(git(run.worktree, "ls-files", ".dev"), "")
        self.assertEqual(git(run.worktree, "show", "--name-only", "--format=", "HEAD"), "e2e/run.py\ntests/test_webhook.py\nwebhook.py")
        self.assertIsNone(data.get("slot"))
        names = self.event_names(run)
        order = ["slot.acquired", "stage.started", "process.spawned", "process.exited", "gate.evaluated",
                 "stage.finished", "run.queued", "slot.released"]
        positions = [names.index(name) for name in order]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(names[-1], "slot.released")
        self.assertIn("run.done", names)
        for stage in ("scope-review", "build", "ship"):
            attempt_dir = run.attempt_dir(stage, 1)
            for name in ("prompt.txt", "stdout.jsonl", "stderr.log", "last-message.md", "gate.json"):
                self.assertTrue((attempt_dir / name).exists(), f"{stage}: {name}")

    def test_host_contract_seen_by_codex(self):
        run = self.queued_run()
        self.work(run)
        call = next(c for c in self.stub_calls("codex") if c["argv"][0] == "exec")
        self.assertFalse(call["stdin_tty"])
        self.assertEqual(call["env"]["FACTORY_STAGE"], "scope-review")
        self.assertEqual(call["env"]["FACTORY_ATTEMPT"], "1")
        self.assertEqual(call["env"]["FACTORY_PLAN"], "webhook")
        self.assertEqual(call["env"]["FACTORY_RUN_DIR"], str(run.dir))
        self.assertEqual(call["env"]["GH_TOKEN"], "gho_stubtoken")
        self.assertEqual(Path(call["cwd"]).resolve(), run.worktree.resolve())
        context = json.loads((run.worktree / ".dev" / "factory-run.json").read_text())
        self.assertEqual(context["stage"], "ship")
        self.assertEqual(git(run.worktree, "status", "--porcelain"), "")

    def test_skill_reported_block_prevents_completion_even_when_the_gate_passes(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [
            {**review_step(), "result": {"status": "blocked", "reason": "screenshot parity command failed"}},
            review_step(index=2),
        ]
        self.scenario(scenario)
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["retries"]["used"], 1)
        first = self.attempts(data, "scope-review")[0]
        self.assertEqual((first["outcome"], first["source"], first["code"]),
                         ("blocked", "skill", "result.blocked"))
        self.assertIn("screenshot parity command failed", first["reason"])

    def test_corrupt_result_file_fails_the_gate(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [{**review_step(), "result": "corrupt"}, review_step(index=2)]
        self.scenario(scenario)
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done")
        first = self.attempts(data, "scope-review")[0]
        self.assertEqual(first["outcome"], "blocked")
        self.assertIn("scope-review-result.json is not valid JSON", first["reason"])


class EvidenceTests(WorkerTestCase):
    def test_r1_comment_only_tests_and_an_empty_e2e_record_never_complete_through_the_cli(self):
        step = build_step(tests="# test_repeated_id_ignored is covered elsewhere\n")
        step["files"]["e2e/run.py"] = ("import json, os\njson.dump({'schema': 'factory.e2e/1', 'plan': 'webhook', "
                                       "'kind': 'non-frontend', 'revision': os.environ['FACTORY_REVISION'], "
                                       "'scenarios': []}, open(os.path.join(os.environ['FACTORY_E2E_OUT'], "
                                       "'e2e.json'), 'w'))\n")
        scenario = happy_scenario()
        scenario["build"] = [step]
        self.scenario(scenario)
        self.claude([scope_files()])
        repo = make_repo(self.root, "r1")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.main(["new", str(repo), "Add idempotency", "--plan", "webhook", "--yes"])
        self.assertEqual(code, 1, out.getvalue())
        data = json.loads((next((self.home / "runs").glob("*-webhook")) / "run.json").read_text())
        self.assertEqual((data["status"], data["stage"]), ("needs-human", "build"))
        self.assertIn("no progress: build attempts 1 and 2 both failed with evidence.tests_failed", data["human"]["reason"])
        builds = self.attempts(data, "build")
        self.assertEqual({b["code"] for b in builds}, {"evidence.tests_failed"})
        self.assertIn("S1: tests/test_webhook.py::WebhookTests::test_repeated_id_ignored was not executed",
                      builds[0]["reason"])
        self.assertEqual(self.gh()["prs"], [])


class ReviewCompletenessTests(WorkerTestCase):
    def ship_variant(self, *, review: dict | None = None, gauntlet: bool = True) -> dict:
        from runner.tests.helpers import SHIP_LENSES, at_head, review_record
        step = ship_step()
        step["run"] = [command for command in step["run"] if "review_1.json" not in " ".join(command)
                       and "gauntlet.json" not in " ".join(command)]
        step["run"].append(at_head(".dev/{plan}/review_1.json", review or review_record(SHIP_LENSES)))
        if gauntlet:
            from runner.tests.helpers import GAUNTLET
            step["run"].append(at_head(".dev/{plan}/gauntlet.json", GAUNTLET))
        return step

    def test_an_incomplete_review_or_a_missing_gauntlet_never_reaches_done(self):
        from runner.tests.helpers import SHIP_LENSES, review_record
        incomplete = {**review_record(SHIP_LENSES), "completeness": "incomplete", "verdict": None,
                      "missing_lenses": ["security"], "expected_lenses": [*SHIP_LENSES, "security"]}
        for name, step, code in (("incomplete", self.ship_variant(review=incomplete), "review.incomplete"),
                                 ("no gauntlet", self.ship_variant(gauntlet=False), "gauntlet.invalid")):
            with self.subTest(name):
                scenario = happy_scenario()
                scenario["ship"] = [step, step, step, step, step, step]
                self.scenario(scenario)
                run = self.queued_run(plan=name.replace(" ", "-"))
                data = self.work(run)
                self.assertEqual((data["status"], data["stage"]), ("cancelled", "ship"))
                self.assertEqual({a["code"] for a in self.attempts(data, "ship")}, {code})
        self.assertTrue(all(pr["isDraft"] is False for pr in self.gh()["prs"]))


class NoteTests(WorkerTestCase):
    def test_a_note_survives_a_worker_crash_before_the_attempt_is_saved(self):
        from runner import worktree as wt
        self.scenario(happy_scenario())
        run = self.queued_run()
        (run.dir / "note").write_text("use the existing table\n")
        with mock.patch.object(Worker, "baseline", side_effect=wt.GitError("git rev-parse timed out")):
            with self.assertRaises(wt.GitError):
                Worker(self.home, run.id, grace=1).run()
        self.assertEqual((run.dir / "note").read_text(), "use the existing table\n")
        Worker(self.home, run.id, grace=1).run()
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "done", data["human"])
        self.assertEqual(data["human"]["note"], "use the existing table")
        self.assertFalse((run.dir / "note").exists())


class BoundaryTests(WorkerTestCase):
    def test_r3_scope_review_committing_a_source_file_parks_with_the_path(self):
        scenario = happy_scenario()
        step = review_step()
        step["files"]["src/rogue.py"] = "print('unauthorized')\n"
        step["run"] = [["git", "add", "src/rogue.py"], ["git", "commit", "--quiet", "-m", "rogue"]]
        scenario["scope-review"] = [step]
        self.scenario(scenario)
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual((data["status"], data["stage"]), ("needs-human", "scope-review"))
        review = self.attempts(data, "scope-review")[0]
        self.assertEqual((review["outcome"], review["code"]), ("blocked", "boundary.violation"))
        self.assertIn("src/rogue.py (added, committed)", review["reason"])
        self.assertEqual(len(self.attempts(data, "build")), 0)

    def test_attempts_record_their_start_and_sandbox_capabilities(self):
        run = self.queued_run()
        data = self.work(run)
        review = self.attempts(data, "scope-review")[0]
        self.assertEqual(review["baseline"]["branch"], data["branch"])
        self.assertEqual(len(review["baseline"]["head"]), 40)
        sandbox = review["sandbox"]
        self.assertEqual((sandbox["mode"], sandbox["enforced"]), ("workspace-write", True))
        self.assertNotIn(str(run.dir), sandbox["writable"])
        self.assertIn(str(run.report_dir), sandbox["writable"])
        call = next(c for c in self.stub_calls("codex") if c["argv"][0] == "exec")
        granted = [call["argv"][i + 1] for i, part in enumerate(call["argv"]) if part == "--add-dir"]
        self.assertNotIn(str(run.dir), granted)
        self.assertFalse(any(g.endswith(".git") for g in granted), granted)
        (self.home / "config.json").write_text(json.dumps({**self.fast_config, "repos": {
            data["repo"]: {"codex_sandbox": "bypass"}}}))
        bypass = self.queued_run(plan="bypass", repo=Path(data["repo"]))
        review = self.attempts(self.work(bypass), "scope-review")[0]
        self.assertEqual((review["sandbox"]["mode"], review["sandbox"]["enforced"], review["sandbox"]["protects"]),
                         ("bypass", False, []))


class RetryTests(WorkerTestCase):
    def test_block_then_pass(self):
        scenario = happy_scenario()
        broken = build_step()
        del broken["files"][".dev/{plan}/scenario-map.json"]
        scenario["build"] = [broken, {"files": build_step()["files"]}]
        self.scenario(scenario)
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done")
        builds = self.attempts(data, "build")
        self.assertEqual([a["outcome"] for a in builds], ["blocked", "done"])
        self.assertIn("scenario-map.json does not exist", builds[0]["reason"])
        self.assertEqual(data["retries"]["used"], 1)
        prompt = (run.attempt_dir("build", 2) / "prompt.txt").read_text()
        self.assertIn("Attempt 1 ended blocked:", prompt)
        self.assertIn("retry.scheduled", self.event_names(run))

    def test_budget_is_shared_across_stages(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [{"result": "omit"}, review_step(index=1)]
        no_report = build_step()
        del no_report["files"][".dev/{plan}/scenario-map.json"]
        scenario["build"] = [no_report, {"files": build_step()["files"], "exit": 1}, {"files": build_step()["files"]}]
        scenario["ship"] = [{"result": "omit"}, ship_step()]
        self.scenario(scenario)
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["retries"]["used"], 3)
        self.assertEqual([a["outcome"] for a in self.attempts(data, "build")], ["blocked", "done"])
        self.assertEqual(len(self.attempts(data, "scope-review")), 2)
        self.assertEqual(self.attempts(data, "scope-review")[0]["outcome"], "failed")

    def test_exhaustion_cancels_and_keeps_draft_pr(self):
        blocked = ship_step("BLOCK", draft=True, blockers="[security] app.py:3 - token logged\n")
        scenario = happy_scenario()
        scenario["ship"] = [blocked]
        self.scenario(scenario)
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "cancelled")
        ships = self.attempts(data, "ship")
        self.assertEqual(len(ships), 6)
        self.assertEqual(data["retries"], {"used": 5, "budget": 5})
        self.assertIn("retry budget exhausted (5/5)", data["human"]["reason"])
        self.assertIn("token logged", data["human"]["reason"])
        self.assertTrue(data["pr"]["draft"])
        self.assertEqual(data["pr"]["number"], 1)
        self.assertEqual(slots.holders(self.home / "slots"), {})
        self.assertTrue(run.worktree.is_dir())
        self.assertEqual(len(self.gh()["prs"]), 1)

    def test_non_retryable_result_parks(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [review_step("APPROVED WITH DEFERRALS",
                                                "### D1 - feasibility - wrong premise\nKind: premise\nNo webhooks exist.\n")]
        self.scenario(scenario)
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "needs-human")
        self.assertEqual(data["stage"], "scope-review")
        self.assertIn("wrong premise (premise)", data["human"]["reason"])
        self.assertEqual(data["retries"]["used"], 0)
        self.assertIn("run.needs_human", self.event_names(run))

    def test_missing_binary_parks(self):
        import os
        from unittest import mock
        run = self.queued_run()
        with mock.patch.dict(os.environ, {"FACTORY_CODEX_BIN": str(self.root / "no-codex")}):
            data = self.work(run)
        self.assertEqual(data["status"], "needs-human")
        self.assertIn("no-codex", data["human"]["reason"])
        self.assertEqual(self.stub_calls("codex"), [])

    def test_timeout_is_retryable(self):
        scenario = happy_scenario()
        scenario["build"] = [{"sleep": 30, "ignore_term": True}, build_step()]
        self.scenario(scenario)
        pipeline = dict(PIPELINE)
        # One deadline per attempt: the agent's allowance is 6 of its 8 seconds, so the sleeping first attempt times
        # out while the second still has room for the runner's own tests, e2e driver, and validation.
        pipeline["build"] = dataclasses.replace(PIPELINE["build"], timeout_s=8)
        run = self.queued_run(stage="build")
        started = time.monotonic()
        data = self.work(run, pipeline=pipeline, grace=0.5)
        self.assertLess(time.monotonic() - started, 30)
        builds = self.attempts(data, "build")
        self.assertEqual(builds[0]["outcome"], "timeout")
        self.assertEqual(builds[0]["exit_code"], -9)
        self.assertEqual(data["status"], "done")

    def test_legacy_token_ceiling_config_does_not_park_after_attempt(self):
        (self.home / "config.json").write_text(json.dumps({**self.fast_config, "max_tokens_per_run": 500}))
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done")


class CancellationTests(WorkerTestCase):
    def test_cancel_while_queued(self):
        (self.home / "config.json").write_text(json.dumps({**self.fast_config, "max_concurrent_stages": 1}))
        holder = slots.try_acquire(self.home / "slots", "someone-else", 1)
        run = self.queued_run()
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("code", Worker(self.home, run.id).run()))
        thread.start()
        self.wait_for(lambda: supervise.worker_alive(run.dir), 10, "worker lock")
        time.sleep(0.3)
        self.assertEqual(Run.load(run.dir).status, "queued")
        self.assertEqual(self.cli("cancel", run.id), 0)
        thread.join(timeout=20)
        holder.release()
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "cancelled")
        self.assertEqual(data["attempts"], [])
        self.assertIn("while queued", data["human"]["reason"])

    def test_cancel_mid_stage_terminates_the_process_group(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [{"sleep": 60}]
        self.scenario(scenario)
        run = self.queued_run()
        thread = threading.Thread(target=lambda: Worker(self.home, run.id, grace=1).run())
        thread.start()
        self.wait_event(run, "process.spawned")
        pid = Run.load(run.dir).data["attempts"][0]["pid"]
        self.assertEqual(self.cli("cancel", run.id), 0)
        thread.join(timeout=20)
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "cancelled")
        self.assertEqual(data["attempts"][0]["outcome"], "cancelled")
        self.assertFalse(supervise.pid_alive(pid))
        self.assertEqual(slots.holders(self.home / "slots"), {})
        self.assertTrue(run.worktree.is_dir())

    def test_duplicate_worker_is_excluded(self):
        run = self.queued_run()
        lock = supervise.WorkerLock(run.dir)
        self.assertTrue(lock.acquire())
        try:
            self.assertEqual(Worker(self.home, run.id).run(), 3)
            self.assertEqual(Run.load(run.dir).status, "queued")
        finally:
            lock.release()


class OrphanTests(WorkerTestCase):
    def test_orphaned_attempt_resumes_without_duplicate_workers(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [review_step(), {**review_step(index=2), "sleep": 2}]
        self.scenario(scenario)
        run = self.queued_run()
        run.data["status"] = "running"
        attempt = run.begin_attempt("scope-review", host="codex", model="m", effort="low")
        attempt["pid"] = 999999
        run.save()
        self.assertEqual(self.cli("resume", run.id, "--detach"), 0)
        self.assertTrue(supervise.worker_alive(run.dir))
        self.assertEqual(self.cli("resume", run.id), 1)
        self.assertEqual(Worker(self.home, run.id).run(), 3)
        data = self.wait_status(run.dir, ("done", "needs-human", "cancelled"))
        self.assertEqual(data["status"], "done")
        reviews = self.attempts(data, "scope-review")
        self.assertEqual(reviews[0]["outcome"], "failed")
        self.assertIn("orphaned", reviews[0]["reason"])
        self.assertEqual(data["retries"]["used"], 1)
        self.assertIn("run.resumed", self.event_names(run))


class ParkConditionTests(WorkerTestCase):
    """R2: a typed park condition participates in completion and needs an explicit, verified resolution."""

    KEY = "FACTORY_TEST_SANDBOX_KEY"

    def blocked_build(self, **condition) -> dict:
        step = build_step()
        step["result"] = {"status": "blocked", "reason": "payment sandbox needs credentials", "conditions": [{
            "code": "environment.missing_credentials", "summary": f"{self.KEY} is not available",
            "evidence": ["curl sandbox.example returned 401"], **condition}]}
        return step

    def show(self, run: Run) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            cli.main(["show", run.id])
        return stdout.getvalue()

    def test_a_launch_problem_the_runners_own_e2e_disproves_does_not_park(self):
        step = build_step()
        step["result"] = {"status": "blocked", "reason": "no browser in this sandbox", "conditions": [{
            "code": "launch.unavailable", "summary": "The required e2e browser could not launch in this host.",
            "evidence": ["Chrome exited before DevToolsActivePort"]}]}
        scenario = happy_scenario()
        scenario["build"] = [step]
        self.scenario(scenario)
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done", data["human"])
        condition = data["conditions"][0]
        self.assertEqual((condition["code"], condition["status"], condition["resolution"]["by"]),
                         ("launch.unavailable", "resolved", "runner"))
        self.assertIn("the runner's e2e driver passed at the gate", condition["resolution"]["evidence"][0])
        self.assertEqual(data["retries"]["used"], 0)
        build = self.attempts(data, "build")[0]
        self.assertEqual(build["outcome"], "done")
        self.assertIn("skill reported blocked on launch.unavailable, which the runner resolved", build["warning"])

    def test_r2_unresolved_condition_parks_despite_a_passing_gate(self):
        scenario = happy_scenario()
        scenario["build"] = [self.blocked_build(requires_env=[self.KEY])]
        self.scenario(scenario)
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual((data["status"], data["stage"]), ("needs-human", "build"))
        build = self.attempts(data, "build")[0]
        self.assertTrue(json.loads((run.attempt_dir("build", 1) / "gate.json").read_text())["gate"]["passed"])
        self.assertEqual((build["outcome"], build["code"], build["conditions"]),
                         ("blocked", "environment.missing_credentials", ["C1"]))
        self.assertEqual(data["human"]["reason"],
                         f"environment.missing_credentials (C1): {self.KEY} is not available")
        self.assertEqual(data["retries"]["used"], 0)
        shown = self.show(run)
        self.assertIn(f"C1 environment.missing_credentials (unresolved, raised at build attempt 1): {self.KEY} is not "
                      "available", shown)
        self.assertIn("evidence: curl sandbox.example returned 401", shown)
        self.assertIn(f"next: provide the credentials to the runner's environment, then `factory retry {run.id}`",
                      shown)
        from runner import watch
        viewer = watch.Watcher(run.dir, out=io.StringIO(), poll=0.01)
        self.assertEqual(viewer.run(), 1)
        self.assertIn(f"  C1 environment.missing_credentials: {self.KEY} is not available\n"
                      "    evidence: curl sandbox.example returned 401\n"
                      f"> provide the credentials to the runner's environment, then `factory retry {run.id}`",
                      viewer.out.getvalue())

    def test_clearing_a_real_prerequisite_permits_progress_after_the_runner_re_checks_it(self):
        scenario = happy_scenario()
        scenario["build"] = [self.blocked_build(requires_env=[self.KEY]),
                             {**build_step(), "result": {"status": "done", "reason": "credentials present",
                                                          "conditions": [{"code": "environment.missing_credentials",
                                                                          "summary": "fixed", "resolution": "resolved",
                                                                          "resolves": "C1", "evidence": ["401 gone"]}]}},
                             build_step()]
        self.scenario(scenario)
        run = self.queued_run()
        self.work(run)
        self.assertEqual(self.cli("retry", run.id, "--detach"), 0)
        data = self.wait_status(run.dir, ("done", "needs-human", "cancelled"))
        self.assertEqual(data["status"], "needs-human", "a claimed fix the runner can disprove must not pass")
        self.assertEqual(data["conditions"][0]["status"], "unresolved")
        self.assertIn(f"still unset in the runner's environment: {self.KEY}", self.show(run))
        warnings = [e["data"]["warning"] for e in events.read(run.dir) if e["event"] == "stage.warning"]
        self.assertTrue(any("C1 claimed resolved, but the runner's re-check found it" in w for w in warnings))
        with mock.patch.dict(os.environ, {self.KEY: "sk_test_123"}):
            self.assertEqual(self.cli("retry", run.id, "--reset-budget", "--detach"), 0)
            data = self.wait_status(run.dir, ("done", "needs-human", "cancelled"))
        self.assertEqual(data["status"], "done", data["human"])
        condition = data["conditions"][0]
        self.assertEqual((condition["status"], condition["resolution"]["by"], condition["resolution"]["attempt"]),
                         ("resolved", "runner", 3))
        self.assertIn(f"resolved at attempt 3 by runner: now set in the runner's environment: {self.KEY}",
                      self.show(run))

    def test_a_condition_the_runner_cannot_observe_needs_an_evidenced_resolution(self):
        premise = build_step()
        premise["result"] = {"status": "blocked", "reason": "no such webhook", "conditions": [
            {"code": "premise.invalidated", "summary": "the provider sends no delivery ids"}]}
        silent = build_step()
        resolved = {**build_step(), "result": {"status": "done", "reason": "premise confirmed", "conditions": [
            {"code": "premise.invalidated", "summary": "operator confirmed ids exist", "resolution": "resolved",
             "resolves": "C1", "evidence": ["operator note: ids are in the X-Delivery header"]}]}}
        scenario = happy_scenario()
        scenario["build"] = [premise, silent, resolved]
        self.scenario(scenario)
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "needs-human")
        self.assertEqual(self.cli("retry", run.id, "--note", "ids are in X-Delivery", "--detach"), 0)
        data = self.wait_status(run.dir, ("done", "needs-human", "cancelled"))
        self.assertEqual(data["status"], "needs-human", "an unaddressed condition keeps blocking")
        self.assertEqual(self.attempts(data, "build")[1]["code"], "premise.invalidated")
        self.assertEqual(self.cli("retry", run.id, "--detach"), 0)
        data = self.wait_status(run.dir, ("done", "needs-human", "cancelled"))
        self.assertEqual(data["status"], "done", data["human"])
        self.assertEqual(data["conditions"][0]["resolution"]["by"], "skill")
        context = json.loads((run.worktree / ".dev" / "factory-run.json").read_text())
        self.assertEqual(context["conditions"], [])
        prompt = (run.attempt_dir("build", 3) / "prompt.txt").read_text()
        self.assertIn("Open condition C1 premise.invalidated: the provider sends no delivery ids.", prompt)

    def test_an_unknown_condition_code_blocks_until_the_skill_writes_a_valid_result(self):
        unknown = build_step()
        unknown["result"] = {"status": "blocked", "reason": "x", "conditions": [{"code": "human.please", "summary": "?"}]}
        scenario = happy_scenario()
        scenario["build"] = [unknown, build_step()]
        self.scenario(scenario)
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done", data["human"])
        builds = self.attempts(data, "build")
        self.assertEqual([(b["outcome"], b["code"]) for b in builds], [("blocked", "result.invalid"), ("done", None)])
        self.assertIn("the stage result is invalid: build-result.json is malformed", builds[0]["reason"])
        self.assertIn("unknown condition code 'human.please'", builds[0]["reason"])
        self.assertEqual(data["retries"]["used"], 1)

    def test_decision_failures_block_until_the_skill_resolves_them(self):
        decision = build_step()
        decision["result"] = {"status": "blocked", "reason": "applied retry policy failed", "conditions": [
            {"code": "decision.verification_failed", "summary": "exponential backoff broke the idempotency test"}]}
        fixed = {**build_step(), "result": {"status": "done", "reason": "ok", "conditions": [
            {"code": "decision.verification_failed", "summary": "fixed", "resolution": "resolved", "resolves": "C1",
             "evidence": ["tests/test_webhook.py passes"]}]}}
        scenario = happy_scenario()
        scenario["build"] = [decision, fixed]
        self.scenario(scenario)
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done", data["human"])
        builds = self.attempts(data, "build")
        self.assertEqual([b["code"] for b in builds], ["decision.verification_failed", None])
        self.assertEqual(data["retries"]["used"], 1)


if __name__ == "__main__":
    unittest.main()
