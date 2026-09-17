import contextlib
import io
import json
import unittest

from runner import cli, events, supervise
from runner.model import Run
from runner.tests.helpers import SPEC, FactoryTestCase, git, happy_scenario, make_repo, review_step, scope_files


class CliTestCase(FactoryTestCase):
    def call(self, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def parked(self, status: str, stage: str = "scope-review", used: int = 0, attempts: int = 1,
               plan: str = "webhook") -> Run:
        run = self.queued_run(stage=stage, plan=plan)
        for _ in range(attempts):
            attempt = run.begin_attempt(stage, host="codex", model="m", effort="low")
            run.finish_attempt(attempt, outcome="blocked", reason="panel escalated", retryable=False, source="gate")
        run.data["status"] = status
        run.data["human"]["reason"] = "panel escalated"
        run.data["retries"]["used"] = used
        run.save()
        return run


class LookupTests(CliTestCase):
    def make(self, plan: str) -> Run:
        return Run.create(self.home / "runs", repo="/tmp/r", request="x", plan=plan)

    def test_exact_prefix_and_ambiguous(self):
        alpha = self.make("alpha")
        self.make("alpine")
        self.assertEqual(cli.resolve(self.home, alpha.id), alpha.dir)
        prefix = alpha.id[:-2]
        code, _, err = self.call("show", alpha.id.rsplit("-", 1)[0] + "-alp")
        self.assertEqual(code, 1)
        self.assertIn("ambiguous run id", err)
        self.assertIn("alpine", err)
        self.assertEqual(cli.resolve(self.home, alpha.id.rsplit("-", 1)[0] + "-alph"), alpha.dir)
        self.assertTrue(prefix)

    def test_rejects_paths_and_globs(self):
        for bad in ("../x", "2026*", "a?b", "/tmp", ".."):
            with self.subTest(bad=bad):
                code, _, err = self.call("rm", bad, "--force")
                self.assertEqual(code, 2)
                self.assertIn("not a run id", err)

    def test_unknown_run(self):
        code, _, err = self.call("show", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no run matches", err)


class OutputTests(CliTestCase):
    def test_json_output_is_stable(self):
        run = Run.create(self.home / "runs", repo="/tmp/r", request="x", plan="stable")
        run.data["status"] = "needs-human"
        run.data["human"]["reason"] = "why"
        run.save()
        code, stdout, _ = self.call("ls", "--json")
        self.assertEqual(code, 0)
        rows = json.loads(stdout)
        self.assertEqual(sorted(rows[0]), sorted([
            "archived", "blocker", "branch", "cached_input_tokens", "created_at", "decision", "duration", "execution_alive",
            "gate_seconds", "group", "heartbeat_age", "id", "next", "note", "order", "overrides", "pr", "queue_seconds", "repo", "retries", "stage", "stale", "status", "tokens", "updated_at", "worker_alive",
        ]))
        code, stdout, _ = self.call("show", run.id, "--json")
        payload = json.loads(stdout)
        self.assertEqual(sorted(payload), ["run", "summary"])
        self.assertEqual(payload["run"]["id"], run.id)

    def test_ls_prints_outcome_first_and_next_command(self):
        run = self.parked("needs-human")
        code, stdout, _ = self.call("ls")
        lines = stdout.splitlines()
        self.assertEqual(lines[0], "1 need you, 0 in flight.")
        self.assertIn(f"! {run.id}  scope-review  needs-human  retries 0/5", stdout)
        self.assertIn(f'> factory retry {run.id} --note "..."', stdout)

    def test_show_names_blocker_and_next_command(self):
        run = self.parked("needs-human")
        code, stdout, _ = self.call("show", run.id)
        self.assertTrue(stdout.startswith("Needs you at scope-review: panel escalated"))
        self.assertIn("scope-review-1  blocked", stdout)
        self.assertIn(f'> factory retry {run.id} --note "..."', stdout)

    def test_logs_render_and_raw(self):
        run = self.queued_run()
        from runner.worker import Worker
        Worker(self.home, run.id).run()
        code, stdout, _ = self.call("logs", run.id, "--stage", "build")
        self.assertIn("== build-1 done", stdout)
        self.assertIn("$ git add -A", stdout)
        self.assertNotIn("stub.unknown_event", stdout)
        code, stdout, _ = self.call("logs", run.id, "--stage", "build", "--raw")
        self.assertIn("stub.unknown_event", stdout)
        code, stdout, _ = self.call("logs", run.id, "-f")
        self.assertIn("Done: pull request", stdout)


class OperatorTests(CliTestCase):
    def test_retry_with_note_reaches_the_prompt(self):
        run = self.parked("needs-human")
        code, stdout, _ = self.call("retry", run.id, "--note", "use the existing table")
        self.assertEqual(code, 0, stdout)
        data = self.wait_status(run.dir, ("done", "needs-human", "cancelled"))
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["retries"]["used"], 1)
        prompt = (run.attempt_dir("scope-review", 2) / "prompt.txt").read_text()
        self.assertIn("Operator note: use the existing table.", prompt)
        self.assertIn("Attempt 1 ended blocked: panel escalated.", prompt)
        context_note = [e for e in events.read(run.dir) if e["event"] == "run.retry_requested"]
        self.assertEqual(context_note[0]["data"]["note"], "use the existing table")

    def test_exhausted_budget_requires_reset(self):
        run = self.parked("cancelled", used=5, attempts=6)
        code, _, err = self.call("retry", run.id)
        self.assertEqual(code, 1)
        self.assertIn("--reset-budget", err)
        self.assertEqual(Run.load(run.dir).status, "cancelled")
        self.scenario({**happy_scenario(), "scope-review": [review_step()]})
        code, stdout, _ = self.call("retry", run.id, "--reset-budget")
        self.assertEqual(code, 0)
        data = self.wait_status(run.dir, ("done", "needs-human", "cancelled"))
        self.assertEqual(data["retries"]["used"], 1)
        self.assertEqual(data["status"], "done")

    def test_retry_refuses_active_runs(self):
        run = self.queued_run()
        code, _, err = self.call("retry", run.id)
        self.assertEqual(code, 1)
        self.assertIn("only needs-human or cancelled", err)

    def test_resume_refuses_live_worker(self):
        run = self.queued_run()
        lock = supervise.WorkerLock(run.dir)
        self.assertTrue(lock.acquire())
        try:
            code, _, err = self.call("resume", run.id)
        finally:
            lock.release()
        self.assertEqual(code, 1)
        self.assertIn("is alive", err)

    def test_resume_reports_unrelated_live_pid(self):
        import os
        run = self.queued_run()
        (run.dir / "worker.pid").write_text(str(os.getppid()))
        code, stdout, _ = self.call("resume", run.id)
        self.assertEqual(code, 0)
        self.assertIn("does not hold the worker lock", stdout)
        self.wait_status(run.dir, ("done",))

    def test_cancel_without_worker_is_immediate(self):
        run = self.queued_run()
        code, stdout, _ = self.call("cancel", run.id)
        self.assertEqual(code, 0)
        self.assertTrue(stdout.startswith(f"Cancelled {run.id}"))
        self.assertEqual(Run.load(run.dir).status, "cancelled")
        self.assertIn("run.cancel_requested", [e["event"] for e in events.read(run.dir)])

    def test_gc_dry_run_then_archive_only_merged(self):
        merged = self.queued_run(plan="merged")
        from runner.worker import Worker
        Worker(self.home, merged.id).run()
        self.assertEqual(Run.load(merged.dir).status, "done")
        state = self.gh()
        pending = Run.create(self.home / "runs", repo=Run.load(merged.dir).data["repo"], request="x", plan="unmerged")
        pending.data.update({"status": "done", "stage": "ship"})
        pending.data["pr"].update({"number": 2, "url": "https://github.com/stub/repo/pull/2"})
        pending.save()
        state["prs"].append({"number": 2, "url": "https://github.com/stub/repo/pull/2", "isDraft": False,
                             "headRefName": "factory/unmerged", "state": "OPEN", "mergedAt": None})
        self.gh_state.write_text(json.dumps(state))
        import subprocess
        subprocess.run([self.env["FACTORY_GH_BIN"], "pr", "merge", "1"], check=True, capture_output=True)

        code, stdout, _ = self.call("gc", "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(stdout.startswith("Would archive 1 merged run(s); kept 1 completed run(s)."))
        self.assertTrue(merged.worktree.is_dir())

        code, stdout, _ = self.call("gc")
        self.assertIn(f"archived {merged.id}", stdout)
        self.assertIn(f"kept {pending.id}: PR #2 is not merged", stdout)
        archived = self.home / "archive" / merged.id
        self.assertTrue((archived / "run.json").is_file())
        self.assertTrue((archived / "events.jsonl").is_file())
        self.assertTrue((archived / "plan" / "spec.md").is_file())
        self.assertTrue((archived / "plan" / "implementation-notes.md").is_file())
        self.assertFalse((archived / "worktree").exists())
        self.assertFalse(merged.dir.exists())
        self.assertTrue(pending.dir.exists())
        repo = Run.load(archived).data["repo"]
        self.assertEqual(git(__import__("pathlib").Path(repo), "branch", "--list", "factory/merged"), "")
        self.assertIn("gc.archived", [e["event"] for e in events.read(archived)])
        code, stdout, _ = self.call("show", merged.id)
        self.assertEqual(code, 0)

    def test_rm_force_targets_exactly_one_run(self):
        keep = self.queued_run(plan="keep-me")
        drop = self.queued_run(plan="drop")
        code, _, err = self.call("rm", drop.id)
        self.assertEqual(code, 2)
        self.assertIn("--force", err)
        code, stdout, _ = self.call("rm", drop.id, "--force")
        self.assertEqual(code, 0)
        self.assertFalse(drop.dir.exists())
        self.assertTrue(keep.dir.exists())
        self.assertTrue(keep.worktree.is_dir())
        self.assertIn("remote branch and pull request are untouched", stdout)


class NewAndScopeTests(CliTestCase):
    def test_preflight_failure_creates_nothing(self):
        state = self.gh()
        state["auth"] = False
        self.gh_state.write_text(json.dumps(state))
        repo = make_repo(self.root)
        code, stdout, _ = self.call("new", str(repo), "Add idempotency", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("Preflight failed", stdout)
        self.assertIn("gh auth login", stdout)
        self.assertEqual(list((self.home / "runs").glob("*")), [])

    def test_tracked_plan_files_refuse_only_a_run_that_would_reuse_their_directory(self):
        repo = make_repo(self.root, "tracked-plans")
        (repo / ".dev" / "old").mkdir(parents=True)
        (repo / ".dev" / "old" / "spec.md").write_text("# old\n")
        git(repo, "add", "-f", ".dev/old/spec.md")
        git(repo, "commit", "--quiet", "-m", "tracked plan")
        git(repo, "push", "--quiet")
        code, stdout, _ = self.call("new", str(repo), "Add idempotency", "--plan", "old", "--yes", "--detach")
        self.assertEqual(code, 2)
        self.assertIn("the base already tracks this run's plan directory .dev/old/: .dev/old/spec.md", stdout)
        self.assertIn("repair: choose another name with --plan, or move them out of .dev/", stdout)
        self.assertEqual(list((self.home / "runs").glob("*")), [])

        self.claude([scope_files()])
        code, stdout, _ = self.call("new", str(repo), "Add idempotency", "--plan", "webhook", "--yes", "--detach")
        self.assertIn("Note: the base tracks other plan files under .dev/ (.dev/old/spec.md)", stdout)
        self.assertNotIn("Preflight failed", stdout)
        self.assertEqual(len(list((self.home / "runs").glob("*"))), 1)
        self.assertEqual(git(repo, "ls-files", ".dev"), ".dev/old/spec.md")

    def test_invalid_spec_stays_scoping_then_resume_hands_off(self):
        bad = SPEC.replace("  ✗ in-memory set - lost on every restart\n", "  ⚑ ask: which store?\n")
        good = scope_files()
        good["files"]["docs/decisions.md"] = "# Decisions\n"
        self.claude([scope_files(bad), good])
        repo = make_repo(self.root)
        code, stdout, _ = self.call("new", str(repo), "Add idempotency protection to webhook processing", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Scope is not ready to hand off", stdout)
        self.assertIn("spec contains 1 ⚑ mark(s)", stdout)
        run_dir = next((self.home / "runs").iterdir())
        run = Run.load(run_dir)
        self.assertEqual(run.status, "scoping")
        self.assertEqual(run.plan, "add-idempotency-protection-webhook")
        session = run.data["scope_session_id"]

        code, stdout, _ = self.call("scope", run.id, "--resume", "--yes")
        self.assertEqual(code, 0, stdout)
        self.assertIn("Handed off", stdout)
        launches = self.stub_calls("claude")
        self.assertEqual(launches[0]["argv"][0], "/factory:scope Add idempotency protection to webhook processing")
        self.assertIn("--session-id", launches[0]["argv"])
        self.assertEqual(launches[1]["session"], session)
        self.assertTrue(launches[1]["resume"])
        data = self.wait_status(run_dir, ("done", "needs-human", "cancelled"))
        self.assertEqual(data["status"], "done")
        self.assertEqual([a["stage"] for a in data["attempts"]], ["scope", "scope", "scope-review", "build", "ship"])
        self.assertEqual(data["retries"]["used"], 0)
        subjects = git(run_dir / "worktree", "log", "--format=%s%n%b")
        self.assertIn("docs(scope): record decisions for add-idempotency-protection-webhook", subjects)
        self.assertNotIn("Co-Authored-By", subjects)
        self.assertEqual(git(run_dir / "worktree", "ls-files", ".dev"), "")
        author = git(run_dir / "worktree", "log", "-1", "--format=%an", "HEAD~1")
        self.assertEqual(author, "Factory Test")

    def test_cancel_during_scope_blocks_the_handoff(self):
        self.claude([{"files": {".dev/{plan}/spec.md": SPEC, "../cancel": "operator\n"}}])
        repo = make_repo(self.root)
        code, stdout, _ = self.call("new", str(repo), "Add idempotency", "--plan", "webhook", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Cancelled", stdout)
        run = Run.load(next((self.home / "runs").iterdir()))
        self.assertEqual(run.status, "cancelled")
        self.assertEqual(git(run.worktree, "log", "--format=%s"), "initial")
        self.assertFalse(supervise.worker_alive(run.dir))

    def test_handoff_needs_confirmation_without_yes(self):
        repo = make_repo(self.root)
        code, stdout, _ = self.call("new", str(repo), "Add idempotency", "--plan", "webhook")
        self.assertEqual(code, 1)
        self.assertIn("no terminal is available to confirm", stdout)
        run = Run.load(next((self.home / "runs").iterdir()))
        self.assertEqual(run.status, "scoping")
        self.assertEqual(git(run.worktree, "log", "--format=%s"), "initial")

    def test_worktree_failure_parks_with_repair(self):
        repo = make_repo(self.root)
        git(repo, "remote", "set-url", "origin", str(self.root / "missing.git"))
        code, stdout, _ = self.call("new", str(repo), "Add idempotency", "--plan", "webhook", "--yes")
        self.assertEqual(code, 1)
        run = Run.load(next((self.home / "runs").iterdir()))
        self.assertEqual(run.status, "needs-human")
        self.assertIn("worktree setup failed", run.data["human"]["reason"])
        self.assertIn("git -C", stdout)

    def test_plan_slug(self):
        self.assertEqual(cli.derive_plan("Add idempotency protection to webhook processing"),
                         "add-idempotency-protection-webhook")
        self.assertEqual(cli.derive_plan("!!!"), "change")


class DoctorTests(CliTestCase):
    def test_doctor_with_healthy_stubs(self):
        code, stdout, _ = self.call("doctor", "--json")
        payload = json.loads(stdout)
        failures = [c for c in payload["checks"] if c["status"] == "fail"]
        self.assertEqual(failures, [])
        self.assertEqual(code, 0)
        names = {c["check"] for c in payload["checks"]}
        for expected in ("python", "binary:codex", "codex:plugin", "claude:plugin", "gh:auth", "config", "runtime",
                         "flock", "slots", "dashboard"):
            self.assertIn(expected, names)

    def test_doctor_reports_unsafe_setup_without_modifying_it(self):
        (self.home / "config.json").write_text("{broken")
        state = self.gh()
        state["auth"] = False
        self.gh_state.write_text(json.dumps(state))
        code, stdout, _ = self.call("doctor")
        self.assertEqual(code, 1)
        self.assertIn("A new run is unsafe", stdout)
        self.assertIn("repair: gh auth login", stdout)
        self.assertEqual((self.home / "config.json").read_text(), "{broken")

    def test_doctor_flags_dead_workers_and_missing_worktrees(self):
        run = self.queued_run()
        import shutil
        shutil.rmtree(run.worktree)
        code, stdout, _ = self.call("doctor")
        self.assertIn(f"worker:{run.id}", stdout)
        self.assertIn(f"worktree:{run.id}", stdout)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
