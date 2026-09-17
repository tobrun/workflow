"""The runner hosts the attempt's browser outside the agent's sandbox and hands it over as a CDP endpoint."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from runner import browser, commands, supervise
from runner.tests.helpers import STUBS

STUB = str(STUBS / "chrome")


class FindTests(unittest.TestCase):
    def test_the_override_wins_and_then_the_newest_downloaded_chrome(self):
        home = Path(tempfile.mkdtemp(prefix="fb-"))
        try:
            found = []
            for version in ("chrome-151.0.7922.71", "chrome-152.0.7977.64", "chrome-150.0.7871.24"):
                exe = (home / ".agent-browser" / "browsers" / version / "Google Chrome for Testing.app" / "Contents"
                       / "MacOS" / "Google Chrome for Testing")
                exe.parent.mkdir(parents=True)
                exe.write_text("#!/bin/sh\n")
                exe.chmod(0o755)
                found.append(exe)
            env = {"PATH": "/nonexistent"}
            self.assertEqual(browser.find(env, home), found[1])
            self.assertEqual(browser.find({**env, "FACTORY_BROWSER_BIN": STUB}, home), Path(STUB))
            with unittest.mock.patch("runner.browser._SYSTEM_CANDIDATES", ()):
                self.assertIsNone(browser.find(env, home / "empty"))
        finally:
            shutil.rmtree(home, ignore_errors=True)


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="fb-"))
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.root), "FACTORY_BROWSER_BIN": STUB}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def launch(self, **overrides) -> browser.Browser:
        values = dict(home=self.root / "home", holder="test:browser", port_range=(21000, 21999), mode="host",
                      directory=self.root / "attempt" / "browser", env=self.env)
        values.update(overrides)
        return browser.launch(**values)

    def test_the_browser_listens_on_a_leased_port_and_the_environment_points_at_it(self):
        hosted = self.launch()
        try:
            self.assertEqual(browser.version(hosted.port)["Browser"], "StubChrome/1.0")
            self.assertEqual(browser.environment(hosted),
                             {"AGENT_BROWSER_CDP": str(hosted.port), "FACTORY_BROWSER_CDP_URL": hosted.cdp_url})
            entry = browser.record(hosted)
            self.assertEqual((entry["status"], entry["port"], entry["pid"]), ("hosted", hosted.port, hosted.process.pid))
            self.assertTrue((hosted.directory / "profile" / "DevToolsActivePort").is_file())
            self.assertTrue((self.root / "home" / "resources" / "ports" / f"{hosted.port}.lock").is_file())
        finally:
            browser.stop(hosted)
        self.assertIsNotNone(hosted.process.poll())
        self.assertIsNone(browser.version(hosted.port))
        self.assertEqual(hosted.lease.fd, -1)

    def test_a_recorded_browser_a_dead_worker_left_behind_is_stopped_by_identity(self):
        hosted = self.launch()
        entry = browser.record(hosted)
        self.assertTrue(browser.stop_recorded(entry))
        deadline = time.monotonic() + 5
        while supervise.process_identity(entry["pid"])[0] and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(supervise.process_identity(entry["pid"])[0])
        hosted.lease.release()
        self.assertFalse(browser.stop_recorded(entry))
        self.assertFalse(browser.stop_recorded({"status": "unavailable"}))

    def test_no_browser_on_the_host_is_reported_with_the_fix(self):
        env = {"PATH": "/nonexistent", "HOME": str(self.root / "nowhere")}
        with unittest.mock.patch("runner.browser._SYSTEM_CANDIDATES", ()), self.assertRaises(browser.Unavailable) as caught:
            self.launch(env=env)
        self.assertIn("no Chrome or Chromium found", str(caught.exception))
        self.assertIn("FACTORY_BROWSER_BIN", str(caught.exception))
        self.assertEqual(list((self.root / "home" / "resources" / "ports").glob("*.lock")) if
                         (self.root / "home" / "resources" / "ports").exists() else [], [])

    def test_a_browser_that_exits_at_startup_is_reported_with_its_output(self):
        crashing = self.root / "crashing-chrome"
        crashing.write_text("#!/bin/sh\necho 'chrome: cannot start here' >&2\nexit 3\n")
        crashing.chmod(0o755)
        with self.assertRaises(browser.Unavailable) as caught:
            self.launch(env={**self.env, "FACTORY_BROWSER_BIN": str(crashing)})
        message = str(caught.exception)
        self.assertIn("exited 3 before it listened", message)
        self.assertIn("chrome: cannot start here", message)
        self.assertIn("[full output: ", message)

    def test_a_browser_that_never_listens_is_stopped_after_the_timeout(self):
        sleeper = self.root / "sleeper"
        sleeper.write_text("#!/bin/sh\nsleep 30\n")
        sleeper.chmod(0o755)
        with self.assertRaises(browser.Unavailable) as caught:
            self.launch(env={**self.env, "FACTORY_BROWSER_BIN": str(sleeper)}, ready_timeout_s=0.5)
        self.assertIn("did not listen", str(caught.exception))

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("agent-browser") and browser.find(dict(os.environ)),
                         "needs agent-browser and a real Chrome on macOS")
    def test_agent_browser_attaches_to_the_hosted_chrome_from_inside_a_sandboxed_command(self):
        env = {name: os.environ[name] for name in commands.BASELINE_ENV if name in os.environ}
        hosted = self.launch(env=env, mode="workspace-write")
        socket_dir = Path(tempfile.mkdtemp(prefix="ab-", dir="/tmp"))
        client = {**env, **browser.environment(hosted), "AGENT_BROWSER_SOCKET_DIR": str(socket_dir)}
        try:
            self.assertIn("webSocketDebuggerUrl", browser.version(hosted.port))
            open_argv = commands.sandbox_wrap(["agent-browser", "--session", "factory-hosted", "open", "about:blank",
                                               "--json"], "workspace-write", writable=[socket_dir])
            result = subprocess.run(open_argv, env=client, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('"browserLaunched":true', result.stdout)
            self.assertIn('"launched":false', result.stdout, "the session attached instead of launching its own Chrome")
            subprocess.run(["agent-browser", "--session", "factory-hosted", "close"], env=client, capture_output=True,
                           timeout=60)
            self.assertIsNotNone(browser.version(hosted.port), "closing a session must not end the hosted browser")
        finally:
            browser.stop(hosted)
            shutil.rmtree(socket_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
