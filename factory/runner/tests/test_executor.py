"""Guarded executions: supervision, the start barrier, recovery, and ownership verification."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from runner import executor, slots, supervise
from runner.tests.helpers import STUBS


def wait_until(predicate, timeout: float = 15, message: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {message}")


def gone(pid: int) -> bool:
    exists, _ = executor.process_identity(pid)
    return not exists


class ExecutorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(os.path.realpath(self._tmp.name))
        self.run_dir = self.dir / "run"
        self.run_dir.mkdir()
        self.spawned: list[int] = []
        self.procs: list[subprocess.Popen] = []

    def tearDown(self):
        for pid in self.spawned + [proc.pid for proc in self.procs]:
            for kill in (os.killpg, os.kill):
                try:
                    kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        for proc in self.procs:
            proc.wait()
        self._tmp.cleanup()

    def request(self, script: str | None = None, *, argv: list[str] | None = None, name: str = "exec",
                **overrides) -> tuple[Path, executor.Request]:
        exec_dir = self.dir / name
        exec_dir.mkdir(exist_ok=True)
        values = dict(token=executor.new_token(), kind="test", label=name,
                      argv=argv or [sys.executable, "-c", script], cwd=str(self.dir), run_dir=str(self.run_dir),
                      stdout=str(exec_dir / "out"), stderr=str(exec_dir / "err"), grace=1, poll=0.05)
        values.update(overrides)
        return exec_dir, executor.Request(**values)

    def run_exec(self, script: str | None = None, **kwargs) -> executor.Receipt:
        exec_dir, request = self.request(script, **kwargs)
        return executor.run(exec_dir, request, dict(os.environ))


class SupervisionTests(ExecutorTestCase):
    def test_exit_code_devnull_stdin_and_receipt(self):
        receipt = self.run_exec("import sys; data = sys.stdin.read(); print(repr(data)); sys.exit(3)")
        self.assertEqual((receipt.classification, receipt.exit_code), ("failed", 3))
        self.assertTrue(receipt.spawned)
        self.assertEqual((self.dir / "exec" / "out").read_text().strip(), "''")
        self.assertEqual(receipt.stdout["bytes"], 3)
        saved = json.loads((self.dir / "exec" / "receipt.json").read_text())
        self.assertEqual(saved["schema"], "factory.receipt/1")
        self.assertEqual(saved["token"], receipt.token)
        self.assertFalse(executor.lease_held(self.run_dir))

    def test_success(self):
        self.assertEqual(self.run_exec("print('ok')").classification, "succeeded")

    def test_deadline_terminates_the_process_group(self):
        script = ("import subprocess, sys, time;"
                  "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
                  "open('child.pid', 'w').write(str(child.pid)); time.sleep(60)")
        started = time.monotonic()
        receipt = self.run_exec(script, deadline_at=time.time() + 1)
        self.assertEqual(receipt.classification, "timeout")
        self.assertLess(time.monotonic() - started, 10)
        child = int((self.dir / "child.pid").read_text())
        wait_until(lambda: gone(child), 5, "grandchild exit")

    def test_sigterm_ignored_escalates_to_sigkill(self):
        script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
        receipt = self.run_exec(script, deadline_at=time.time() + 0.5, grace=0.5)
        self.assertEqual((receipt.classification, receipt.exit_code), ("timeout", -9))

    def test_cancel_wins_over_timeout(self):
        flag = self.dir / "cancel-later"
        exec_dir, request = self.request("import time; time.sleep(60)", cancel_file=str(flag),
                                         deadline_at=time.time() + 30)
        guard = executor.launch(exec_dir, request, dict(os.environ))
        wait_until(lambda: (executor.read_state(exec_dir) or {}).get("state") == "running", message="running")
        flag.write_text("x")
        guard.wait(timeout=20)
        self.assertEqual(executor.read_receipt(exec_dir).classification, "cancelled")

    def test_nothing_starts_when_cancelled_or_expired(self):
        marker = self.dir / "ran"
        flag = self.dir / "cancel"
        flag.write_text("x")
        receipt = self.run_exec(f"open({str(marker)!r}, 'w')", cancel_file=str(flag))
        self.assertEqual((receipt.classification, receipt.spawned), ("cancelled", False))
        receipt = self.run_exec(f"open({str(marker)!r}, 'w')", deadline_at=time.time() - 1, name="late")
        self.assertEqual((receipt.classification, receipt.spawned), ("timeout", False))
        self.assertFalse(marker.exists())

    def test_spawn_error(self):
        receipt = self.run_exec(argv=[str(self.dir / "missing-binary")])
        self.assertEqual(receipt.classification, "spawn_failed")
        self.assertIn("missing-binary", receipt.spawn_error)
        receipt = self.run_exec("print(1)", cwd=str(self.dir / "no-such-dir"), name="nocwd")
        self.assertEqual(receipt.classification, "spawn_failed")

    def test_a_second_execution_is_refused_while_the_lease_is_held(self):
        exec_dir, request = self.request("import time; time.sleep(30)")
        guard = executor.launch(exec_dir, request, dict(os.environ))
        self.procs.append(guard)
        wait_until(lambda: executor.lease_held(self.run_dir), message="lease")
        receipt = self.run_exec("print('second')", name="second")
        self.assertEqual(receipt.classification, "refused")
        self.assertFalse((self.dir / "second" / "out").exists())
        guard.send_signal(signal.SIGTERM)
        guard.wait(timeout=20)
        self.assertEqual(executor.read_receipt(exec_dir).classification, "cancelled")

    def test_stub_codex_is_executable(self):
        self.assertTrue(os.access(STUBS / "codex", os.X_OK))


class RecoveryTests(ExecutorTestCase):
    def guard_env(self, fault: str) -> dict:
        return {**os.environ, "FACTORY_FAULT": fault}

    def test_guard_lost_before_go_never_runs_the_command(self):
        marker = self.dir / "ran"
        exec_dir, request = self.request(f"open({str(marker)!r}, 'w')")
        guard = executor.launch(exec_dir, request, self.guard_env("guard.before_go"))
        guard.wait(timeout=20)
        state = executor.read_state(exec_dir)
        self.assertEqual(state["state"], "registered")
        wait_until(lambda: gone(state["child_pid"]), 5, "barrier child exit")
        receipt = executor.recover(self.run_dir, exec_dir, request.token, grace=1, poll=0.05)
        self.assertEqual(receipt.classification, "lost")
        self.assertFalse(marker.exists())

    def test_worker_death_keeps_guard_supervising_and_slot_held(self):
        slot = slots.try_acquire(self.dir / "slots", "run", 1)
        exec_dir, request = self.request("import time; time.sleep(1.5); print('done')",
                                         inherit_fds=[slot.fd])
        guard = executor.launch(exec_dir, request, dict(os.environ))
        os.close(slot.fd)  # the worker dies: its copy of the slot fd closes without an unlock
        wait_until(lambda: (executor.read_state(exec_dir) or {}).get("state") == "running", message="running")
        self.assertIn(0, slots.holders(self.dir / "slots"))
        self.assertIsNone(slots.try_acquire(self.dir / "slots", "other", 1))
        self.assertTrue(executor.live(self.run_dir))
        receipt = executor.recover(self.run_dir, exec_dir, request.token, grace=1, poll=0.05)
        guard.wait(timeout=10)
        self.assertEqual(receipt.classification, "succeeded")
        self.assertEqual(slots.holders(self.dir / "slots"), {})

    def test_guard_lost_after_go_waits_for_the_child_and_enforces_cancel(self):
        flag = self.dir / "cancel"
        exec_dir, request = self.request("import time; time.sleep(60)", cancel_file=str(flag))
        guard = executor.launch(exec_dir, request, self.guard_env("guard.after_go"))
        guard.wait(timeout=20)
        state = executor.read_state(exec_dir)
        child = state["child_pid"]
        self.spawned.append(child)
        self.assertFalse(executor.lease_held(self.run_dir))
        self.assertTrue(executor.live(self.run_dir))
        flag.write_text("x")
        receipt = executor.recover(self.run_dir, exec_dir, request.token, cancel_requested=flag.exists, grace=1,
                                   poll=0.05)
        self.assertEqual(receipt.classification, "lost")
        wait_until(lambda: gone(child), 5, "child stopped")
        self.assertFalse(executor.live(self.run_dir))

    def test_pid_reuse_leaves_the_unrelated_process_untouched(self):
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.procs.append(bystander)
        exec_dir, request = self.request("print(1)")
        exec_dir.mkdir(exist_ok=True)
        (exec_dir / "executor.json").write_text(json.dumps({
            "schema": "factory.executor/1", "token": request.token, "kind": "stage", "guard_pid": None,
            "child_pid": bystander.pid, "child_start": "ps:Thu Jan  1 00:00:00 1970", "state": "running"}))
        (self.run_dir / "executor.current").write_text(json.dumps({"token": request.token, "dir": "../exec"}))
        self.assertFalse(executor.live(self.run_dir))
        receipt = executor.recover(self.run_dir, exec_dir, request.token, cancel_requested=lambda: True, grace=0.2)
        self.assertEqual(receipt.classification, "lost")
        self.assertEqual(executor.stop_live(self.run_dir, grace=0.2), "idle")
        self.assertIsNone(bystander.poll())

    def test_unverifiable_identity_is_ambiguous_and_kills_nothing(self):
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.procs.append(bystander)
        exec_dir, request = self.request("print(1)")
        exec_dir.mkdir(exist_ok=True)
        (exec_dir / "executor.json").write_text(json.dumps({
            "schema": "factory.executor/1", "token": request.token, "child_pid": bystander.pid, "child_start": None}))
        (self.run_dir / "executor.current").write_text(json.dumps({"token": request.token, "dir": "../exec"}))
        with self.assertRaises(executor.Ambiguous):
            executor.recover(self.run_dir, exec_dir, request.token, cancel_requested=lambda: True, grace=0.2)
        with mock.patch.object(executor, "process_identity", return_value=(True, None)):
            with self.assertRaises(executor.Ambiguous):
                executor.stop_live(self.run_dir, grace=0.2)
        self.assertIsNone(bystander.poll())

    def test_stop_live_stops_a_guarded_execution(self):
        exec_dir, request = self.request("import time; time.sleep(60)")
        guard = executor.launch(exec_dir, request, dict(os.environ))
        wait_until(lambda: (executor.read_state(exec_dir) or {}).get("state") == "running", message="running")
        child = executor.read_state(exec_dir)["child_pid"]
        self.assertEqual(executor.stop_live(self.run_dir, grace=1), "stopped")
        guard.wait(timeout=10)
        self.assertTrue(gone(child))
        self.assertEqual(executor.read_receipt(exec_dir).classification, "cancelled")

    def test_identity_of_self_is_stable(self):
        exists, first = executor.process_identity(os.getpid())
        self.assertTrue(exists)
        self.assertIsNotNone(first)
        self.assertEqual(executor.process_identity(os.getpid())[1], first)
        self.assertEqual(executor.process_identity(2 ** 22 + 12345), (False, None))
        self.assertFalse(supervise.pid_alive(2 ** 22 + 12345))


if __name__ == "__main__":
    unittest.main()
