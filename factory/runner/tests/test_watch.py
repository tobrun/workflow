import contextlib
import io
import json
import re
import threading
import unittest

from runner import cli, supervise, watch
from runner.model import Run
from runner.tests.helpers import FactoryTestCase, happy_scenario, make_repo, ship_step
from runner.worker import Worker

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class TtyBuffer(io.StringIO):
    def isatty(self):
        return True


class WatchTestCase(FactoryTestCase):
    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class AttachedCliTests(WatchTestCase):
    def test_new_stays_attached_until_the_pr_is_ready(self):
        repo = make_repo(self.root)
        code, stdout, _ = self.call("new", str(repo), "Add idempotency", "--plan", "webhook", "--yes")
        self.assertEqual(code, 0, stdout)
        lines = stdout.splitlines()
        self.assertIn("Watching the assembly line; Ctrl-C detaches and the run keeps going.", stdout)
        order = ["scope-review attempt 1 started (openai.gpt-5.6-sol/low)", "scope-review attempt 1 done",
                 "build queued after scope-review", "build attempt 1 started", "ship attempt 1 done",
                 "done: https://github.com/stub/repo/pull/1"]
        positions = [next(i for i, line in enumerate(lines) if text in line) for text in order]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(lines[-1], "Done: https://github.com/stub/repo/pull/1")
        self.assertIsNone(ANSI.search(stdout))
        run_dir = next((self.home / "runs").iterdir())
        self.assertFalse(supervise.worker_alive(run_dir))

    def test_attached_run_that_exhausts_retries_exits_nonzero(self):
        scenario = happy_scenario()
        scenario["ship"] = [ship_step("BLOCK", draft=True, blockers="[x] a.py:1 - replay window unbounded\n")]
        self.scenario(scenario)
        repo = make_repo(self.root)
        code, stdout, _ = self.call("new", str(repo), "Add idempotency", "--plan", "webhook", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("retry 5/5 scheduled for ship", stdout)
        self.assertIn("Cancelled at ship: retry budget exhausted (5/5)", stdout)
        self.assertTrue(stdout.rstrip().endswith("--reset-budget"))

    def test_detach_returns_after_handoff(self):
        repo = make_repo(self.root)
        code, stdout, _ = self.call("new", str(repo), "Add idempotency", "--plan", "webhook", "--yes", "--detach")
        self.assertEqual(code, 0)
        self.assertIn("Handed off: worker", stdout)
        self.assertNotIn("Watching the assembly line", stdout)
        run_dir = next((self.home / "runs").iterdir())
        self.wait_status(run_dir, ("done",))

    def test_watch_command_on_finished_and_scoping_runs(self):
        run = self.queued_run()
        Worker(self.home, run.id).run()
        code, stdout, _ = self.call("watch", run.id)
        self.assertEqual(code, 0)
        self.assertTrue(stdout.startswith("Done: pull request"))
        scoping = Run.create(self.home / "runs", repo="/tmp/r", request="x", plan="later")
        scoping.data["status"] = "scoping"
        scoping.save()
        code, _, err = self.call("watch", scoping.id)
        self.assertEqual(code, 1)
        self.assertIn("nothing runs unattended until scope hands off", err)


class WatcherTests(WatchTestCase):
    def test_recent_failures_and_warnings_stay_visible_with_evidence_paths(self):
        run = self.queued_run()
        run.data.update({"status": "running", "stage": "build"})
        failed = {
            "stage": "build", "n": 1, "host": "codex", "started_at": run.data["created_at"],
            "ended_at": run.data["created_at"], "outcome": "blocked", "code": "e2e.failed",
            "reason": "duplicate delivery scenario failed after the browser disconnected",
        }
        warned = {
            "stage": "build", "n": 2, "host": "codex", "started_at": run.data["created_at"],
            "ended_at": run.data["created_at"], "outcome": "done",
            "warning": "skill reported blocked: screenshot parity command exhausted the hosted browser file descriptors",
        }
        run.data["attempts"].extend([failed, warned])
        for n in (1, 2):
            attempt_dir = run.attempt_dir("build", n)
            attempt_dir.mkdir(parents=True)
            (attempt_dir / "gate.json").write_text("{}")
            (attempt_dir / "stderr.log").write_text("")
        run.save()

        lines = watch.attention_lines(run, watch.Style(color=False), 100)
        text = "\n".join(lines)
        self.assertIn("build diagnostics (persisted for this run)", text)
        self.assertIn("build attempt 1 blocked [e2e.failed]", text)
        self.assertIn("duplicate delivery scenario failed after the browser", text)
        self.assertIn("disconnected", text)
        self.assertIn("build attempt 2 done: skill reported blocked", text)
        self.assertIn("attempts/build-1/gate.json, stderr.log", text)
        self.assertIn(f"factory logs {run.id} --stage build --attempt 1", text)
        self.assertTrue(all(len(ANSI.sub("", line)) <= 100 for line in lines))

        prior = dict(failed, n=0, code="setup.failed", reason="dependency setup could not install packages")
        oldest = dict(failed, n=-1, code="setup.failed", reason="the original setup did not finish")
        run.data["attempts"][0:0] = [oldest, prior]
        self.assertEqual([attempt["n"] for attempt in watch.attention_attempts(run)], [2, 1, 0])
        self.assertEqual([attempt["n"] for attempt in watch.attention_attempts(run, all_attempts=True)],
                         [2, 1, 0, -1])

        run.data.update({"stage": "ship"})
        self.assertEqual(watch.attention_attempts(run), [])

    def test_tty_view_redraws_stage_rows_in_place(self):
        scenario = happy_scenario()
        scenario["build"][0]["sleep"] = 1.5
        self.scenario(scenario)
        run = self.queued_run()
        worker = threading.Thread(target=lambda: Worker(self.home, run.id).run())
        worker.start()
        self.wait_for(lambda: supervise.worker_alive(run.dir), 10, "worker lock")
        buffer = TtyBuffer()
        code = watch.Watcher(run.dir, out=buffer, poll=0.1, width=100).run()
        worker.join(timeout=20)
        text = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("\x1b[", text)
        plain = ANSI.sub("", text)
        self.assertRegex(plain, r"▶ build\s+running\s+attempt 1")
        self.assertRegex(plain, r"✓ scope-review\s+done")
        self.assertRegex(plain, r"· ship")
        self.assertIn("worker alive", plain)
        self.assertRegex(plain, r"now: (agent: |\$ git)")
        self.assertRegex(plain, r"✓ ship\s+done")
        for line in plain.splitlines():
            self.assertLessEqual(len(line), 100)

    def test_the_last_lines_of_an_attempt_that_ends_between_polls_are_still_shown(self):
        run = self.queued_run()
        run.data["status"] = "running"
        run.data["stage"] = "build"
        run.data["attempts"].append({"stage": "build", "n": 1, "host": "codex", "started_at": run.data["created_at"],
                                     "ended_at": None})
        run.save()
        stream = run.attempt_dir("build", 1) / "stdout.jsonl"
        stream.parent.mkdir(parents=True)

        def command(text):
            return json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": text}}) + "\n"

        def exit(code):
            return json.dumps({"type": "item.completed",
                               "item": {"type": "command_execution", "exit_code": code,
                                        "status": "failed" if code else "completed"}}) + "\n"

        stream.write_text(command("pytest -q") + exit(1))
        polls = []

        def between_polls(_seconds):
            polls.append(1)
            if len(polls) == 1:
                with stream.open("a") as handle:
                    handle.write(command("git add -A"))
                data = json.loads((run.dir / "run.json").read_text())
                data["attempts"][-1]["ended_at"] = data["created_at"]
                data["status"], data["stage"] = "done", "ship"
                (run.dir / "run.json").write_text(json.dumps(data))

        viewer = watch.Watcher(run.dir, out=io.StringIO(), poll=0, width=120, sleep=between_polls)
        viewer.verbose = True
        viewer.run()
        plain = ANSI.sub("", viewer.out.getvalue())
        self.assertIn("| $ pytest -q — exit 1 (failed)", plain)
        self.assertNotIn("| $ git add -A", plain)

    def test_ctrl_c_detaches_without_touching_the_run(self):
        run = self.queued_run()
        (run.dir / "worker.lock").touch()

        def interrupt(_seconds):
            raise KeyboardInterrupt

        buffer = io.StringIO()
        code = watch.Watcher(run.dir, out=buffer, sleep=interrupt).run()
        self.assertEqual(code, 0)
        self.assertIn(f"Detached from {run.id}; the run keeps going.", buffer.getvalue())
        self.assertIn(f"> factory watch {run.id}", buffer.getvalue())
        self.assertEqual(Run.load(run.dir).status, "queued")
        self.assertFalse((run.dir / "cancel").exists())

    def test_dead_worker_is_reported_with_resume_hint(self):
        run = self.queued_run()
        buffer = io.StringIO()
        code = watch.Watcher(run.dir, out=buffer, sleep=lambda s: None).run()
        self.assertEqual(code, 2)
        self.assertIn(f"The worker for {run.id} stopped while the run is queued at scope-review.", buffer.getvalue())
        self.assertIn(f"> factory resume {run.id}", buffer.getvalue())

    def test_history_is_shown_when_attaching_later(self):
        run = self.queued_run()
        Worker(self.home, run.id).run()
        buffer = io.StringIO()
        watch.Watcher(run.dir, out=buffer, sleep=lambda s: None).run()
        lines = buffer.getvalue().splitlines()
        self.assertIn("done: https://github.com/stub/repo/pull/1", lines[4])
        self.assertEqual(lines[-1], "Done: https://github.com/stub/repo/pull/1")

    def test_stream_tail_reads_incrementally(self):
        path = self.root / "stdout.jsonl"
        tail = watch.StreamTail()
        path.write_text(json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "npm   test"}})
                        + "\n" + '{"type": "turn.comp')
        tail.follow(path)
        self.assertEqual(tail.latest, "$ npm test")
        with path.open("a") as handle:
            handle.write('leted", "usage": {"input_tokens": 1200, "output_tokens": 300}}\n')
        tail.follow(path)
        self.assertEqual(tail.tokens, 1500)
        self.assertEqual(watch.short_tokens(1500), "1.5k")

    def test_stream_tail_keeps_milestones_and_failures_not_routine_commands(self):
        path = self.root / "stdout.jsonl"
        path.write_text(
            json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "npm test"}}) + "\n"
            + json.dumps({"type": "item.completed", "item": {"type": "command_execution", "exit_code": 0}}) + "\n"
            + json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "npm run lint"}}) + "\n"
            + json.dumps({"type": "item.completed", "item": {"type": "command_execution", "exit_code": 1,
                                                               "status": "failed"}}) + "\n"
            + json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                                                              "text": "Lint failure is isolated to one new rule."}}) + "\n"
        )
        tail = watch.StreamTail()
        tail.follow(path)
        text = "\n".join(tail.drain())
        self.assertNotIn("npm test", text)
        self.assertIn("$ npm run lint — exit 1 (failed)", text)
        self.assertIn("agent: Lint failure is isolated to one new rule.", text)
        self.assertEqual(tail.latest_agent, "agent: Lint failure is isolated to one new rule.")


if __name__ == "__main__":
    unittest.main()
