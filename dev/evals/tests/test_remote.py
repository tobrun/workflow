"""Tests for the remote skill's transport: remote.py on this side, runner.sh in the sandbox.

Run from the repo root: python3 -m unittest discover -s dev/evals/tests -t .
Each test builds a laptop checkout, a bare origin, an empty sources root, and
two fake workspaces, then drives remote.py as a subprocess with the `local`
transport. The fakes in remote_fakes/ stand in for incus, fleet, systemctl,
and claude with the real commands' git semantics, so a test proves the whole
dispatch -> run -> collect cycle on real git, without a fleet host or a model.
"""

from __future__ import annotations

import importlib.util
import json
import io
import os
import re
import shutil
import subprocess
import sys
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
        self.sources = t / "sources"
        self.vms = t / "vms"
        for path in (self.repo, self.sources, self.vms / "ws-ws1" / "home", self.vms / "ws-ws2" / "home",
                     t / "home"):
            path.mkdir(parents=True)
        (t / "remote.toml").write_text(
            f'transport = "local"\nsources_root = "{self.sources}"\nworkspaces = ["ws1", "ws2"]\n')
        self.env = {**os.environ, **IDENTITY,
                    "HOME": str(t / "home"), "GIT_CONFIG_NOSYSTEM": "1",
                    "PATH": os.pathsep.join([str(FAKES), str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
                    "DEV_REMOTE_CONFIG": str(t / "remote.toml"), "DEV_REMOTE_HOME": str(t / "state"),
                    "DEV_REMOTE_TMP": str(t / "laptop-tmp"),
                    "FAKE_FLEET_ROOT": str(self.vms), "FAKE_SOURCES_ROOT": str(self.sources)}
        self.env.pop("FAKE_CLAUDE_MODE", None)
        origin = t / "origin.git"
        self.git(t, "init", "-q", "--bare", "-b", "main", str(origin))
        self.git(self.repo, "init", "-q", "-b", "main")
        (self.repo / ".gitignore").write_text("/.dev/*\n!/.dev/config.yaml\n")
        (self.repo / "README.md").write_text("# fixture\n")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-q", "-m", "initial")
        self.git(self.repo, "remote", "add", "origin", str(origin))
        self.git(self.repo, "push", "-q", "-u", "origin", "main")
        self.base = self.git(self.repo, "rev-parse", "HEAD")
        plan = self.repo / ".dev" / "greeting"
        plan.mkdir(parents=True)
        (plan / "spec.md").write_text(SPEC)
        self.addCleanup(self.stop_all)

    def stop_all(self) -> None:
        """No background run outlives its test, whatever the test asserted."""
        for unit in self.vms.glob("ws-*/units/*.pid"):
            try:
                os.killpg(int(unit.read_text()), 9)
            except (ProcessLookupError, PermissionError, ValueError):
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

    def dispatch(self, *extra: str, mode: str | None = None) -> dict:
        return json.loads(self.remote("dispatch", "--plan", "greeting", *extra, mode=mode).stdout)

    def wait(self, run: str) -> int:
        return self.remote("wait", run, "--interval", "0.1", check=False).returncode

    def status(self, run: str) -> dict:
        return json.loads(self.remote("status", run, "--json").stdout)[0]

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

    # --- the cycle -----------------------------------------------------------

    def test_full_cycle_brings_commits_plan_reports_and_sessions_home(self) -> None:
        out = self.dispatch()
        run = out["run"]
        self.assertEqual(out["branch"], "dev/greeting")
        self.assertEqual(out["base"], self.base)
        self.assertEqual(self.wait(run), 0)
        self.assertEqual(self.status(run)["state"], "done")
        prompts = [c["prompt"] for c in self.calls(out["workspace"])]
        self.assertEqual(prompts, ["/dev:build greeting", "/dev:ship plan .dev/greeting"])
        argv = self.calls(out["workspace"])[0]["argv"]
        for flag in ("--permission-mode", "bypassPermissions", "--disallowed-tools", "AskUserQuestion",
                     "--output-format", "stream-json"):
            self.assertIn(flag, argv)
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        self.assertEqual(settings["attribution"], {"commit": "", "pr": ""}, "no Co-Authored-By in sandbox commits")
        self.assertEqual({c["bg_wait_ms"] for c in self.calls(out["workspace"])}, {"6000000"},
                         "every phase waits ten times Claude Code's 600 s default for its background agents")

        summary = json.loads(self.remote("collect", run).stdout)
        self.assertEqual(summary["branch_update"], "created")
        self.assertEqual(len(summary["commits"]), 2)
        self.assertEqual(self.git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main", "checkout untouched")
        log = self.git(self.repo, "log", "--format=%s", "main..dev/greeting")
        self.assertEqual(log.splitlines(), ["fix: harden after review", "feat: build the feature"])
        plan = self.repo / ".dev" / "greeting"
        self.assertTrue((plan / "implementation-notes.md").exists())
        self.assertEqual(summary["verdict"], "PASS")
        self.assertEqual(summary["open_calls"], [])
        self.assertIn("review_1.md", summary["plan"]["updated"])
        self.assertEqual(summary["plan"]["conflicts"], [])
        report = self.t / "laptop-tmp" / "fixture" / "reports" / "greeting-e2e.json"
        self.assertTrue(report.exists())

        worktree = Path(summary["worktree"])
        self.assertEqual(worktree.name, "fixture", "worktree keeps the repo name for /tmp/{project-slug}")
        self.assertEqual(self.git(worktree, "rev-parse", "--abbrev-ref", "HEAD"), "dev/greeting")
        self.assertEqual((worktree / ".dev" / "greeting").resolve(), plan.resolve())

        artifacts = self.t / "state" / "runs" / run
        sessions = [c["session"] for c in self.calls(out["workspace"])]
        self.assertEqual(summary["sessions"], sorted(sessions), "every phase session's transcript comes home")
        for sid in sessions:
            self.assertTrue((artifacts / "sessions" / f"{sid}.jsonl").exists())
            self.assertTrue((artifacts / "sessions" / sid / "subagents" / "agent-1.jsonl").exists(),
                            "with its subagents, so a later round can continue it anywhere")
        self.assertTrue((self.t / "state" / "runs" / run / "logs" / "build.jsonl").exists())

        lock = self.vms / f"ws-{out['workspace']}" / "home" / ".dev-workflow" / "lock"
        self.assertFalse(lock.exists(), "collect releases the workspace")
        self.assertIn("already", self.remote("collect", run, check=False).stderr)

    # --- publication -----------------------------------------------------------

    def contract(self, out: dict) -> str:
        return (self.vms / f"ws-{out['workspace']}" / "home" / ".dev-workflow" / "runs" / out["run"]
                / "contract.md").read_text()

    def origin_branches(self) -> list[str]:
        return self.git(self.t / "origin.git", "for-each-ref", "--format=%(refname:short)", "refs/heads").split()

    def test_sandbox_publishes_when_its_credentials_can_push(self) -> None:
        # As on the fleet: origin is a github.com URL that the workspace's git config rewrites to a relay.
        relay = self.t / "relay.gitconfig"
        relay.write_text(f'[url "{self.t / "origin.git"}"]\n\tinsteadOf = https://github.com/test/fixture.git\n')
        self.env.update(FAKE_GH="signed-in", FAKE_GITCONFIG=str(relay))
        self.git(self.repo, "remote", "set-url", "origin", "https://github.com/test/fixture.git")
        out = self.dispatch()
        self.assertEqual(out["publish"], {"mode": "sandbox"})
        contract = self.contract(out)
        self.assertIn("Publication is authorized", contract)
        self.assertNotIn("Publication is withheld", contract)
        self.assertNotIn("<!-- publish", contract)
        self.assertNotIn("{branch}", contract)
        self.assertEqual(self.origin_branches(), ["main"], "the probe pushes nothing")
        self.assertEqual(self.wait(out["run"]), 0)
        self.assertIn("dev/greeting", self.origin_branches())
        summary = json.loads(self.remote("collect", out["run"]).stdout)
        self.assertEqual(summary["publish"], {"mode": "sandbox"})
        self.assertEqual(summary["pr"], {"url": "https://example.test/pr/7", "number": "7", "state": "OPEN",
                                         "draft": "false"})
        self.assertEqual(self.git(self.repo, "rev-parse", "dev/greeting"),
                         self.git(self.t / "origin.git", "rev-parse", "dev/greeting"),
                         "the laptop branch matches what the sandbox published")

    def test_auto_withholds_publication_when_gh_is_signed_out(self) -> None:
        out = self.dispatch()
        self.assertEqual(out["publish"], {"mode": "laptop", "reason": "gh is not signed in"})
        self.assertIn("Publication is withheld", self.contract(out))
        self.assertNotIn("Publication is authorized", self.contract(out))
        self.assertEqual(self.wait(out["run"]), 0)
        self.assertEqual(self.origin_branches(), ["main"])
        summary = json.loads(self.remote("collect", out["run"]).stdout)
        self.assertIsNone(summary["pr"])

    def test_auto_withholds_publication_when_origin_rejects_the_push(self) -> None:
        self.env["FAKE_GH"] = "signed-in"
        self.git(self.repo, "remote", "set-url", "origin", str(self.t / "missing.git"))
        out = self.dispatch("--phases", "build,ship")
        self.assertEqual(out["publish"]["mode"], "laptop")
        self.assertTrue(out["publish"]["reason"].startswith("git push to origin failed"), out["publish"])

    def test_requested_sandbox_publication_refuses_without_credentials(self) -> None:
        result = self.remote("dispatch", "--plan", "greeting", "--publish", "sandbox", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("the workspace cannot publish: gh is not signed in", result.stderr)
        self.assertEqual(list(self.vms.glob("ws-*/home/.dev-workflow/lock")), [], "the refusal frees the workspace")

    def test_requested_laptop_publication_and_build_only_runs_skip_the_probe(self) -> None:
        self.env["FAKE_GH"] = "signed-in"
        self.assertEqual(self.dispatch("--publish", "laptop")["publish"], {"mode": "laptop", "reason": "requested"})
        self.assertEqual(self.dispatch("--phases", "build", "--branch", "dev/other")["publish"],
                         {"mode": "laptop", "reason": "no ship phase"})

    def test_logs_render_the_session(self) -> None:
        run = self.dispatch()["run"]
        self.wait(run)
        text = self.remote("logs", run, "--tail", "3").stdout
        self.assertEqual(text.splitlines(), ["say: Working on ship for greeting", "tool: Bash Show status",
                                             "result: success after 3 turns, 0.0 min, error=False"],
                         "progress ticks never crowd out the session's events")

    def test_desktop_viewer_follows_the_run_until_the_next_run_replaces_it(self) -> None:
        self.env["FAKE_DISPLAY"] = ":1"
        first = self.dispatch("--workspace", "ws1", "--phases", "build")["run"]
        self.assertEqual(self.wait(first), 0)
        [call] = self.terminals()
        self.assertEqual(call["display"], ":1")
        self.assertEqual(call["argv"][:3], ["--maximize", "--title", f"dev:remote {first}"])
        self.assertEqual(call["argv"][-3:], ["python3", f"{self.vms}/ws-ws1/home/.dev-workflow/runs/{first}/dev/"
                                             "skills/remote/scripts/view.py",
                                             f"{self.vms}/ws-ws1/home/.dev-workflow/runs/{first}"])
        screen = self.viewer_output(0, "━━ done")
        for shown in (f"run {first}", "━━ phase build", "● Working on build for greeting", "Bash Show status",
                      "$ git status", "result: success after 3 turns"):
            self.assertIn(shown, screen)
        self.assertLess(screen.index("━━ phase build"), screen.index("━━ done"))
        self.assertTrue(alive(self.viewer_pid(0)), "the window outlasts its run, showing how it ended")
        self.remote("collect", first)
        second = self.dispatch("--workspace", "ws1", "--phases", "ship")["run"]
        self.assertEqual(self.wait(second), 0)
        self.assertEqual(len(self.terminals()), 2)
        self.viewer_output(1, "━━ done")
        self.assertFalse(alive(self.viewer_pid(0)), "the next run on the workspace closes the earlier window")
        self.assertTrue(alive(self.viewer_pid(1)))

    def test_desktop_viewer_survives_a_resume_and_shows_it(self) -> None:
        self.env["FAKE_DISPLAY"] = ":1"
        run = self.dispatch(mode="fail-build-once")["run"]
        self.assertEqual(self.wait(run), 1)
        self.viewer_output(0, "━━ failed: build ended with exit 1")
        self.remote("resume", run, mode="fail-build-once")
        self.assertEqual(self.wait(run), 0)
        screen = self.viewer_output(0, "━━ done")
        self.assertEqual(len(self.terminals()), 1, "resume keeps the window that already follows the run")
        self.assertLess(screen.index("━━ failed"), screen.index("━━ resumed"))
        self.assertLess(screen.index("━━ resumed"), screen.index("━━ phase ship"))

    def test_no_desktop_viewer_when_turned_off_or_without_a_display(self) -> None:
        run = self.dispatch("--phases", "build")["run"]
        self.assertEqual(self.wait(run), 0)
        self.assertEqual(self.terminals(), [], "no DISPLAY, no window")
        self.remote("collect", run)
        self.env["FAKE_DISPLAY"] = ":1"
        config = self.t / "remote.toml"
        config.write_text(config.read_text() + "view = false\n")
        run = self.dispatch("--phases", "build")["run"]
        self.assertEqual(self.wait(run), 0)
        self.assertEqual(self.terminals(), [])
        config.write_text(config.read_text().replace("view = false", 'view = "yes"'))
        result = self.remote("status", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("view must be true or false", result.stderr)

    def test_second_run_on_the_same_workspace_starts_from_a_clean_checkout(self) -> None:
        first = self.dispatch("--workspace", "ws1", "--phases", "build", mode="dirty")
        self.assertEqual(self.wait(first["run"]), 0)
        summary = json.loads(self.remote("collect", first["run"]).stdout)
        self.assertEqual(len(summary["uncommitted_patches"]), 1)
        self.assertIn("stray.txt", Path(summary["uncommitted_patches"][0]).read_text())
        worktree = Path(summary["worktree"])
        laptop_reports = self.t / "laptop-tmp" / "fixture" / "reports"
        laptop_reports.mkdir(parents=True, exist_ok=True)
        (laptop_reports / "from-laptop.html").write_text("<html>earlier e2e</html>\n")
        # Still on main here: the existing work branch, not HEAD, is what continues.
        second = self.dispatch("--workspace", "ws1", "--phases", "ship")
        self.assertEqual(second["branch"], "dev/greeting")
        self.assertEqual(second["sent"], "refs/heads/dev/greeting")
        self.assertEqual(self.wait(second["run"]), 0)
        work = self.vms / "ws-ws1" / "home" / "work" / "fixture"
        self.assertFalse((work / "stray.txt").exists())
        self.assertTrue((work / "feature.txt").exists(), "the sandbox continued from the built branch")
        self.assertTrue((self.vms / "ws-ws1" / "tmp" / "fixture" / "reports" / "from-laptop.html").exists(),
                        "reports collected earlier travel with the next dispatch")
        summary = json.loads(self.remote("collect", second["run"]).stdout)
        self.assertEqual(summary["branch_update"], "fast-forwarded")
        self.assertEqual(summary["worktree"], str(worktree))
        self.assertTrue((worktree / "ship.txt").exists(), "the checked-out branch fast-forwards in place")

    def test_refuses_when_fleet_sync_leaves_the_workspace_stale(self) -> None:
        first = self.dispatch("--workspace", "ws1", "--phases", "build")
        self.assertEqual(self.wait(first["run"]), 0)
        self.remote("collect", first["run"])
        self.git(self.repo, "checkout", "-q", "-b", "dev/next", "main")
        self.env["FAKE_FLEET_SYNC"] = "stale"
        result = self.remote("dispatch", "--plan", "greeting", "--workspace", "ws1", "--phases", "build",
                             check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("fleet sync did not deliver dev/next to ws1", result.stderr)
        self.assertEqual(list(self.vms.glob("ws-*/home/.dev-workflow/lock")), [], "the refusal frees the workspace")

    def test_base_is_the_local_default_when_there_is_no_origin(self) -> None:
        self.git(self.repo, "remote", "remove", "origin")
        self.git(self.repo, "checkout", "-q", "-b", "dev/greeting")
        (self.repo / "work.txt").write_text("done\n")
        self.git(self.repo, "add", "work.txt")
        self.git(self.repo, "commit", "-q", "-m", "work")
        self.git(self.repo, "checkout", "-q", "main")
        out = self.dispatch("--phases", "ship")
        self.assertEqual(out["sent"], "refs/heads/dev/greeting")
        self.assertEqual(out["base"], self.base, "ship must diff the branch against main, not against itself")

    def test_refuses_when_the_work_branch_checkout_is_dirty(self) -> None:
        self.git(self.repo, "checkout", "-q", "-b", "dev/greeting")
        self.git(self.repo, "checkout", "-q", "main")
        tree = self.t / "wt" / "fixture"
        self.git(self.repo, "worktree", "add", "-q", str(tree), "dev/greeting")
        (tree / "README.md").write_text("edited in the worktree\n")
        result = self.remote("dispatch", "--plan", "greeting", check=False)
        self.assertIn(f"tracked files in {tree}", result.stderr)

    # --- refusals ------------------------------------------------------------

    def test_refuses_uncommitted_tracked_changes(self) -> None:
        (self.repo / "README.md").write_text("changed\n")
        result = self.remote("dispatch", "--plan", "greeting", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("uncommitted changes", result.stderr)

    def test_refuses_plan_files_that_git_would_track(self) -> None:
        (self.repo / ".gitignore").write_text("\n")
        self.git(self.repo, "commit", "-q", "-am", "drop ignore")
        result = self.remote("dispatch", "--plan", "greeting", check=False)
        self.assertIn("not git-ignored", result.stderr)
        self.assertIn("dispatch sends the plan directory to the sandbox with the run", result.stderr)
        self.assertIn("add `/.dev/*` and `!/.dev/config.yaml` to .gitignore", result.stderr)

    def test_refuses_a_committed_plan_and_says_how_to_untrack_it(self) -> None:
        self.git(self.repo, "add", "-f", ".dev/greeting/spec.md")
        self.git(self.repo, "commit", "-q", "-m", "commit the plan")
        result = self.remote("dispatch", "--plan", "greeting", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn(".dev/greeting is committed", result.stderr)
        self.assertIn("git rm -r --cached .dev/greeting", result.stderr)

    def test_refuses_an_unsettled_spec(self) -> None:
        (self.repo / ".dev" / "greeting" / "spec.md").write_text(SPEC.replace("(✓ plain text)", "(✓ cloud)"))
        result = self.remote("dispatch", "--plan", "greeting", check=False)
        self.assertIn("not settled", result.stderr)

    def test_busy_workspaces_refuse_and_release_frees_one(self) -> None:
        a = self.dispatch(mode="sleep")
        self.git(self.repo, "checkout", "-q", "-b", "other")
        b = self.dispatch(mode="sleep")
        self.assertNotEqual(a["workspace"], b["workspace"])
        self.git(self.repo, "checkout", "-q", "-b", "third")
        result = self.remote("dispatch", "--plan", "greeting", check=False)
        self.assertIn("every workspace is busy", result.stderr)
        self.assertIn(a["run"], result.stderr)
        refused = self.remote("release", a["run"], check=False)
        self.assertIn("stop it first", refused.stderr)
        self.remote("stop", a["run"])
        self.remote("stop", b["run"])
        self.assertTrue(json.loads(self.remote("release", a["run"]).stdout)["lock_released"])
        self.assertEqual(self.dispatch(mode="sleep")["workspace"], a["workspace"])

    def test_run_ids_stay_unique_within_one_second(self) -> None:
        a = self.dispatch(mode="sleep")
        self.git(self.repo, "checkout", "-q", "-b", "other")
        b = self.dispatch(mode="sleep")
        self.assertNotEqual(a["run"], b["run"])
        self.assertEqual(len(json.loads(self.remote("status", "--json").stdout)), 2)

    def test_refuses_a_second_run_on_the_same_branch(self) -> None:
        self.dispatch(mode="sleep")
        result = self.remote("dispatch", "--plan", "greeting", check=False)
        self.assertIn("already working on dev/greeting", result.stderr)

    # --- failure handling ----------------------------------------------------

    def test_failed_phase_resumes_its_own_session_then_runs_the_rest(self) -> None:
        run = self.dispatch(mode="fail-build-once")["run"]
        self.assertEqual(self.wait(run), 1)
        status = self.status(run)
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["phase"], "build")
        self.assertIn("error_during_execution", status["reason"])
        first_session = self.calls()[0]["session"]
        self.remote("resume", run, mode="fail-build-once")
        self.assertEqual(self.wait(run), 0)
        calls = self.calls()
        self.assertEqual(calls[1]["resume"], first_session)
        self.assertIn("Continue the /dev:build greeting run", calls[1]["prompt"])
        self.assertEqual(calls[2]["prompt"], "/dev:ship plan .dev/greeting")

    def test_gateway_error_is_named_and_resumes(self) -> None:
        run = self.dispatch(mode="api-error-once")["run"]
        self.assertEqual(self.wait(run), 1)
        self.assertEqual(self.status(run)["reason"], "build ended with exit 1 (api-error)")
        self.remote("resume", run, mode="api-error-once")
        self.assertEqual(self.wait(run), 0)

    def resume(self, *args: str, mode: str | None = None) -> dict:
        return json.loads(self.remote("resume", *args, mode=mode).stdout)

    def test_a_collected_run_continues_its_last_session(self) -> None:
        run = self.dispatch()["run"]
        self.assertEqual(self.wait(run), 0)
        self.remote("collect", run)
        ship_session = self.calls()[1]["session"]
        out = self.resume(run, "--prompt", "Finish the mutation pass.")
        self.assertEqual((out["how"], out["workspace"], out["round"]), ("restored", "ws1", 2))
        self.assertEqual((out["phase"], out["mode"], out["session"]), ("ship", "continue", ship_session))
        self.assertIn(self.status(run)["state"], ("starting", "running", "done"))
        self.assertEqual(self.wait(run), 0)
        last = self.calls()[-1]
        self.assertEqual(last["resume"], ship_session)
        self.assertTrue(last["prompt"].startswith("Continue the /dev:ship plan .dev/greeting run"))
        self.assertTrue(last["prompt"].endswith("\n\nFinish the mutation pass."))
        self.assertEqual(len(self.calls()), 3, "only ship continues; build is not run again")
        summary = json.loads(self.remote("collect", run).stdout)
        self.assertEqual(summary["state"], "done", "a second round collects like the first")
        record = json.loads((self.t / "state" / "runs" / f"{run}.json").read_text())
        self.assertEqual([r["round"] for r in record["rounds"]], [2])
        self.assertEqual(record["state"], "collected")

    def test_a_restored_run_takes_this_machines_branch_and_plan_to_another_workspace(self) -> None:
        run = self.dispatch("--workspace", "ws1")["run"]
        self.assertEqual(self.wait(run), 0)
        summary = json.loads(self.remote("collect", run).stdout)
        worktree = Path(summary["worktree"])
        (worktree / "laptop.txt").write_text("a fix made here after collect\n")
        self.git(worktree, "add", "laptop.txt")
        self.git(worktree, "commit", "-q", "-m", "fix: made on the laptop")
        (self.repo / ".dev" / "greeting" / "notes.md").write_text("edited here\n")
        lock = self.vms / "ws-ws1" / "home" / ".dev-workflow" / "lock"
        lock.mkdir()
        (lock / "run").write_text("someone-else")
        ship_session = self.calls("ws1")[1]["session"]
        out = self.resume(run)
        self.assertEqual((out["workspace"], out["mode"], out["session"]), ("ws2", "continue", ship_session),
                         "the run's workspace is busy, so another one continues the same session")
        self.assertEqual(self.wait(run), 0)
        self.assertEqual(self.calls("ws2")[-1]["resume"], ship_session,
                         "the collected transcript went with the run, or the fake would have failed the resume")
        work = self.vms / "ws-ws2" / "home" / "work" / "fixture"
        self.assertTrue((work / "laptop.txt").exists(), "the laptop's commit is what the round continues from")
        self.assertEqual((work / ".dev" / "greeting" / "notes.md").read_text(), "edited here\n")
        logs = self.vms / "ws-ws2" / "home" / ".dev-workflow" / "runs" / run / "logs"
        self.assertTrue((logs / "build.jsonl").exists(), "the run's earlier logs travel with it")
        self.remote("collect", run)
        self.assertEqual(self.git(self.repo, "log", "-1", "--format=%s", "dev/greeting"), "fix: harden after review")

    def test_from_starts_a_phase_fresh_with_an_instruction(self) -> None:
        run = self.dispatch()["run"]
        self.assertEqual(self.wait(run), 0)
        out = self.resume(run, "--from", "ship", "--prompt", "CI failed on lint; fix it.")
        self.assertEqual((out["how"], out["mode"], out["session"]), ("in place", "fresh", None),
                         "an uncollected run resumes in place, even a finished one")
        self.assertEqual(self.wait(run), 0)
        last = self.calls()[-1]
        self.assertIsNone(last["resume"])
        self.assertEqual(last["prompt"], "/dev:ship plan .dev/greeting\n\nCI failed on lint; fix it.")

    def test_a_later_session_is_judged_by_its_own_stream(self) -> None:
        run = self.dispatch(mode="crash-ship-again")["run"]
        self.assertEqual(self.wait(run), 0)
        self.resume(run, "--from", "ship", mode="crash-ship-again")
        self.assertEqual(self.wait(run), 1, "the first ship session's success is not the second one's")
        self.assertEqual(self.status(run)["reason"], "ship ended with exit 1 (no-result)")

    def test_resume_refuses_a_live_run_and_a_point_it_cannot_reach(self) -> None:
        run = self.dispatch("--phases", "build", mode="sleep")["run"]
        for _ in range(100):
            if self.calls():
                break
            time.sleep(0.1)
        result = self.remote("resume", run, check=False)
        self.assertIn("stop it first", result.stderr)
        self.remote("stop", run)
        result = self.remote("resume", run, "--continue", "ship", check=False)
        self.assertIn("ship is not among the run's phases", result.stderr)
        result = self.remote("resume", run, "--continue", "ship", "--phases", "build,ship", check=False)
        self.assertIn("no session was recorded for ship", result.stderr)
        self.assertIn("--from ship starts it fresh", result.stderr)

    def test_continue_without_the_transcript_anywhere_points_to_from(self) -> None:
        run = self.dispatch("--workspace", "ws1", "--phases", "build")["run"]
        self.assertEqual(self.wait(run), 0)
        self.remote("collect", run)
        build_session = self.calls("ws1")[0]["session"]
        shutil.rmtree(self.t / "state" / "runs" / run / "sessions")
        (self.vms / "ws-ws1" / "home" / ".dev-workflow" / "lock").mkdir()
        result = self.remote("resume", run, check=False)
        self.assertIn(f"session {build_session} is neither on ws-ws2 nor collected here", result.stderr)
        self.assertEqual(list(self.vms.glob("ws-ws2/home/.dev-workflow/lock")), [], "the refusal frees ws2")
        self.assertEqual(self.resume(run, "--from", "build")["mode"], "fresh")
        self.assertEqual(self.wait(run), 0)

    def test_build_gate_resumes_a_build_that_skipped_its_evidence(self) -> None:
        out = self.dispatch(mode="skip-evidence-once")
        self.assertEqual(self.wait(out["run"]), 0)
        builds = [c for c in self.calls(out["workspace"]) if "/dev:build" in c["prompt"]]
        self.assertEqual(len(builds), 2, "one resume of the same build session")
        self.assertEqual(builds[1]["resume"], builds[0]["session"])
        self.assertIn("check-tests.py", builds[1]["prompt"])
        self.assertIn("test-evidence.json", builds[1]["prompt"], "the gate's own output reaches the session")
        status = json.loads((self.vms / f"ws-{out['workspace']}" / "home" / ".dev-workflow" / "runs" / out["run"]
                             / "status.json").read_text())
        self.assertEqual(status["gate"]["build"], "pass")

    def test_build_gate_fails_the_run_after_its_resumes(self) -> None:
        out = self.dispatch(mode="skip-evidence")
        self.assertEqual(self.wait(out["run"]), 1)
        self.assertTrue(self.status(out["run"])["reason"].startswith("build gate: check-tests.py still fails"))
        prompts = [c["prompt"] for c in self.calls(out["workspace"])]
        self.assertEqual(len(prompts), 3, "the build and two gate resumes; ship never starts")
        summary = json.loads(self.remote("collect", out["run"]).stdout)
        self.assertTrue((Path(summary["artifacts"]) / "gate-build.txt").exists())

    def test_stop_records_stopped_and_collect_takes_what_exists(self) -> None:
        run = self.dispatch(mode="dirty-sleep")["run"]
        work = self.vms / "ws-ws1" / "home" / "work" / "fixture"
        for _ in range(100):
            if (work / "in-flight.txt").exists():
                break
            time.sleep(0.1)
        self.assertEqual(self.remote("collect", run, check=False).returncode, 1, "a running run is not collected")
        self.remote("stop", run)
        self.assertEqual(self.status(run)["state"], "stopped")
        summary = json.loads(self.remote("collect", run).stdout)
        self.assertEqual(summary["state"], "stopped")
        self.assertEqual(summary["commits"], [])
        patches = [Path(p) for p in summary["uncommitted_patches"]]
        self.assertEqual([p.name for p in patches], ["uncommitted-build.patch"])
        self.assertIn("in-flight.txt", patches[0].read_text(), "a stop keeps the in-flight work beside the logs")

    def test_lost_when_the_unit_dies_without_recording_an_end(self) -> None:
        run = self.dispatch(mode="sleep")["run"]
        for _ in range(100):
            if self.calls():
                break
            time.sleep(0.1)
        pid = int((self.vms / "ws-ws1" / "units" / f"dev-run-{run}.pid").read_text())
        os.killpg(pid, 9)
        time.sleep(0.3)
        self.assertEqual(self.status(run)["state"], "lost")

    def test_refuses_when_fleet_sync_never_clones(self) -> None:
        self.env["FAKE_FLEET_SYNC"] = "skip"
        result = self.remote("dispatch", "--plan", "greeting", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("the workspace has no such ref", result.stderr)
        self.assertEqual(list(self.vms.glob("ws-*/home/.dev-workflow/lock")), [], "the refusal frees the workspace")
        self.assertEqual(list((self.t / "state" / "runs").glob("*.json")) if (self.t / "state" / "runs").exists()
                         else [], [], "a refused dispatch leaves no run record")

    def test_plan_edited_on_both_sides_keeps_both(self) -> None:
        run = self.dispatch("--phases", "build", mode="edit-spec")["run"]
        self.wait(run)
        spec = self.repo / ".dev" / "greeting" / "spec.md"
        spec.write_text(SPEC + "\nlocal note\n")
        summary = json.loads(self.remote("collect", run).stdout)
        self.assertEqual(summary["plan"]["conflicts"], ["spec.md"])
        self.assertIn("local note", spec.read_text())
        self.assertEqual((spec.parent / "spec.md.remote").read_text(), "remote rewrite\n")

    def test_diverged_branch_is_left_for_the_user(self) -> None:
        self.git(self.repo, "checkout", "-q", "-b", "feature")
        run = self.dispatch("--phases", "build")["run"]
        self.wait(run)
        (self.repo / "local.txt").write_text("local\n")
        self.git(self.repo, "add", "local.txt")
        self.git(self.repo, "commit", "-q", "-m", "local work")
        result = self.remote("collect", run, check=False)
        self.assertIn("diverged", result.stderr)
        self.assertTrue(self.git(self.repo, "rev-parse", f"refs/dev-remote/{run}"))
        self.assertEqual(self.git(self.repo, "log", "-1", "--format=%s"), "local work")


class RemoteUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.m = load_module()

    def test_github_repo_reads_every_github_url_form(self) -> None:
        mod = load_module()
        for url in ("https://github.com/o/r.git", "git@github.com:o/r.git", "ssh://git@github.com/o/r",
                    "https://github.com/o/r/"):
            self.assertEqual(mod.github_repo(url), "o/r", url)
        for url in ("", "/srv/repo.git", "https://gitlab.com/o/r.git"):
            self.assertEqual(mod.github_repo(url), "", url)

    def test_normalize_url_matches_ssh_and_https_forms(self) -> None:
        n = self.m.normalize_url
        self.assertEqual(n("git@github.com:Owner/Repo.git"), n("https://github.com/owner/repo"))
        self.assertNotEqual(n("git@github.com:owner/repo.git"), n("git@github.com:owner/other.git"))

    def test_flat_toml_reads_the_config_without_a_toml_library(self) -> None:
        text = ('# remote\nhost = "user@fleet#1"  # the host\nworkspaces = ["ws1", "ws2"]\n'
                "phase_timeout_hours = 8.5\nview = false\nmodel = 'a # b'\n")
        self.assertEqual(self.m.parse_flat_toml(text), {"host": "user@fleet#1", "workspaces": ["ws1", "ws2"],
                                                        "phase_timeout_hours": 8.5, "view": False, "model": "a # b"})
        for bad in ("[table]\n", "when = 2026-11-13\n"):
            with self.assertRaises(ValueError):
                self.m.parse_flat_toml(bad)

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
        self.assertEqual(r(self.line(type="user", message={"content": [
            {"type": "tool_result", "is_error": True, "content": [{"type": "text", "text": "boom"}]}]})),
            ["tool error: boom"])

    def run_dir(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        (d / "logs").mkdir()
        (d / "run.json").write_text(json.dumps({"id": "r1", "repo": "fixture", "plan": "p", "branch": "dev/p",
                                                "phases": ["build", "ship"], "publish": "laptop"}))
        return d

    def screen(self, follower, ticks: int = 1) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            for _ in range(ticks):
                follower.tick()
        return re.sub(r"\x1b\[[0-9;]*m|\x1b\][^\x07]*\x07", "", out.getvalue())

    def test_follower_shows_phases_states_and_an_event_still_being_written(self) -> None:
        d = self.run_dir()
        status = d / "status.json"
        status.write_text(json.dumps({"state": "starting"}))
        follower = self.v.Follower(d)
        with mock.patch.object(self.v, "unit_active", return_value=True):
            self.assertIn("━━ starting", self.screen(follower))
            status.write_text(json.dumps({"state": "running", "phase": "build"}))
            say = self.assistant({"type": "text", "text": "Building"})
            (d / "logs" / "build.jsonl").write_text(say + "\n" + say[:20])
            text = self.screen(follower)
            self.assertIn("━━ phase build", text)
            self.assertEqual(text.count("● Building"), 1, "the half-written event waits for its newline")
            with (d / "logs" / "build.jsonl").open("a") as handle:
                handle.write(say[20:] + "\n")
            self.assertEqual(self.screen(follower).count("● Building"), 1)
            status.write_text(json.dumps({"state": "running", "phase": "build", "gate": {"build": "resume 1"}}))
            self.assertIn("build gate: resume 1", self.screen(follower))
            (d / "logs" / "ship.jsonl").write_text(self.assistant({"type": "tool_use", "name": "Bash", "input": {
                "command": f"cd {Path.home()}/work/fixture; python3 - <<'EOF'\nprint(1)\nEOF"}}) + "\n"
                + self.assistant({"type": "tool_use", "name": "Bash", "input": {"command": "cd /tmp && ls"}}) + "\n")
            status.write_text(json.dumps({"state": "done", "phase": ""}))
            text = self.screen(follower)
        self.assertIn("━━ phase ship", text)
        self.assertIn("Bash python3 - <<'EOF'", text)
        self.assertIn("    print(1)", text, "a heredoc's body follows its first line")
        self.assertIn("Bash cd /tmp && ls", text, "only a cd into the checkout is dropped")
        self.assertIn("━━ done", text)

    def test_one_poll_that_sees_several_steps_prints_them_in_order(self) -> None:
        d = self.run_dir()
        status = d / "status.json"
        status.write_text(json.dumps({"state": "failed", "phase": "build", "resumed": ""}))
        follower = self.v.Follower(d)
        with mock.patch.object(self.v, "unit_active", return_value=True):
            self.screen(follower)
            (d / "logs" / "build.jsonl").write_text(self.assistant({"type": "text", "text": "resumed build"}) + "\n")
            (d / "logs" / "ship.jsonl").write_text(self.assistant({"type": "text", "text": "shipping"}) + "\n")
            status.write_text(json.dumps({"state": "done", "resumed": "t1", "gate": {"build": "pass"}}))
            text = self.screen(follower)
        marks = ["━━ resumed", "● resumed build", "build gate: pass", "━━ phase ship", "● shipping", "━━ done"]
        self.assertEqual([text.index(m) for m in marks], sorted(text.index(m) for m in marks), text)

    def test_follower_names_a_runner_that_vanished(self) -> None:
        d = self.run_dir()
        (d / "status.json").write_text(json.dumps({"state": "running", "phase": "build"}))
        follower = self.v.Follower(d)
        with mock.patch.object(self.v, "unit_active", return_value=False):
            text = self.screen(follower)
        self.assertIn("━━ lost", text)
        with mock.patch.object(self.v, "unit_active", return_value=False):
            follower.next_unit_check = 0
            self.assertNotIn("lost", self.screen(follower), "said once")
        (d / "status.json").write_text(json.dumps({"state": "running", "phase": "build", "resumed": "t2"}))
        with mock.patch.object(self.v, "unit_active", return_value=True):
            follower.next_unit_check = 0
            self.assertIn("━━ resumed", self.screen(follower))
        self.assertFalse(follower.lost)


if __name__ == "__main__":
    unittest.main()
