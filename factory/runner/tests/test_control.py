import contextlib
import io
import re
import threading
import time
import unittest

from runner import cli, control, events, supervise, watch
from runner.model import Run
from runner.tests.helpers import FactoryTestCase, happy_scenario, review_step
from runner.worker import Worker

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class TtyBuffer(io.StringIO):
    def isatty(self):
        return True


class ScriptedKeys:
    """Feeds keys when a run predicate holds, and answers prompts in order."""

    def __init__(self, run_dir, script):
        self.run_dir = run_dir
        self.script = list(script)
        self.answers = []
        self.prompts = []

    def read(self, timeout):
        time.sleep(min(timeout, 0.05))
        if not self.script:
            return []
        predicate, keys, answers = self.script[0]
        if predicate(Run.load(self.run_dir)):
            self.script.pop(0)
            self.answers += answers
            return list(keys)
        return []

    def prompt(self, text, write):
        write(text + "\n")
        self.prompts.append(text)
        return self.answers.pop(0) if self.answers else ""


def status(*statuses):
    return lambda run: run.status in statuses


def running(stage):
    return lambda run: run.status == "running" and run.stage == stage


class ControlTestCase(FactoryTestCase):
    def names(self, run):
        return [e["event"] for e in events.read(run.dir)]

    def start(self, run):
        thread = threading.Thread(target=lambda: Worker(self.home, run.id).run())
        thread.start()
        self.wait_for(lambda: supervise.worker_alive(run.dir), 10, "worker lock")
        return thread

    def watcher(self, run, script, out=None, **kwargs):
        keys = ScriptedKeys(run.dir, script)
        viewer = watch.Watcher(run.dir, out=out or io.StringIO(), poll=0.05, keys=keys, width=120,
                               opener=lambda path: None, **kwargs)
        return viewer, keys


class PauseAndNoteTests(ControlTestCase):
    def test_pause_waits_for_the_attempt_then_continue_finishes_the_run(self):
        scenario = happy_scenario()
        scenario["build"][0]["sleep"] = 1.5
        self.scenario(scenario)
        run = self.queued_run()
        thread = self.start(run)
        out = TtyBuffer()
        viewer, keys = self.watcher(run, [
            (running("build"), "p", []),
            (status("paused"), "n", ["ship it with the existing evidence"]),
            (status("paused"), "r", []),
        ], out=out)
        code = viewer.run()
        thread.join(timeout=20)
        self.assertEqual(code, 0)
        data = Run.load(run.dir).data
        self.assertEqual(data["status"], "done")
        self.assertEqual([a["stage"] for a in data["attempts"]], ["scope-review", "build", "ship"])
        self.assertEqual(data["attempts"][1]["outcome"], "done")
        names = self.names(run)
        for name in ("run.pause_requested", "run.paused", "run.unpaused", "run.note_added"):
            self.assertIn(name, names)
        self.assertLess(names.index("stage.finished", names.index("run.pause_requested")), names.index("run.paused"))
        prompt = (run.attempt_dir("ship", 1) / "prompt.txt").read_text()
        self.assertIn("Operator note: ship it with the existing evidence.", prompt)
        plain = ANSI.sub("", out.getvalue())
        self.assertRegex(plain, r"‖ ship\s+paused")
        self.assertIn("keys: r continue  n note  c cancel  q quit", plain)
        self.assertIn("keys: p withdraw pause", plain)
        self.assertIn("paused before ship", plain)
        self.assertIn("continued at ship", plain)
        self.assertTrue(plain.rstrip().endswith("Done: https://github.com/stub/repo/pull/1"))

    def test_withdrawn_pause_never_takes_effect(self):
        scenario = happy_scenario()
        scenario["build"][0]["sleep"] = 1.0
        self.scenario(scenario)
        run = self.queued_run()
        thread = self.start(run)
        viewer, _ = self.watcher(run, [(running("build"), "p", []), (running("build"), "p", [])])
        self.assertEqual(viewer.run(), 0)
        thread.join(timeout=20)
        self.assertNotIn("run.paused", self.names(run))
        self.assertEqual(Run.load(run.dir).status, "done")


class CancelAndRetryTests(ControlTestCase):
    def test_cancel_needs_confirmation_and_the_view_stays_on_the_parked_run(self):
        scenario = happy_scenario()
        scenario["build"][0]["sleep"] = 30
        self.scenario(scenario)
        run = self.queued_run()
        thread = self.start(run)
        viewer, keys = self.watcher(run, [
            (running("build"), "c", ["n"]),
            (running("build"), "c", ["y"]),
            (lambda r: r.status == "cancelled" and not supervise.worker_alive(r.dir), "q", []),
        ])
        code = viewer.run()
        thread.join(timeout=20)
        self.assertEqual(code, 1)
        self.assertEqual(len(keys.prompts), 2)
        self.assertTrue(keys.prompts[0].startswith(f"Cancel {run.id}?"))
        data = Run.load(run.dir).data
        self.assertEqual(data["status"], "cancelled")
        self.assertEqual(data["attempts"][-1]["outcome"], "cancelled")
        self.assertIn("Cancelled at build: cancelled by operator", viewer.out.getvalue())

    def test_retry_a_needs_human_run_from_the_view(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [
            review_step("APPROVED WITH DEFERRALS", "### D1 - feasibility - premise\nKind: premise\nNo webhooks.\n"),
            review_step(index=2),
        ]
        self.scenario(scenario)
        run = self.queued_run()
        thread = self.start(run)
        viewer, keys = self.watcher(run, [
            (lambda r: r.status == "needs-human" and not supervise.worker_alive(r.dir), "r",
             ["the premise holds: the provider retries"]),
        ])
        code = viewer.run()
        thread.join(timeout=20)
        self.assertEqual(code, 0)
        data = Run.load(run.dir).data
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["retries"]["used"], 1)
        self.assertIn("Operator note: the premise holds: the provider retries.",
                      (run.attempt_dir("scope-review", 2) / "prompt.txt").read_text())
        self.assertIn("Queued", viewer.out.getvalue())

    def test_exhausted_budget_is_reported_and_b_resets_it(self):
        run = self.queued_run()
        for _ in range(6):
            attempt = run.begin_attempt("scope-review", host="codex", model="m", effort="low")
            run.finish_attempt(attempt, outcome="blocked", reason="stuck", retryable=True, source="gate", tokens=100)
        run.data.update({"status": "cancelled", "retries": {"used": 5, "budget": 5}})
        run.data["human"]["reason"] = "retry budget exhausted (5/5)"
        run.save()
        self.scenario({**happy_scenario(), "scope-review": [review_step()]})
        viewer, _ = self.watcher(run, [(status("cancelled"), "r", [""]), (status("cancelled"), "b", [""])])
        self.assertEqual(viewer.run(), 0)
        self.assertIn("not done: retry budget exhausted (5/5); pass --reset-budget", viewer.out.getvalue())
        data = Run.load(run.dir).data
        self.assertEqual(data["retries"]["used"], 1)

    def test_reset_retry_uses_the_configured_cap(self):
        run = self.queued_run()
        run.data.update({"status": "cancelled", "retries": {"used": 5, "budget": 5}})
        run.save()
        capped = control.config.parse({"max_retries": 2})
        outcome = control.retry(self.home, capped, run.dir, reset_budget=True)
        self.assertEqual(outcome.run.data["retries"], {"used": 0, "budget": 2})

    def test_dead_worker_is_resumed_from_the_view(self):
        run = self.queued_run()
        ticks = {"n": 0}

        def after_worker_down(_run):
            ticks["n"] += 1
            return ticks["n"] > watch.WORKER_DOWN_POLLS + 1

        out = TtyBuffer()
        viewer, _ = self.watcher(run, [(after_worker_down, "r", [])], out=out)
        self.assertEqual(viewer.run(), 0)
        self.assertEqual(Run.load(run.dir).status, "done")
        self.assertIn("run.resumed", self.names(run))
        self.assertIn("keys: r resume the worker  c cancel  q quit", ANSI.sub("", out.getvalue()))

    def test_help_verbose_and_dashboard_keys(self):
        scenario = happy_scenario()
        scenario["build"][0]["sleep"] = 1.0
        self.scenario(scenario)
        run = self.queued_run()
        thread = self.start(run)
        opened = []
        out = TtyBuffer()
        keys = ScriptedKeys(run.dir, [(lambda r: True, "?vd", [])])
        viewer = watch.Watcher(run.dir, out=out, poll=0.05, keys=keys, width=140, opener=opened.append)
        self.assertEqual(viewer.run(), 0)
        thread.join(timeout=20)
        plain = ANSI.sub("", out.getvalue())
        self.assertIn("p  pause before the next attempt starts", plain)
        self.assertIn("streaming agent milestones and failed commands", plain)
        self.assertRegex(plain, r"\| agent: ")
        self.assertEqual(opened, [self.home / "dashboard.html"])


class CommandTests(ControlTestCase):
    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_pause_note_and_resume_commands(self):
        run = self.queued_run()
        code, stdout, _ = self.call("note", run.id, "prefer the existing table")
        self.assertEqual(code, 0)
        self.assertIn("Note saved for the next scope-review attempt", stdout)
        code, stdout, _ = self.call("pause", run.id)
        self.assertEqual(Run.load(run.dir).status, "paused")
        code, stdout, _ = self.call("show", run.id)
        self.assertTrue(stdout.startswith("Paused before scope-review"))
        self.assertIn("next note  prefer the existing table", stdout)
        self.assertIn(f"> factory resume {run.id}", stdout)
        code, stdout, err = self.call("pause", run.id)
        self.assertEqual(code, 1)
        self.assertIn("only queued or running runs can pause", err)
        code, stdout, _ = self.call("resume", run.id)
        self.assertEqual(code, 0, stdout)
        self.assertIn(f"Continued {run.id} at scope-review", stdout)
        data = Run.load(run.dir).data
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["human"]["note"], "prefer the existing table")
        self.assertFalse((run.dir / "note").exists())
        self.assertIn("Operator note: prefer the existing table.",
                      (run.attempt_dir("scope-review", 1) / "prompt.txt").read_text())

    def test_pause_undo_and_cancel_a_paused_run(self):
        run = self.queued_run()
        lock = supervise.WorkerLock(run.dir)
        self.assertTrue(lock.acquire())
        try:
            code, stdout, _ = self.call("pause", run.id)
            self.assertIn("pauses before the next attempt starts", stdout)
            code, stdout, _ = self.call("pause", run.id, "--undo")
            self.assertIn("Pause withdrawn", stdout)
            self.assertFalse((run.dir / "pause").exists())
        finally:
            lock.release()
        self.call("pause", run.id)
        code, stdout, _ = self.call("cancel", run.id)
        self.assertEqual(Run.load(run.dir).status, "cancelled")
        code, stdout, _ = self.call("ls")
        self.assertIn("cancelled", stdout)

    def test_paused_runs_need_attention_in_ls_and_dashboard(self):
        run = self.queued_run()
        self.call("pause", run.id)
        code, stdout, _ = self.call("ls", "--json")
        import json
        row = json.loads(stdout)[0]
        self.assertEqual((row["status"], row["group"], row["next"]), ("paused", "needs", f"factory resume {run.id}"))

    def test_worker_pauses_before_slot_and_consumes_note(self):
        run = self.queued_run()
        control.intent(run.dir, "pause")
        self.assertEqual(Worker(self.home, run.id).run(), 0)
        self.assertEqual(Run.load(run.dir).status, "paused")
        self.assertEqual(Run.load(run.dir).data["attempts"], [])


if __name__ == "__main__":
    unittest.main()
