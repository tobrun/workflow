"""The contract's setup runs in the run's own worktree, with writable package-manager caches."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from runner import commands, supervise, watch
from runner import worktree as wt
from runner.tests.helpers import CONTRACT, STUBS, FactoryTestCase, git, happy_scenario, make_repo
from runner.worker import Worker

INSTALL = ["sh", "-c", "mkdir -p __pycache__ && echo installed >> __pycache__/installs"]


class SetupTests(FactoryTestCase):
    def repo_with(self, contract: dict, name: str) -> Path:
        repo = make_repo(self.root, name)
        (repo / ".factory" / "contract.json").write_text(json.dumps(contract))
        git(repo, "commit", "--quiet", "-am", "contract")
        git(repo, "push", "--quiet")
        return repo

    def test_setup_runs_once_before_build_and_is_reused_while_dependencies_are_unchanged(self):
        self.scenario(happy_scenario())
        run = self.queued_run(repo=self.repo_with({**CONTRACT, "setup": [{"id": "deps", "run": INSTALL}]}, "setup"))
        Worker(self.home, run.id, grace=1).run()
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "done", data["human"])
        self.assertEqual((run.worktree / "__pycache__" / "installs").read_text(), "installed\n")
        build, ship = (next(a for a in data["attempts"] if a["stage"] == stage) for stage in ("build", "ship"))
        self.assertEqual([d["decision"] for d in build["setup"]["decisions"]], ["computed: no checkpoint"])
        self.assertEqual(build["setup"]["commands"][0]["classification"], "succeeded")
        self.assertTrue(ship["setup"]["decisions"][0]["decision"].startswith("reused"))

    def test_setup_reruns_when_an_installed_output_disappears_and_hides_it_from_git_status(self):
        scenario = happy_scenario()
        # The build agent "tidies" the worktree by removing what setup installed, as one real run did to node_modules.
        scenario["build"][0]["run"].insert(0, ["rm", "-rf", "__pycache__"])
        self.scenario(scenario)
        contract = {**CONTRACT, "setup": [{"id": "deps", "run": INSTALL, "produces": ["__pycache__"]}]}
        run = self.queued_run(repo=self.repo_with(contract, "vanishing"))
        Worker(self.home, run.id, grace=1).run()
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "done", data["human"])
        build, ship = (next(a for a in data["attempts"] if a["stage"] == stage) for stage in ("build", "ship"))
        self.assertEqual(build["setup"]["produces"], [str(run.worktree / "__pycache__")])
        # The build gate runs setup again, finds the install gone, and reinstalls; ship then reuses that.
        self.assertEqual([d["decision"] for d in build["checkpoints"] if d["subphase"] == "setup"],
                         ["computed: setup output __pycache__ is missing"])
        self.assertTrue(ship["setup"]["decisions"][0]["decision"].startswith("reused"))
        self.assertEqual((run.worktree / "__pycache__" / "installs").read_text(), "installed\n")
        excludes = (wt.common_dir(run.worktree) / "info" / "exclude").read_text()
        self.assertIn("/__pycache__/", excludes.splitlines())
        self.assertEqual(wt.changed_paths(run.worktree), [])

    def test_a_declared_output_that_setup_does_not_produce_only_warns(self):
        self.scenario(happy_scenario())
        contract = {**CONTRACT, "setup": [{"id": "deps", "run": ["true"], "produces": ["node_modules"]}]}
        run = self.queued_run(stage="build", repo=self.repo_with(contract, "undeclared"))
        Worker(self.home, run.id, grace=1).run()
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "done", data["human"])
        build = next(a for a in data["attempts"] if a["stage"] == "build")
        self.assertEqual(build["setup"]["warnings"],
                         ["setup command deps succeeded but did not produce node_modules, which its contract record declares"])
        self.assertIn("did not produce node_modules", build["warning"])
        self.assertIn('"event": "stage.warning"', (run.dir / "events.jsonl").read_text())

    def test_a_failing_setup_parks_before_the_agent_starts_and_names_the_full_output(self):
        failing = ["sh", "-c", "echo resolving; echo npm error code EPERM >&2; exit 1"]
        self.scenario(happy_scenario())
        run = self.queued_run(stage="build",
                              repo=self.repo_with({**CONTRACT, "setup": [{"id": "deps", "run": failing}]}, "broken"))
        Worker(self.home, run.id, grace=1).run()
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "needs-human")
        reason = data["human"]["reason"]
        self.assertIn("before launching build: Setup command deps (sh -c 'echo resolving; echo npm error code EPERM "
                      ">&2; exit 1') exited 1 in the host boundary: resolving; npm error code EPERM", reason)
        logs = Path(reason.split("[full output: ")[1].split("/stdout.log")[0])
        self.assertIn("npm error code EPERM", (logs / "stderr.log").read_text())
        self.assertEqual([c for c in self.stub_calls("codex") if c["argv"][0] == "exec"
                          and c["env"]["FACTORY_STAGE"] == "build"], [])
        self.assertEqual(data["attempts"][-1]["code"], "setup.failed")


class ProducesTests(unittest.TestCase):
    def test_package_manager_installs_imply_their_output_directory(self):
        root = Path(os.path.realpath(tempfile.mkdtemp(prefix="fp-")))
        try:
            (root / "ui").mkdir()
            self.assertEqual(commands.produces({"run": ["npm", "ci"], "cwd": "ui"}, root), ([root / "ui" / "node_modules"], False))
            self.assertEqual(commands.produces({"run": ["yarn"]}, root), ([root / "node_modules"], False))
            self.assertEqual(commands.produces({"run": ["uv", "sync", "--frozen"]}, root), ([root / ".venv"], False))
            self.assertEqual(commands.produces({"run": ["uv", "sync"], "set": {"UV_PROJECT_ENVIRONMENT": "/x"}}, root),
                             ([], False))
            self.assertEqual(commands.produces({"run": ["pip", "install", "-e", "."]}, root), ([], False))
            self.assertEqual(commands.produces({"run": ["make", "deps"], "produces": ["vendor", "ui/dist"]}, root),
                             ([root / "vendor", root / "ui" / "dist"], True))
            with self.assertRaises(commands.CommandError):
                commands.produces({"run": ["make"], "produces": ["../outside"]}, root)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class CacheTests(unittest.TestCase):
    def test_workspace_write_points_package_caches_at_the_runner_and_keeps_declared_ones(self):
        env = {"PATH": os.environ["PATH"], "UV_CACHE_DIR": "/opt/uv-cache"}
        with unittest.mock.patch("runner.commands.cache_root", return_value=Path("/tmp/factory-cache-test")):
            additions, paths = commands.tool_caches(env, "workspace-write")
        self.assertEqual(additions["npm_config_cache"], "/tmp/factory-cache-test/npm")
        # Bun keeps its install cache and the temp files beside it under ~/.bun, which the boundary denies.
        self.assertEqual(additions["BUN_INSTALL_CACHE_DIR"], "/tmp/factory-cache-test/bun")
        self.assertEqual(additions["AGENT_BROWSER_SOCKET_DIR"], "/tmp/factory-cache-test/agent-browser")
        self.assertEqual(additions["AGENT_BROWSER_ARGS"], "--no-sandbox")
        self.assertNotIn("UV_CACHE_DIR", additions)
        self.assertIn(Path("/opt/uv-cache"), paths)
        self.assertEqual(commands.tool_caches(env, "host"), ({}, []))
        shutil.rmtree("/tmp/factory-cache-test", ignore_errors=True)

    @unittest.skipUnless(sys.platform == "darwin" or shutil.which("bwrap"), "no sandbox enforcement here")
    def test_a_sandboxed_command_can_write_its_package_cache_but_not_the_home_directory(self):
        home = Path(os.path.realpath(os.path.expanduser("~")))
        cache = home / ".factory-cache-probe"
        blocked = home / ".factory-sandbox-probe"
        try:
            with unittest.mock.patch("runner.commands.cache_root", return_value=cache):
                argv, env = commands.confine(
                    ["sh", "-c", 'echo ok > "$npm_config_cache/probe" && echo no > "$HOME/.factory-sandbox-probe"'],
                    "workspace-write", writable=[], env={"PATH": os.environ["PATH"], "HOME": str(home)})
            result = subprocess.run(argv, env=env, capture_output=True, text=True)
            self.assertEqual((cache / "npm" / "probe").read_text(), "ok\n")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(blocked.exists())
        finally:
            shutil.rmtree(cache, ignore_errors=True)
            blocked.unlink(missing_ok=True)


    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("agent-browser"), "needs agent-browser on macOS")
    def test_agent_browser_launches_chrome_inside_the_sandbox_with_the_defaults(self):
        cache = Path(tempfile.mkdtemp(prefix="fc-"))
        try:
            with unittest.mock.patch("runner.commands.cache_root", return_value=cache):
                base = {"PATH": os.environ["PATH"], "HOME": os.path.expanduser("~"), "TMPDIR": tempfile.gettempdir()}
                argv, env = commands.confine(["agent-browser", "--session", "factory-test", "open", "about:blank"],
                                             "workspace-write", writable=[], env=base)
                result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
                close, close_env = commands.confine(["agent-browser", "--session", "factory-test", "close"],
                                                    "workspace-write", writable=[], env=base)
                subprocess.run(close, env=close_env, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finally:
            shutil.rmtree(cache, ignore_errors=True)


class AgentEnvironmentTests(FactoryTestCase):
    def test_codex_attempts_get_writable_caches_and_browser_settings(self):
        self.scenario(happy_scenario())
        run = self.queued_run()
        Worker(self.home, run.id, grace=1).run()
        build = next(c for c in self.stub_calls("codex") if c["argv"][0] == "exec" and c["env"]["FACTORY_STAGE"] == "build")
        cache = self.home / "cache"
        self.assertEqual(build["env"]["AGENT_BROWSER_ARGS"], "--no-sandbox")
        self.assertEqual(build["env"]["npm_config_cache"], str(cache / "npm"))
        self.assertIn(str(cache), build["argv"])


class HostedBrowserTests(FactoryTestCase):
    fast_config = {**FactoryTestCase.fast_config, "browser": "auto"}

    def test_build_and_ship_attempts_get_a_hosted_browser_that_ends_with_the_attempt(self):
        self.scenario(happy_scenario())
        run = self.queued_run()
        with unittest.mock.patch.dict(os.environ, {"FACTORY_BROWSER_BIN": str(STUBS / "chrome")}):
            Worker(self.home, run.id, grace=1).run()
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "done", data["human"])
        for stage in ("build", "ship"):
            attempt = next(a for a in data["attempts"] if a["stage"] == stage)
            call = next(c for c in self.stub_calls("codex") if c["argv"][0] == "exec" and c["env"]["FACTORY_STAGE"] == stage)
            port = attempt["browser"]["port"]
            self.assertEqual(attempt["browser"]["status"], "hosted")
            self.assertEqual(call["env"]["AGENT_BROWSER_CDP"], str(port))
            self.assertEqual(call["env"]["FACTORY_BROWSER_CDP_URL"], f"http://127.0.0.1:{port}")
            launched = json.loads((Path(attempt["browser"]["log"]).parent / "profile" / "stub.json").read_text())
            self.assertIn(f"--remote-debugging-port={port}", launched["argv"])
            self.assertEqual(launched["pid"], attempt["browser"]["pid"])
            self.assertFalse(supervise.process_identity(attempt["browser"]["pid"])[0], "the browser outlived the attempt")
            self.assertTrue(Path(attempt["browser"]["log"]).is_file())
        self.assertFalse((run.attempt_dir("scope-review", 1) / "browser").exists(), "no browser for scope-review")
        review = next(a for a in data["attempts"] if a["stage"] == "scope-review")
        self.assertEqual(review.get("browser"), {"status": "off"})
        events = (run.dir / "events.jsonl").read_text()
        self.assertIn('"event": "browser.hosted"', events)

    def test_a_host_without_a_browser_records_why_and_the_attempt_still_runs(self):
        self.scenario(happy_scenario())
        run = self.queued_run(stage="build")
        with unittest.mock.patch("runner.browser.find", return_value=None):
            Worker(self.home, run.id, grace=1).run()
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "done", data["human"])
        build = next(a for a in data["attempts"] if a["stage"] == "build")
        self.assertEqual(build["browser"]["status"], "unavailable")
        self.assertIn("no Chrome or Chromium found", build["browser"]["reason"])
        call = next(c for c in self.stub_calls("codex") if c["argv"][0] == "exec" and c["env"]["FACTORY_STAGE"] == "build")
        self.assertNotIn("AGENT_BROWSER_CDP", call["env"])


class BlockerTests(FactoryTestCase):
    def test_the_watch_view_wraps_a_long_blocker_instead_of_cutting_it(self):
        run = self.queued_run(stage="build")
        reason = "launch.unavailable (C1): the browser could not launch; " + "detail " * 30 + "END-OF-REASON"
        run.data["status"] = "needs-human"
        run.data["human"]["reason"] = reason
        lines = watch.status_block(run, watch.Style(color=False), watch.StreamTail(), 0, False, 60)
        blocker = [line.strip() for line in lines if "detail" in line or "launch.unavailable" in line]
        self.assertGreater(len(blocker), 1)
        self.assertTrue(all(len(line) <= 60 for line in lines))
        self.assertEqual(" ".join(blocker), reason)


if __name__ == "__main__":
    unittest.main()
