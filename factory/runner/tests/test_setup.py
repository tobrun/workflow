"""The contract's setup runs in the run's own worktree, with writable package-manager caches."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from runner import cli, commands, watch
from runner.model import Run
from runner.tests.helpers import CONTRACT, FactoryTestCase, git, happy_scenario, make_repo
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


class CacheTests(unittest.TestCase):
    def test_workspace_write_points_package_caches_at_the_runner_and_keeps_declared_ones(self):
        env = {"PATH": os.environ["PATH"], "UV_CACHE_DIR": "/opt/uv-cache"}
        with unittest.mock.patch("runner.commands.cache_root", return_value=Path("/tmp/factory-cache-test")):
            additions, paths = commands.tool_caches(env, "workspace-write")
        self.assertEqual(additions["npm_config_cache"], "/tmp/factory-cache-test/npm")
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
