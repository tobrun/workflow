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
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "dev" / "skills" / "remote" / "scripts" / "remote.py"
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


def load_module():
    spec = importlib.util.spec_from_file_location("remote", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RemoteCycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name).resolve()
        self.t = t
        self.repo = t / "laptop" / "fixture"
        self.sources = t / "sources"
        self.vms = t / "vms"
        self.memory = t / "memory"
        for path in (self.repo, self.sources, self.vms / "ws-ws1" / "home", self.vms / "ws-ws2" / "home",
                     t / "home"):
            path.mkdir(parents=True)
        (t / "remote.toml").write_text(
            f'transport = "local"\nsources_root = "{self.sources}"\nworkspaces = ["ws1", "ws2"]\n')
        self.env = {**os.environ, **IDENTITY,
                    "HOME": str(t / "home"), "GIT_CONFIG_NOSYSTEM": "1",
                    "PATH": os.pathsep.join([str(FAKES), str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
                    "DEV_REMOTE_CONFIG": str(t / "remote.toml"), "DEV_REMOTE_HOME": str(t / "state"),
                    "DEV_REMOTE_TMP": str(t / "laptop-tmp"), "DEV_MEMORY_DIR": str(self.memory),
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

    def calls(self, ws: str = "ws1") -> list[dict]:
        path = self.vms / f"ws-{ws}" / "claude-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    # --- the cycle -----------------------------------------------------------

    def test_full_cycle_brings_commits_plan_reports_and_journal_home(self) -> None:
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

        entries = [json.loads(line) for f in (self.memory / "journal").glob("*.jsonl")
                   for line in f.read_text().splitlines()]
        self.assertEqual([e["skill"] for e in entries], ["build", "ship"])
        for entry in entries:
            self.assertEqual(entry["remote"]["run"], run)
            self.assertTrue(Path(entry["transcript"]).exists(), entry["transcript"])
            self.assertTrue(entry["transcript"].startswith(str(self.memory)))
            self.assertEqual(entry["signals"][0]["transcript"], entry["transcript"])
            ledger = Path(entry["ledger"]).read_text().splitlines()
            self.assertEqual(json.loads(ledger[entry["ledger_line"] - 1])["skill"], entry["skill"])
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

    def test_render_event_skips_noise_and_names_tools(self) -> None:
        r = self.m.render_event
        self.assertEqual(r(""), [])
        self.assertEqual(r(json.dumps({"type": "user", "message": {"content": "plain"}})), [])
        lines = r(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/a/b.py"}}]}}))
        self.assertEqual(lines, ["tool: Read /a/b.py"])

    def test_rewrite_paths_only_touches_prefixed_strings(self) -> None:
        out = self.m.rewrite_paths({"a": "/home/agent/x", "b": ["/home/agentx", 3], "c": {"d": "/home/agent/y"}},
                                   "/home/agent/", "/m/")
        self.assertEqual(out, {"a": "/m/x", "b": ["/home/agentx", 3], "c": {"d": "/m/y"}})

    def test_open_calls_reads_none_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pr = Path(tmp) / "pr.md"
            pr.write_text("## Summary\n\nx\n\n## Open calls\n\nnone\n")
            self.assertEqual(self.m.open_calls(pr), [])
            pr.write_text("## Open calls\n\n- guess at 60%\n- blocker\n\n## Other\n\nz\n")
            self.assertEqual(self.m.open_calls(pr), ["- guess at 60%", "- blocker"])
            pr.write_text("## Open calls\n\n- a concern whose line\nwraps twice\nbefore ending\n- second\n")
            self.assertEqual(self.m.open_calls(pr), ["- a concern whose line wraps twice before ending", "- second"])


if __name__ == "__main__":
    unittest.main()
