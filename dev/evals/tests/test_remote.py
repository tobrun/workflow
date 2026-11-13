"""Tests for the remote skill's transport: remote.py on this side, runner.sh in the sandbox.

Run from the repo root: python3 -m unittest discover -s dev/evals/tests -t .
Each test builds a laptop checkout, a bare origin, and two fake instances,
then drives remote.py as a subprocess with the `local` transport. The fakes
in remote_fakes/ stand in for incus, fleet, systemctl, gh, and claude with
the real commands' semantics, so a test proves the whole start -> run ->
pull cycle on real git, without a fleet host or a model.
"""

from __future__ import annotations

import importlib.util
import json
import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "dev" / "skills" / "remote" / "scripts" / "remote.py"
VIEW = SCRIPT.with_name("view.py")
FAKES = Path(__file__).resolve().parent / "remote_fakes"

SPEC = """# Greeting

## Research

D-greeting: How does the tool greet?
  ✓ plain text - the only consumer is a terminal
  ✗ JSON - nobody parses it

## Scope

The tool greets in plain text per D-greeting (✓ plain text).

### Validation

- `cat feature.txt`

## Change plan

1. Greet the user
   a. `feature.txt` - add the greeting - decisions: D-greeting (✓ plain text)
   tests: [unit] the greeting is printed
"""

IDENTITY = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com"}


def load_module(path: Path = SCRIPT):
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))  # remote.py imports its renderer from view.py beside it
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return os.waitpid(pid, os.WNOHANG)[0] == 0
    except ChildProcessError:
        return True


class RemoteCycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name).resolve()
        self.t = t
        self.repo = t / "laptop" / "fixture"
        self.vms = t / "vms"
        for path in (self.repo, self.vms / "ws-ws1" / "home", self.vms / "ws-ws2" / "home", t / "home"):
            path.mkdir(parents=True)
        self.config = t / "remote.toml"
        self.config.write_text('transport = "local"\nbackoff_seconds = 0\nattempts = 3\n')
        self.env = {**os.environ, **IDENTITY,
                    "HOME": str(t / "home"), "GIT_CONFIG_NOSYSTEM": "1",
                    "PATH": os.pathsep.join([str(FAKES), str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
                    "DEV_REMOTE_CONFIG": str(self.config), "FAKE_FLEET_ROOT": str(self.vms)}
        self.env.pop("FAKE_CLAUDE_MODE", None)
        self.origin = t / "origin.git"
        self.git(t, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git(self.repo, "init", "-q", "-b", "main")
        (self.repo / ".gitignore").write_text("/.dev/*\n!/.dev/config.yaml\n")
        (self.repo / "README.md").write_text("# fixture\n")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-q", "-m", "initial")
        self.git(self.repo, "remote", "add", "origin", str(self.origin))
        self.git(self.repo, "push", "-q", "-u", "origin", "main")
        # As on the fleet: origin is a github.com URL that the instance's git config rewrites to a relay.
        relay = t / "relay.gitconfig"
        relay.write_text(f'[url "{self.origin}"]\n\tinsteadOf = https://github.com/test/fixture.git\n')
        self.env.update(FAKE_GH="signed-in", FAKE_GITCONFIG=str(relay))
        self.git(self.repo, "remote", "set-url", "origin", "https://github.com/test/fixture.git")
        self.base = self.git(self.repo, "rev-parse", "HEAD")
        plan = self.repo / ".dev" / "greeting"
        plan.mkdir(parents=True)
        (plan / "spec.md").write_text(SPEC)
        self.addCleanup(self.stop_all)

    def stop_all(self) -> None:
        """No background run outlives its test, whatever the test asserted."""
        rows = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "ppid="], capture_output=True, text=True).stdout
        children: dict[int, list[int]] = {}
        for row in rows.splitlines():
            parts = row.split()
            if len(parts) == 2:
                children.setdefault(int(parts[1]), []).append(int(parts[0]))
        for unit in self.vms.glob("ws-*/units/*.pid"):
            try:
                todo = [int(unit.read_text())]
            except ValueError:
                continue
            while todo:  # the whole tree: GNU timeout moves the command it runs out of the group
                pid = todo.pop()
                todo.extend(children.get(pid, []))
                try:
                    os.kill(pid, 9)
                except (ProcessLookupError, PermissionError):
                    pass

    # --- helpers -------------------------------------------------------------

    def git(self, cwd: Path, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=cwd, env={**os.environ, **IDENTITY, "GIT_CONFIG_NOSYSTEM": "1"},
                                capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def remote(self, *args: str, mode: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
        env = {**self.env, **({"FAKE_CLAUDE_MODE": mode} if mode else {})}
        result = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.repo, env=env,
                                capture_output=True, text=True, check=False, timeout=120)
        if check and result.returncode != 0:
            self.fail(f"remote.py {' '.join(args)} exited {result.returncode}:\n{result.stderr}{result.stdout}")
        return result

    def start(self, n: str = "1", *extra: str, mode: str | None = None) -> dict:
        what = extra if extra else ("--plan", "greeting")
        return json.loads(self.remote("start", n, *what, mode=mode).stdout)

    def wait(self, n: str = "1") -> int:
        return self.remote("wait", n, "--interval", "0.1", check=False).returncode

    def status(self, n: str = "1") -> dict:
        return json.loads(self.remote("status", n, "--json").stdout)[0]

    def pull(self, n: str = "1") -> dict:
        return json.loads(self.remote("pull", n).stdout)

    def run_dir(self, ws: str = "ws1") -> Path:
        return self.vms / f"ws-{ws}" / "home" / ".dev-workflow" / "run"

    def origin_branches(self) -> list[str]:
        return self.git(self.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()

    def terminals(self, ws: str = "ws1") -> list[dict]:
        path = self.vms / f"ws-{ws}" / "terminal-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def viewer_output(self, n: int, until: str, ws: str = "ws1") -> str:
        """The n-th desktop viewer's screen, once it shows `until` (it polls once a second)."""
        path = self.vms / f"ws-{ws}" / f"view-{n}.out"
        text = ""
        for _ in range(100):
            text = re.sub(r"\x1b\[[0-9;]*m|\x1b\][^\x07]*\x07", "", path.read_text()) if path.exists() else ""
            if until in text:
                return text
            time.sleep(0.1)
        self.fail(f"viewer {n} never showed {until!r}:\n{text}")

    def viewer_pid(self, n: int, ws: str = "ws1") -> int:
        return int((self.vms / f"ws-{ws}" / "units" / f"view-{n}.pid").read_text())

    def calls(self, ws: str = "ws1") -> list[dict]:
        path = self.vms / f"ws-{ws}" / "claude-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    # --- a run finishes by itself ----------------------------------------------

    def test_a_plan_runs_to_its_pull_request_and_comes_home(self) -> None:
        out = self.start()
        self.assertEqual((out["instance"], out["workspace"], out["branch"], out["base"]),
                         ("1", "ws1", "dev/greeting", self.base))
        self.assertTrue(out["run"].startswith("greeting-"))
        self.assertEqual(self.wait(), 0)
        live = self.status()
        self.assertEqual((live["state"], live["attempt"], live["pr"]["url"], live["pr"]["draft"]),
                         ("done", "1", "https://example.test/pr/7", "false"))
        [call] = self.calls()
        self.assertTrue(call["prompt"].startswith("/dev:yolo Build and ship the plan in `.dev/greeting/`."))
        self.assertEqual(call["branch"], "dev/greeting")
        argv = call["argv"]
        for flag in ("--permission-mode", "bypassPermissions", "--disallowed-tools", "AskUserQuestion",
                     "--output-format", "stream-json"):
            self.assertIn(flag, argv)
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        self.assertEqual(settings["attribution"], {"commit": "", "pr": ""}, "no Co-Authored-By in its commits")
        self.assertEqual(call["bg_wait_ms"], "6000000",
                         "a session waits ten times Claude Code's 600 s default for its background agents")
        self.assertEqual(self.origin_branches(), ["dev/greeting", "main"], "the instance published the branch")

        summary = self.pull()
        self.assertEqual((summary["state"], summary["branch"], summary["branch_update"]),
                         ("done", "dev/greeting", "created"))
        self.assertEqual(self.git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main", "checkout untouched")
        log = self.git(self.repo, "log", "--format=%s", "main..dev/greeting")
        self.assertEqual(log.splitlines(), ["fix: harden after review", "feat: build the feature"])
        self.assertEqual(len(summary["commits"]), 2)
        plan = self.repo / ".dev" / "greeting"
        self.assertTrue((plan / "implementation-notes.md").exists())
        self.assertEqual(sorted(summary["plan"]["updated"]),
                         ["greeting/implementation-notes.md", "greeting/pr.md", "greeting/review_1.md"])
        self.assertEqual((summary["verdict"], summary["open_calls"], summary["blocked"]), ("PASS", [], None))
        self.assertEqual(self.pull()["plan"], {"updated": [], "conflicts": []}, "a second pull changes nothing")

    def test_a_task_runs_as_yolo_from_scratch(self) -> None:
        out = self.start("2", "--task", "Add a greeting")
        self.assertEqual((out["instance"], out["workspace"], out["branch"]), ("2", "ws2", "dev/add-a-greeting"))
        self.assertEqual(self.wait("2"), 0)
        self.assertEqual([c["prompt"] for c in self.calls("ws2")], ["/dev:yolo Add a greeting"])
        summary = self.pull("2")
        self.assertEqual(summary["plans"], ["from-task"])
        self.assertIn("Add a greeting", (self.repo / ".dev" / "from-task" / "spec.md").read_text())
        self.assertEqual(self.calls("ws1"), [], "the instance is the one the user named")

    def test_a_plan_takes_an_extra_instruction(self) -> None:
        self.start("1", "--plan", "greeting", "--task", "Only change set 1.")
        self.wait()
        self.assertTrue(self.calls()[0]["prompt"].endswith("The operator adds:\n\nOnly change set 1."))

    def finishes_in_a_second_session(self, mode: str, why: str) -> None:
        self.start(mode=mode)
        self.assertEqual(self.wait(), 0)
        first, second = self.calls()
        self.assertEqual(second["resume"], first["session"], "the same session goes back to work")
        self.assertIn("This run is not finished: " + why, second["prompt"])
        live = self.status()
        self.assertEqual((live["state"], live["attempt"], live["pr"]["number"]), ("done", "2", "7"))

    def test_a_session_that_stops_before_the_pull_request_is_sent_back(self) -> None:
        self.finishes_in_a_second_session("early-once", "there is no pull request for dev/greeting")

    def test_a_session_that_leaves_uncommitted_work_is_sent_back(self) -> None:
        self.finishes_in_a_second_session("dirty-once", "the working tree has uncommitted changes:\n?? ship.txt")

    def test_a_gateway_error_is_retried_in_the_same_session(self) -> None:
        self.finishes_in_a_second_session("api-error-once", "the last session ended early (api-error); ")

    def test_a_crashed_session_is_retried(self) -> None:
        self.finishes_in_a_second_session("crash-once", "the last session ended early (no-result); ")

    def test_attempts_run_out_as_failed_and_continue_picks_the_run_up(self) -> None:
        self.start(mode="never")
        self.assertEqual(self.wait(), 1)
        live = self.status()
        self.assertEqual(live["state"], "failed")
        self.assertIn("still unfinished after 3 sessions: there is no pull request", live["reason"])
        self.assertEqual(len(self.calls()), 3)
        out = json.loads(self.remote("continue", "1").stdout)
        self.assertEqual((out["was"], out["session"]), ("failed", self.calls()[0]["session"]))
        self.assertEqual(self.wait(), 0)
        self.assertEqual(self.calls()[-1]["resume"], self.calls()[0]["session"])
        self.assertEqual(self.status()["state"], "done")

    # --- blocked, and what continue does ---------------------------------------

    def test_a_hard_stop_ends_blocked_and_continue_goes_on_with_an_instruction(self) -> None:
        self.start(mode="blocked")
        self.assertEqual(self.wait(), 1)
        live = self.status()
        self.assertEqual(live["state"], "blocked")
        self.assertIn("committed secret", live["reason"])
        self.assertEqual(len(self.calls()), 1, "a blocked run is not sent back to work")
        self.assertIn("committed secret", self.pull()["blocked"])
        self.remote("continue", "1", "--prompt", "The secret is rotated; go on.")
        self.assertEqual(self.wait(), 0)
        last = self.calls()[-1]
        self.assertEqual(last["resume"], self.calls()[0]["session"])
        self.assertIn("The operator adds:\n\nThe secret is rotated; go on.", last["prompt"])
        self.assertFalse((self.run_dir() / "blocked.md").exists())
        self.assertEqual(len(list(self.run_dir().glob("blocked-*.md"))), 1, "the earlier stop stays on record")
        summary = self.pull()
        self.assertEqual((summary["state"], summary["blocked"]), ("done", None))
        self.assertIn("feat: the operator's addition", " ".join(summary["commits"]))

    def test_a_rejected_push_blocks_and_the_commits_still_come_home(self) -> None:
        self.start(mode="no-push")
        self.assertEqual(self.wait(), 1)
        self.assertEqual(self.origin_branches(), ["main"])
        summary = self.pull()
        self.assertEqual((summary["state"], summary["branch_update"], len(summary["commits"])),
                         ("blocked", "created", 2))
        self.assertIn("no write access", summary["blocked"])
        self.assertTrue((self.repo / ".dev" / "greeting" / "pr.md").exists())

    def test_continue_gives_a_finished_run_more_work(self) -> None:
        self.start()
        self.assertEqual(self.wait(), 0)
        self.assertEqual(self.pull()["branch_update"], "created")
        self.remote("continue", "1", "--prompt", "Also greet in French.")
        self.assertEqual(self.wait(), 0)
        self.assertEqual(self.calls()[-1]["resume"], self.calls()[0]["session"])
        summary = self.pull()
        self.assertEqual((summary["branch_update"], len(summary["commits"])), ("fast-forwarded", 3))
        self.assertEqual(self.git(self.origin, "rev-parse", "dev/greeting"),
                         self.git(self.repo, "rev-parse", "dev/greeting"), "the pull request has the new commit")

    def test_continue_starts_a_new_session_when_the_transcript_is_gone(self) -> None:
        self.start(mode="never")
        self.assertEqual(self.wait(), 1)
        shutil.rmtree(self.vms / "ws-ws1" / "home" / ".claude")
        self.remote("continue", "1")
        self.assertEqual(self.wait(), 0)
        last = self.calls()[-1]
        self.assertIsNone(last["resume"])
        self.assertTrue(last["prompt"].startswith("/dev:yolo Build and ship the plan"))
        self.assertIn("This run is not finished: there is no pull request", last["prompt"])

    def test_continue_refuses_an_idle_and_a_running_instance(self) -> None:
        result = self.remote("continue", "2", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("instance 2 has no run", result.stderr)
        self.start(mode="sleep")
        self.running()
        result = self.remote("continue", "1", check=False)
        self.assertIn("still running", result.stderr)

    # --- the instance is the user's choice -------------------------------------

    def running(self, n: str = "1") -> dict:
        for _ in range(100):
            live = self.status(n)
            if live["state"] == "running" and self.calls(f"ws{n}"):
                return live
            time.sleep(0.1)
        self.fail(f"instance {n} never ran: {live}")

    def test_a_busy_instance_refuses_and_stop_frees_it(self) -> None:
        first = self.start(mode="sleep")["run"]
        self.running()
        result = self.remote("start", "1", "--plan", "greeting", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"instance 1 is running {first} (fixture, dev/greeting)", result.stderr)
        self.assertIn("`stop 1` first", result.stderr)
        stopped = json.loads(self.remote("stop", "1").stdout)
        self.assertEqual((stopped["state"], stopped["reason"]), ("stopped", "stopped"))
        second = self.start()["run"]
        self.assertEqual(self.wait(), 0)
        history = self.vms / "ws-ws1" / "home" / ".dev-workflow" / "history"
        self.assertEqual([p.name for p in history.iterdir()], [first], "the run before stays on the instance")
        self.assertEqual(self.status()["id"], second)

    def test_lost_when_the_runner_dies_without_recording_an_end(self) -> None:
        self.start(mode="sleep")
        self.running()
        pid = int((self.vms / "ws-ws1" / "units" / "dev-remote.pid").read_text())
        os.kill(pid, 9)
        for _ in range(50):
            if self.status()["state"] == "lost":
                break
            time.sleep(0.1)
        self.assertEqual(self.status()["state"], "lost")
        self.assertEqual(self.wait(), 1)

    def test_instances_are_numbered_by_the_configuration_or_named(self) -> None:
        self.config.write_text(self.config.read_text() + 'instances = ["ws2", "ws1"]\n')
        out = self.start("1")
        self.assertEqual((out["instance"], out["workspace"]), ("1", "ws2"))
        self.assertEqual(self.wait("ws2"), 0)
        rows = json.loads(self.remote("status", "--json").stdout)
        self.assertEqual([(r["instance"], r["workspace"], r["state"]) for r in rows],
                         [("1", "ws2", "done"), ("2", "ws1", "idle")])
        table = self.remote("status").stdout
        self.assertIn("https://example.test/pr/7", table)
        result = self.remote("start", "3", "--plan", "greeting", check=False)
        self.assertIn("no instance 3 (instances: 1 to 2)", result.stderr)

    def test_start_says_what_it_will_not_run(self) -> None:
        for args, why in ((("--plan", "missing"), "no scoped plan at .dev/missing/spec.md"),
                          ((), "nothing to run")):
            result = self.remote("start", "1", *args, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn(why, result.stderr)
        result = self.remote("start", "9", "--plan", "greeting", check=False)
        self.assertIn("no instance ws-ws9 on the host (have: ws1, ws2)", result.stderr)
        self.assertEqual(self.calls(), [])

    # --- what crosses ----------------------------------------------------------

    def test_only_committed_work_is_sent_and_the_rest_is_named(self) -> None:
        (self.repo / "README.md").write_text("# edited\n")
        (self.repo / "notes.txt").write_text("scratch\n")
        out = self.start()
        self.assertEqual(out["not_sent"], [" M README.md", "?? notes.txt"])
        self.assertEqual(self.wait(), 0)
        work = self.vms / "ws-ws1" / "home" / "work" / "fixture"
        self.assertEqual((work / "README.md").read_text(), "# fixture\n")
        self.assertFalse((work / "notes.txt").exists())

    def test_the_current_work_branch_is_used_and_fast_forwarded_in_place(self) -> None:
        self.git(self.repo, "switch", "-q", "-c", "feature")
        (self.repo / "local.txt").write_text("committed here\n")
        self.git(self.repo, "add", "local.txt")
        self.git(self.repo, "commit", "-q", "-m", "feat: local work")
        out = self.start()
        self.assertEqual((out["branch"], out["base"]), ("feature", self.base))
        self.assertEqual(self.wait(), 0)
        self.assertEqual(self.calls()[0]["branch"], "feature")
        summary = self.pull()
        self.assertEqual((summary["branch_update"], len(summary["commits"])), ("fast-forwarded", 3))
        self.assertTrue((self.repo / "ship.txt").exists(), "the checked-out branch moved with its files")
        (self.repo / "local.txt").write_text("edited after\n")
        self.remote("continue", "1", "--prompt", "One more.")
        self.assertEqual(self.wait(), 0)
        self.assertIn("not updated", self.pull()["branch_update"])
        self.assertEqual(self.git(self.repo, "log", "--format=%s", "-1", "refs/dev-remote/ws1"),
                         "feat: the operator's addition")

    def test_work_an_earlier_run_left_on_the_instance_is_the_starting_point(self) -> None:
        self.start(mode="no-push")
        self.assertEqual(self.wait(), 1)
        self.start()
        self.assertEqual(self.wait(), 0)
        summary = self.pull()
        self.assertEqual(len(summary["commits"]), 2, "the second run went on from the first one's commits")
        work = self.vms / "ws-ws1" / "home" / "work" / "fixture"
        self.assertTrue((work / ".dev" / "greeting" / "implementation-notes.md").exists())

    def test_a_branch_the_instance_diverged_on_is_kept_aside(self) -> None:
        self.start(mode="no-push")
        self.assertEqual(self.wait(), 1)
        (self.repo / "README.md").write_text("# moved on\n")
        self.git(self.repo, "commit", "-q", "-am", "docs: move on")
        self.start()
        self.assertEqual(self.wait(), 0)
        work = self.vms / "ws-ws1" / "home" / "work" / "fixture"
        kept = self.git(work, "for-each-ref", "--format=%(refname)", "refs/dev-remote/kept").splitlines()
        self.assertEqual(len(kept), 1)
        self.assertEqual(self.git(work, "log", "--format=%s", "-1", kept[0]), "fix: harden after review")
        self.assertEqual(self.git(work, "log", "--format=%s", "dev/greeting").splitlines()[-2:],
                         ["docs: move on", "initial"])

    def test_a_plan_file_edited_on_both_sides_keeps_both(self) -> None:
        self.start()
        self.assertEqual(self.wait(), 0)
        spec = self.repo / ".dev" / "greeting" / "spec.md"
        spec.write_text(SPEC + "\nlocal edit\n")
        work_spec = self.vms / "ws-ws1" / "home" / "work" / "fixture" / ".dev" / "greeting" / "spec.md"
        work_spec.write_text("remote rewrite\n")
        summary = self.pull()
        self.assertEqual(summary["plan"]["conflicts"], ["greeting/spec.md"])
        self.assertIn("local edit", spec.read_text())
        self.assertEqual((spec.parent / "spec.md.remote").read_text(), "remote rewrite\n")

    def test_a_diverged_local_branch_is_left_for_the_user(self) -> None:
        self.git(self.repo, "branch", "dev/greeting")
        self.git(self.repo, "switch", "-q", "dev/greeting")
        self.start()
        (self.repo / "mine.txt").write_text("mine\n")
        self.git(self.repo, "add", "mine.txt")
        self.git(self.repo, "commit", "-q", "-m", "feat: mine")
        self.assertEqual(self.wait(), 0)
        summary = self.pull()
        self.assertEqual(summary["branch_update"], "diverged: the instance's commits are at refs/dev-remote/ws1")
        self.assertEqual(self.git(self.repo, "log", "--format=%s", "-1"), "feat: mine")

    def test_pull_refuses_from_another_repository(self) -> None:
        self.start()
        self.assertEqual(self.wait(), 0)
        other = self.t / "laptop" / "other"
        other.mkdir()
        self.git(other, "init", "-q", "-b", "main")
        result = subprocess.run([sys.executable, str(SCRIPT), "pull", "1"], cwd=other, env=self.env,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("holds a run of fixture", result.stderr)

    # --- following a run -------------------------------------------------------

    def test_logs_render_the_session(self) -> None:
        self.start()
        self.wait()
        text = self.remote("logs", "1", "--tail", "3").stdout
        self.assertEqual(text.splitlines(), ["say: Working on the run", "tool: Bash Show status",
                                             "result: success after 3 turns, 0.0 min, error=False"],
                         "progress ticks never crowd out the session's events")

    def test_desktop_viewer_follows_the_run_until_the_next_run_replaces_it(self) -> None:
        self.env["FAKE_DISPLAY"] = ":1"
        first = self.start(mode="early-once")["run"]
        self.assertEqual(self.wait(), 0)
        [call] = self.terminals()
        self.assertEqual(call["display"], ":1")
        self.assertEqual(call["argv"][:3], ["--maximize", "--title", f"dev:remote {first}"])
        self.assertEqual(call["argv"][-4:], ["python3", f"{self.run_dir()}/dev/skills/remote/scripts/view.py",
                                             str(self.run_dir()), first])
        screen = self.viewer_output(0, "━━ done")
        for shown in (f"run {first}", "fixture · plan greeting · branch dev/greeting", "━━ session 1",
                      "● Working on the run", "Bash Show status", "$ git status", "result: success after 3 turns",
                      "↺ session 1 is not finished: there is no pull request for dev/greeting", "━━ session 2",
                      "pull request https://example.test/pr/7"):
            self.assertIn(shown, screen)
        self.assertLess(screen.index("↺ session 1 is not finished"), screen.index("━━ session 2"))
        self.assertLess(screen.index("━━ session 2"), screen.index("━━ done"))
        self.assertTrue(alive(self.viewer_pid(0)), "the window outlasts its run, showing how it ended")
        self.remote("continue", "1", "--prompt", "More.")
        self.assertEqual(self.wait(), 0)
        screen = self.viewer_output(0, "━━ session 3")
        self.assertEqual(len(self.terminals()), 1, "continue keeps the window that already follows the run")
        self.assertLess(screen.index("━━ continued"), screen.index("━━ session 3"))
        self.start()
        self.assertEqual(self.wait(), 0)
        self.assertEqual(len(self.terminals()), 2)
        self.viewer_output(1, "━━ done")
        self.assertFalse(alive(self.viewer_pid(0)), "the next run on the instance closes the earlier window")
        self.assertTrue(alive(self.viewer_pid(1)))

    def test_no_desktop_viewer_when_turned_off_or_without_a_display(self) -> None:
        self.start()
        self.assertEqual(self.wait(), 0)
        self.assertEqual(self.terminals(), [], "no DISPLAY, no window")
        self.env["FAKE_DISPLAY"] = ":1"
        self.config.write_text(self.config.read_text() + "view = false\n")
        self.start("2", "--task", "Another task")
        self.assertEqual(self.wait("2"), 0)
        self.assertEqual(self.terminals("ws2"), [])
        self.config.write_text(self.config.read_text().replace("view = false", 'view = "yes"'))
        result = self.remote("status", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("view must be true or false", result.stderr)


class RemoteUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.m = load_module()

    def test_github_repo_reads_every_github_url_form(self) -> None:
        for url in ("https://github.com/o/r.git", "git@github.com:o/r.git", "ssh://git@github.com/o/r",
                    "https://github.com/o/r/"):
            self.assertEqual(self.m.github_repo(url), "o/r", url)
        for url in ("", "/srv/repo.git", "https://gitlab.com/o/r.git"):
            self.assertEqual(self.m.github_repo(url), "", url)

    def test_an_instance_is_a_number_or_a_fleet_name(self) -> None:
        plain, listed = {"instances": []}, {"instances": ["desk", "ws7"]}
        self.assertEqual([self.m.instance(plain, a) for a in ("1", "3", "ws2", "ws-ws2")], ["ws1", "ws3", "ws2", "ws2"])
        self.assertEqual([self.m.instance(listed, a) for a in ("1", "2", "desk")], ["desk", "ws7", "desk"])
        self.assertEqual([self.m.label(plain, "ws2"), self.m.label(listed, "ws7"), self.m.label(listed, "ws2")],
                         ["2", "2", "ws2"])
        for cfg, bad in ((plain, "0"), (listed, "3"), (plain, "No Such")):
            with self.assertRaises(self.m.Refused):
                self.m.instance(cfg, bad)

    def test_flat_toml_reads_the_config_without_a_toml_library(self) -> None:
        text = ('# remote\nhost = "user@fleet#1"  # the host\nworkspaces = ["ws1", "ws2"]\n'
                "phase_timeout_hours = 8.5\nview = false\nmodel = 'a # b'\n")
        self.assertEqual(self.m.parse_flat_toml(text), {"host": "user@fleet#1", "workspaces": ["ws1", "ws2"],
                                                        "phase_timeout_hours": 8.5, "view": False, "model": "a # b"})
        for bad in ("[table]\n", "when = 2026-11-13\n"):
            with self.assertRaises(ValueError):
                self.m.parse_flat_toml(bad)

    @staticmethod
    def archive(*members: tuple[str, str]) -> bytes:
        """A tar.gz of (name, kind) members: file, link, or setuid."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for name, kind in members:
                info = tarfile.TarInfo(name)
                if kind == "link":
                    info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                    tar.addfile(info)
                    continue
                info.size, info.mode = 4, 0o4777 if kind == "setuid" else 0o644
                tar.addfile(info, io.BytesIO(b"data"))
        return buffer.getvalue()

    def test_extract_tar_keeps_untrusted_output_inside_its_folder_without_the_data_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(self.m, "HAS_DATA_FILTER", False):
            dest = Path(tmp) / "out"
            self.m.extract_tar(self.archive(("plan/spec.md", "file"), ("evil-link", "link"),
                                            ("run/tool", "setuid")), dest)
            self.assertEqual((dest / "plan" / "spec.md").read_bytes(), b"data")
            self.assertFalse((dest / "evil-link").is_symlink(), "links are not data")
            self.assertEqual((dest / "run" / "tool").stat().st_mode & 0o7777, 0o755, "no setuid, no other-write")
            for escape in ("../outside.txt", "/tmp/absolute.txt"):
                with self.assertRaises(self.m.Refused):
                    self.m.extract_tar(self.archive((escape, "file")), dest)
            self.assertFalse((Path(tmp) / "outside.txt").exists())

    def test_open_calls_reads_none_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pr = Path(tmp) / "pr.md"
            pr.write_text("## Summary\n\nx\n\n## Open calls\n\nnone\n")
            self.assertEqual(self.m.open_calls(pr), [])
            pr.write_text("## Open calls\n\n- guess at 60%\n- blocker\n\n## Other\n\nz\n")
            self.assertEqual(self.m.open_calls(pr), ["- guess at 60%", "- blocker"])
            pr.write_text("## Open calls\n\n- a concern whose line\nwraps twice\nbefore ending\n- second\n")
            self.assertEqual(self.m.open_calls(pr), ["- a concern whose line wraps twice before ending", "- second"])


class ViewTest(unittest.TestCase):
    """view.py: the stream parser behind `logs`, and the desktop terminal that follows a run."""

    def setUp(self) -> None:
        self.v = load_module(VIEW)

    @staticmethod
    def line(**event) -> str:
        return json.dumps(event)

    def assistant(self, *blocks, parent: str | None = None) -> str:
        return self.line(type="assistant", message={"content": list(blocks)}, parent_tool_use_id=parent)

    def test_parse_names_what_a_person_follows_and_skips_the_rest(self) -> None:
        parse = self.v.parse
        self.assertEqual(parse(self.line(type="system", subtype="thinking_tokens", estimated_tokens=50)), [])
        self.assertEqual(parse(self.line(type="tool_progress", parent_tool_use_id="t1", heartbeat=True)), [])
        self.assertEqual(parse(self.assistant({"type": "thinking", "thinking": ""})), [])
        self.assertEqual(parse(self.line(type="system", subtype="task_notification", status="completed")), [])
        [tool] = parse(self.assistant({"type": "tool_use", "name": "Bash",
                                       "input": {"command": "pytest -q", "description": "Run tests"}}))
        self.assertEqual((tool.kind, tool.name, tool.text, tool.detail), ("tool", "Bash", "Run tests", "pytest -q"))
        [bare] = parse(self.assistant({"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}))
        self.assertEqual((bare.text, bare.detail), ("ls", ""), "a command without a description is not repeated")
        [nested] = parse(self.assistant({"type": "text", "text": "subagent says"}, parent="toolu_1"))
        self.assertTrue(nested.nested)
        [retry] = parse(self.line(type="system", subtype="api_retry", attempt=2, max_retries=10,
                                  error_status=None, error="unknown"))
        self.assertEqual(retry.text, "api retry 2/10: unknown")
        [result] = parse(self.line(type="result", subtype="success", is_error=True, num_turns=4, duration_ms=60000))
        self.assertFalse(result.ok, "a success subtype carrying an API error is not a success")

    def test_render_event_keeps_the_logs_format(self) -> None:
        r = load_module().render_event
        self.assertEqual(r(""), [])
        self.assertEqual(r(json.dumps({"type": "user", "message": {"content": "plain"}})), [])
        self.assertEqual(r(self.assistant({"type": "tool_use", "name": "Read", "input": {"file_path": "/a/b.py"}})),
                         ["tool: Read /a/b.py"])
        self.assertEqual(r(self.assistant({"type": "text", "text": "hi"}, parent="t")), ["  say: hi"])
        self.assertEqual(r(self.line(type="runner", text="session 1 is not finished: no pull request")),
                         ["runner: session 1 is not finished: no pull request"])
        self.assertEqual(r(self.line(type="user", message={"content": [
            {"type": "tool_result", "is_error": True, "content": [{"type": "text", "text": "boom"}]}]})),
            ["tool error: boom"])

    def run_dir(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        (d / "run.json").write_text(json.dumps({"id": "r1", "repo": "fixture", "plan": "p", "branch": "dev/p"}))
        return d

    def screen(self, follower, ticks: int = 1) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            for _ in range(ticks):
                follower.tick()
        return re.sub(r"\x1b\[[0-9;]*m|\x1b\][^\x07]*\x07", "", out.getvalue())

    def init(self, session: str) -> str:
        return self.line(type="system", subtype="init", session_id=session, model="m")

    def test_follower_shows_sessions_states_and_an_event_still_being_written(self) -> None:
        d = self.run_dir()
        status, log = d / "status.json", d / "log.jsonl"
        status.write_text(json.dumps({"state": "running"}))
        follower = self.v.Follower(d)
        with mock.patch.object(self.v, "unit_active", return_value=True):
            say = self.assistant({"type": "text", "text": "Building"})
            log.write_text(self.init("s1") + "\n" + say + "\n" + say[:20])
            text = self.screen(follower)
            self.assertIn("━━ session 1", text)
            self.assertEqual(text.count("● Building"), 1, "the half-written event waits for its newline")
            with log.open("a") as handle:
                handle.write(say[20:] + "\n")
            self.assertEqual(self.screen(follower).count("● Building"), 1)
            with log.open("a") as handle:
                handle.write(self.line(type="runner", text="session 1 is not finished: there is no pull request")
                             + "\n" + self.init("s1") + "\n" + self.assistant({"type": "tool_use", "name": "Bash", "input": {
                    "command": f"cd {Path.home()}/work/fixture; python3 - <<'EOF'\nprint(1)\nEOF"}}) + "\n"
                    + self.assistant({"type": "tool_use", "name": "Bash", "input": {"command": "cd /tmp && ls"}}) + "\n")
            status.write_text(json.dumps({"state": "done", "pr": {"url": "https://x/pr/1", "draft": "true"}}))
            text = self.screen(follower)
        self.assertLess(text.index("↺ session 1 is not finished: there is no pull request"),
                        text.index("━━ session 2"))
        self.assertIn("Bash python3 - <<'EOF'", text)
        self.assertIn("    print(1)", text, "a heredoc's body follows its first line")
        self.assertIn("Bash cd /tmp && ls", text, "only a cd into the checkout is dropped")
        self.assertLess(text.index("━━ done"), text.index("pull request https://x/pr/1 (draft)"))

    def test_one_poll_that_sees_several_steps_prints_them_in_order(self) -> None:
        d = self.run_dir()
        status = d / "status.json"
        status.write_text(json.dumps({"state": "blocked", "reason": "a secret", "continued": ""}))
        follower = self.v.Follower(d)
        with mock.patch.object(self.v, "unit_active", return_value=True):
            self.assertIn("━━ blocked: a secret", self.screen(follower))
            (d / "log.jsonl").write_text(self.init("s1") + "\n"
                                         + self.assistant({"type": "text", "text": "going on"}) + "\n")
            status.write_text(json.dumps({"state": "done", "continued": "t1"}))
            text = self.screen(follower)
        marks = ["━━ continued", "━━ session 1", "● going on", "━━ done"]
        self.assertEqual([text.index(m) for m in marks], sorted(text.index(m) for m in marks), text)

    def test_follower_names_a_runner_that_vanished(self) -> None:
        d = self.run_dir()
        (d / "status.json").write_text(json.dumps({"state": "running"}))
        follower = self.v.Follower(d)
        with mock.patch.object(self.v, "unit_active", return_value=False):
            text = self.screen(follower)
        self.assertIn("━━ lost", text)
        with mock.patch.object(self.v, "unit_active", return_value=False):
            follower.next_unit_check = 0
            self.assertNotIn("lost", self.screen(follower), "said once")
        (d / "status.json").write_text(json.dumps({"state": "running", "continued": "t2"}))
        with mock.patch.object(self.v, "unit_active", return_value=True):
            follower.next_unit_check = 0
            self.assertIn("━━ continued", self.screen(follower))
        self.assertFalse(follower.lost)


if __name__ == "__main__":
    unittest.main()
