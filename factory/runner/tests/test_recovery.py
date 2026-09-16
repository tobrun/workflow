"""Crash recovery with real detached workers: one executor, exactly-once transitions, and one side effect.

Crashes come from `FACTORY_FAULT` points (see runner/faults.py), so every boundary is hit
deterministically instead of by timing guesses.
"""

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from runner import cli, events, executor, supervise
from runner.model import Run
from runner.tests.helpers import STUBS, FactoryTestCase, build_step, git, happy_scenario, review_step, ship_step


class RecoveryTestCase(FactoryTestCase):
    def call(self, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def start(self, run: Run, fault: str | None = None) -> None:
        """Start a detached worker for a queued run, optionally with a one-shot fault."""
        env = {}
        if fault:
            env = {"FACTORY_FAULT": fault, "FACTORY_FAULT_ONCE": str(self.root / f"fault-{fault.replace(':', '_')}")}
        with mock.patch.dict(os.environ, env):
            code, out, err = self.call("resume", run.id, "--detach")
        self.assertEqual(code, 0, out + err)

    def wait_dead(self, run: Run, timeout: float = 60) -> dict:
        self.wait_for(lambda: not supervise.worker_alive(run.dir), timeout, "worker exit")
        return json.loads((run.dir / "run.json").read_text())

    def kill_worker(self, run: Run) -> None:
        pid = supervise.read_pid(run.dir)
        os.kill(pid, signal.SIGKILL)
        self.wait_for(lambda: not supervise.worker_alive(run.dir), 10, "worker death")

    def codex_execs(self, stage: str | None = None) -> list[dict]:
        return [c for c in self.stub_calls("codex") if c["argv"][:1] == ["exec"]
                and (stage is None or c["env"].get("FACTORY_STAGE") == stage)]

    def finished_events(self, run: Run, stage: str) -> list[dict]:
        return [e for e in events.read(run.dir) if e["event"] == "stage.finished" and e["stage"] == stage]

    def attempts(self, data: dict, stage: str) -> list[dict]:
        return [a for a in data["attempts"] if a["stage"] == stage]


def command_lines() -> list[str]:
    """Every process's command line: from /proc where it exists (minimal Linux images have no ps), else ps."""
    proc = Path("/proc")
    if proc.is_dir():
        lines = []
        for entry in proc.iterdir():
            if entry.name.isdigit():
                try:
                    lines.append((entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip())
                except OSError:
                    continue
        return lines
    return subprocess.run(["ps", "-axww", "-o", "command="], capture_output=True, text=True).stdout.splitlines()


class HostCounter:
    """Samples `ps` for stub codex processes working in one worktree and keeps the maximum seen at once."""

    def __init__(self, worktree: Path):
        self.needle = f"-C {worktree}"
        self.maximum = 0
        self.samples = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            count = sum(1 for line in command_lines()
                        if str(STUBS / "codex") in line and " exec " in f" {line} " and self.needle in line
                        and "-c" not in line.split(str(STUBS / "codex"))[0].split())
            self.maximum = max(self.maximum, count)
            self.samples += 1
            time.sleep(0.05)

    def __enter__(self) -> "HostCounter":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


class SingleExecutorTests(RecoveryTestCase):
    def test_r8_resume_reattaches_to_the_surviving_host_instead_of_starting_another(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [{**review_step(), "sleep": 3}]
        self.scenario(scenario)
        run = self.queued_run()
        with HostCounter(run.worktree) as hosts:
            self.start(run)
            self.wait_for(lambda: "process.spawned" in [e["event"] for e in events.read(run.dir)], 20, "spawn")
            child = Run.load(run.dir).data["attempts"][0]["pid"]
            self.kill_worker(run)
            self.assertTrue(executor.live(run.dir))
            self.assertTrue(executor.process_identity(child)[0])
            self.assertIn("(worker down, execution alive)", self.call("ls")[1])
            self.assertIn("its execution is still alive", self.call("show", run.id)[1])
            code, out, _ = self.call("resume", run.id, "--detach")
            self.assertEqual(code, 0)
            self.assertIn("reattaches to it instead of starting another", out)
            data = self.wait_status(run.dir, ("done", "needs-human", "cancelled"), timeout=90)
        self.assertEqual(data["status"], "done", data["human"])
        self.assertGreater(hosts.samples, 0, "the process counter never sampled")
        self.assertEqual(hosts.maximum, 1)
        self.assertEqual(len(self.codex_execs("scope-review")), 1)
        reviews = self.attempts(data, "scope-review")
        self.assertEqual([a["outcome"] for a in reviews], ["done"])
        self.assertEqual(data["retries"]["used"], 0)
        self.assertIn("run.reattached", [e["event"] for e in events.read(run.dir)])
        self.assertEqual(len(self.finished_events(run, "scope-review")), 1)

    def test_a_host_registered_but_never_released_never_starts_work(self):
        self.scenario(happy_scenario())
        run = self.queued_run()
        self.start(run, fault="guard.before_go:scope-review-1")
        data = self.wait_status(run.dir, ("done", "needs-human", "cancelled"), timeout=90)
        self.assertEqual(data["status"], "done", data["human"])
        calls = self.codex_execs("scope-review")
        self.assertEqual([c["env"]["FACTORY_ATTEMPT"] for c in calls], ["2"])
        reviews = self.attempts(data, "scope-review")
        self.assertEqual(reviews[0]["outcome"], "failed")
        self.assertIn("orphaned attempt", reviews[0]["reason"])
        self.assertEqual(data["retries"]["used"], 1)

    def test_worker_crash_before_launch_starts_no_host(self):
        self.scenario(happy_scenario())
        run = self.queued_run()
        self.start(run, fault="worker.before_launch:scope-review:1")
        data = self.wait_dead(run)
        self.assertEqual(data["status"], "running")
        self.assertEqual(self.codex_execs(), [])
        self.start(run)
        data = self.wait_status(run.dir, ("done",), timeout=90)
        self.assertEqual([c["env"]["FACTORY_ATTEMPT"] for c in self.codex_execs("scope-review")], ["2"])
        self.assertEqual(data["retries"]["used"], 1)

    def test_cancel_stops_a_surviving_host_when_no_worker_is_alive(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [{"sleep": 60}]
        self.scenario(scenario)
        run = self.queued_run()
        self.start(run)
        self.wait_for(lambda: "process.spawned" in [e["event"] for e in events.read(run.dir)], 20, "spawn")
        child = Run.load(run.dir).data["attempts"][0]["pid"]
        self.kill_worker(run)
        self.assertTrue(executor.live(run.dir))
        code, err, _ = self.call("rm", run.id, "--force")
        self.assertEqual(code, 1)
        code, out, _ = self.call("cancel", run.id)
        self.assertEqual(code, 0, out)
        self.assertIn(f"Cancelled {run.id}", out)
        self.assertFalse(executor.live(run.dir))
        self.assertFalse(executor.process_identity(child)[0])
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "cancelled")
        self.assertEqual(data["attempts"][0]["outcome"], "cancelled")

    def test_rm_refuses_a_live_execution(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [{"sleep": 60}]
        self.scenario(scenario)
        run = self.queued_run()
        self.start(run)
        self.wait_for(lambda: "process.spawned" in [e["event"] for e in events.read(run.dir)], 20, "spawn")
        self.kill_worker(run)
        code, _, err = self.call("rm", run.id, "--force")
        self.assertEqual(code, 1)
        self.assertIn("is still live", err)
        self.assertTrue(run.dir.is_dir())
        self.assertEqual(self.call("cancel", run.id)[0], 0)

    def test_unrelated_process_with_a_reused_pid_is_left_alone(self):
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        self.scenario(happy_scenario())
        run = self.opened_attempt(bystander.pid, "ps:Thu Jan  1 00:00:00 1970")
        code, out, err = self.call("resume", run.id, "--detach")
        self.assertEqual(code, 0, out + err)
        self.assertIn("orphaned attempt recorded as failed", out)
        data = self.wait_status(run.dir, ("done",), timeout=90)
        self.assertEqual(self.attempts(data, "scope-review")[0]["outcome"], "failed")
        self.assertIsNone(bystander.poll())

    def test_unverifiable_ownership_blocks_the_replacement_and_kills_nothing(self):
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        run = self.opened_attempt(bystander.pid, None)
        code, out, err = self.call("resume", run.id, "--detach")
        self.assertEqual(code, 1)
        self.assertIn("cannot be verified", err)
        self.assertFalse(supervise.worker_alive(run.dir))
        self.assertEqual(Run.load(run.dir).status, "running")
        self.assertEqual(self.codex_execs(), [])
        self.assertIsNone(bystander.poll())

    def opened_attempt(self, child_pid: int, child_start: str | None) -> Run:
        """A running run whose dead worker left an open attempt recording child_pid as its host."""
        run = self.queued_run()
        run.data["status"] = "running"
        attempt = run.begin_attempt("scope-review", host="codex", model="m", effort="low")
        attempt_dir = run.attempt_dir("scope-review", 1)
        attempt_dir.mkdir(parents=True)
        token = executor.new_token()
        attempt["execution"] = {"token": token, "dir": os.path.relpath(attempt_dir, run.dir)}
        attempt["pid"] = child_pid
        run.save()
        (attempt_dir / "executor.json").write_text(json.dumps({
            "schema": "factory.executor/1", "token": token, "kind": "stage", "label": "scope-review-1",
            "guard_pid": None, "guard_start": None, "child_pid": child_pid, "child_start": child_start,
            "state": "running"}))
        (run.dir / "executor.current").write_text(json.dumps({"token": token, "dir": attempt["execution"]["dir"]}))
        return run


class ExactlyOnceTests(RecoveryTestCase):
    def test_crash_before_the_transition_is_saved_completes_each_stage_once(self):
        self.scenario(happy_scenario())
        run = self.queued_run()
        self.start(run, fault="worker.before_transition_save:build:1")
        data = self.wait_dead(run)
        self.assertEqual((data["status"], data["stage"]), ("running", "build"))
        self.assertIsNone(self.attempts(data, "build")[0]["ended_at"])
        code, out, _ = self.call("resume", run.id, "--detach")
        self.assertIn("finished while no worker was alive", out)
        data = self.wait_status(run.dir, ("done",), timeout=90)
        for stage in ("scope-review", "build", "ship"):
            self.assertEqual(len(self.codex_execs(stage)), 1, stage)
            self.assertEqual([a["outcome"] for a in self.attempts(data, stage)], ["done"], stage)
            self.assertEqual(len(self.finished_events(run, stage)), 1, stage)
        self.assertEqual(data["retries"]["used"], 0)

    def test_crash_after_the_transition_is_saved_repairs_the_log_without_rerunning(self):
        self.scenario(happy_scenario())
        run = self.queued_run()
        self.start(run, fault="worker.after_transition_save:scope-review:1")
        data = self.wait_dead(run)
        self.assertEqual((data["status"], data["stage"]), ("queued", "build"))
        self.assertEqual(self.finished_events(run, "scope-review"), [])
        self.start(run)
        data = self.wait_status(run.dir, ("done",), timeout=90)
        self.assertEqual(len(self.codex_execs("scope-review")), 1)
        finished = self.finished_events(run, "scope-review")
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["data"]["outcome"], "done")
        self.assertEqual(data["retries"]["used"], 0)

    def test_retry_charge_is_neither_lost_nor_duplicated(self):
        scenario = happy_scenario()
        broken = build_step()
        del broken["files"][".dev/{plan}/scenario-map.json"]
        scenario["build"] = [broken, {"files": build_step()["files"]}]
        self.scenario(scenario)
        run = self.queued_run()
        self.start(run, fault="worker.before_transition_save:build:1")
        data = self.wait_dead(run)
        self.assertEqual(data["retries"]["used"], 0)
        self.start(run)
        data = self.wait_status(run.dir, ("done",), timeout=90)
        self.assertEqual([a["outcome"] for a in self.attempts(data, "build")], ["blocked", "done"])
        self.assertEqual(data["retries"]["used"], 1)
        self.assertEqual(len([e for e in events.read(run.dir) if e["event"] == "retry.scheduled"]), 1)

    def test_crash_after_the_scope_review_commit_keeps_one_commit(self):
        scenario = happy_scenario()
        scenario["scope-review"][0]["files"]["docs/decisions.md"] = "# Decisions\n\nD-dedup-store: database table\n"
        self.scenario(scenario)
        run = self.queued_run()
        self.start(run, fault="worker.after_stage_commit:scope-review")
        self.wait_dead(run)
        self.start(run)
        self.wait_status(run.dir, ("done",), timeout=90)
        subjects = git(run.worktree, "log", "--format=%s").splitlines()
        self.assertEqual(sum(1 for s in subjects if s.startswith("docs(scope-review)")), 1)
        self.assertEqual(len(self.codex_execs("scope-review")), 1)

    def test_worker_crash_during_ship_keeps_one_pull_request(self):
        scenario = happy_scenario()
        scenario["ship"] = [{**ship_step(), "sleep": 1}]
        self.scenario(scenario)
        run = self.queued_run()
        self.start(run, fault="worker.after_spawn:ship:1")
        self.wait_dead(run)
        self.start(run)
        data = self.wait_status(run.dir, ("done",), timeout=90)
        creates = [m for m in self.gh()["mutations"] if m["op"] == "create"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(len(self.codex_execs("ship")), 1)
        self.assertEqual(data["pr"]["number"], 1)

    def test_lost_ship_guard_retries_without_a_duplicate_pull_request(self):
        self.scenario(happy_scenario())
        run = self.queued_run()
        self.start(run, fault="guard.after_go:ship-1")
        data = self.wait_status(run.dir, ("done", "needs-human", "cancelled"), timeout=90)
        self.assertEqual(data["status"], "done", data["human"])
        ships = self.attempts(data, "ship")
        self.assertEqual([a["outcome"] for a in ships], ["failed", "done"])
        self.assertEqual(len([m for m in self.gh()["mutations"] if m["op"] == "create"]), 1)
        self.assertEqual(data["retries"]["used"], 1)


class ValidationSupervisionTests(RecoveryTestCase):
    def repo_with_contract(self, *validation: dict) -> Path:
        from runner.tests.helpers import make_repo
        repo = make_repo(self.root, "validated")
        from runner.tests.helpers import CONTRACT
        (repo / ".factory" / "contract.json").write_text(json.dumps({**CONTRACT, "validation": list(validation)}))
        git(repo, "commit", "--quiet", "-am", "contract")
        git(repo, "push", "--quiet")
        return repo

    def test_gate_time_is_recorded_apart_from_the_host_and_inside_the_attempt_total(self):
        repo = self.repo_with_contract({"id": "readme", "run": ["sh", "-c", "sleep 1.2; test -f README.md"]})
        self.scenario(happy_scenario())
        run = self.queued_run(repo=repo)
        self.start(run)
        data = self.wait_status(run.dir, ("done",), timeout=90)
        build = self.attempts(data, "build")[0]
        self.assertGreaterEqual(build["gate_seconds"], 1.2)
        self.assertIn("host_seconds", build)
        self.assertGreaterEqual(build["seconds"] + 1, build["gate_seconds"] + build["host_seconds"])
        executions = json.loads((run.attempt_dir("build", 1) / "gate-executions.json").read_text())
        self.assertIn("validation-readme", [e["label"] for e in executions])
        self.assertTrue(all(Path(e["dir"], "receipt.json").is_file() for e in executions))

    def test_cli_cancel_during_validation_stops_it_and_keeps_its_output(self):
        pid_file = self.root / "validation-child.pid"
        repo = self.repo_with_contract({"id": "slow", "run": ["sh", "-c", f"echo validating; sleep 60 & echo $! > {pid_file}; wait"]})
        self.scenario(happy_scenario())
        run = self.queued_run(repo=repo)
        self.start(run)
        self.wait_for(pid_file.exists, 60, "validation started")
        code, out, _ = self.call("cancel", run.id)
        self.assertEqual(code, 0)
        data = self.wait_status(run.dir, ("cancelled",), timeout=60)
        build = self.attempts(data, "build")[0]
        self.assertEqual(build["outcome"], "cancelled")
        self.assertIn("validation command slow", build["reason"])
        child = int(pid_file.read_text())
        self.wait_for(lambda: not executor.process_identity(child)[0], 10, "validation descendant stopped")
        logs = list(run.attempt_dir("build", 1).glob("gate/*-validation-slow/stdout.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("validating", logs[0].read_text())
        receipt = json.loads((logs[0].parent / "receipt.json").read_text())
        self.assertEqual(receipt["classification"], "cancelled")


if __name__ == "__main__":
    unittest.main()
