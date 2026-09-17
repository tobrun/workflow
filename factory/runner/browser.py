"""A runner-hosted headless browser per attempt, reached over the Chrome DevTools Protocol.

Chrome cannot start inside the Codex sandbox. Its seatbelt policy is closed by default and
denies the Mach lookups a Chrome process makes at startup (launchservicesd, windowserver,
tccd, distributed notifications), so a browser the agent launches exits before it writes
DevToolsActivePort, whatever `--no-sandbox` says. The runner therefore starts one headless
Chrome outside that sandbox, confined by its own write-only boundary (`commands.sandbox_wrap`)
to the attempt's browser directory and the temporary directories, listening on a leased
loopback port, and hands that port to the agent and to the gate's e2e driver:

  AGENT_BROWSER_CDP        every `agent-browser` session attaches to the hosted browser
  FACTORY_BROWSER_CDP_URL  the http endpoint for Playwright's `chromium.connectOverCDP`

Closing a session leaves the browser running; the runner stops it when the attempt ends.
`FACTORY_BROWSER_BIN` names the executable outright (tests use it for a stub); otherwise the
newest Chrome the host already has is used, in the order `candidates` documents.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from runner import commands, resources, slots, supervise

READY_TIMEOUT_S = 30
MODES = ("auto", "off")

# Preference order: the browser agent-browser downloaded for itself, Playwright's Chromium, a
# system Chrome or Chromium, then whatever is on PATH. Headless shells are skipped: they take
# different flags and render differently from the browser a user would run.
_MAC = sys.platform == "darwin"
_HOME_CANDIDATES = (
    ".agent-browser/browsers/chrome-*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    ".agent-browser/browsers/chrome-*/chrome-linux*/chrome",
    ".agent-browser/browsers/chrome-*/chrome",
    "Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
    ".cache/ms-playwright/chromium-*/chrome-linux*/chrome",
)
_SYSTEM_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)
_PATH_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")


class Unavailable(Exception):
    """No browser could be hosted; the message says why and what would fix it."""


@dataclass
class Browser:
    executable: Path
    port: int
    process: subprocess.Popen
    lease: slots.Slot
    directory: Path
    log: Path
    identity: str | None

    @property
    def cdp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _version_key(home: Path, path: str) -> tuple:
    """Numbers in the path below `home`, so `chrome-152.0.7977.64` sorts above `chrome-151.0.7922.71`."""
    return tuple(int(n) for n in re.findall(r"\d+", os.path.relpath(path, home)))


def candidates(env: dict, home: Path | None = None) -> list[Path]:
    """Executables that could serve as the hosted browser, most preferred first."""
    found: list[Path] = []
    for name in ("FACTORY_BROWSER_BIN", "AGENT_BROWSER_EXECUTABLE_PATH"):
        if env.get(name):
            found.append(Path(env[name]))
    home = Path(home if home is not None else env.get("HOME") or Path.home())
    for pattern in _HOME_CANDIDATES:
        matches = sorted(glob.glob(str(home / pattern)), key=lambda match: _version_key(home, match), reverse=True)
        found.extend(Path(match) for match in matches)
    found.extend(Path(path) for path in _SYSTEM_CANDIDATES)
    for name in _PATH_NAMES:
        located = shutil.which(name, path=env.get("PATH"))
        if located:
            found.append(Path(located))
    return found


def find(env: dict, home: Path | None = None) -> Path | None:
    """The browser executable the runner would host, or None when the host has none."""
    for candidate in candidates(env, home):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def argv(executable: Path, port: int, directory: Path) -> list[str]:
    """Headless Chrome with remote debugging on a loopback port and a throwaway profile.

    `--no-sandbox` because Chrome's own sandbox cannot nest inside the runner's boundary; the
    boundary still confines every write to `directory` and the temporary directories.
    """
    return [
        str(executable), "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
        "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
        "--disable-component-update", "--disable-sync", "--window-size=1280,800",
        f"--remote-debugging-port={port}", f"--user-data-dir={directory / 'profile'}", "about:blank",
    ]


def version(port: int, timeout: float = 1.0) -> dict | None:
    """Chrome's /json/version document while the browser answers on `port`, else None."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def launch(*, home: Path, holder: str, port_range: tuple, mode: str, directory: Path, env: dict,
           ready_timeout_s: float = READY_TIMEOUT_S) -> Browser:
    """Start the hosted browser for one attempt or gate; raises Unavailable with the reason.

    `mode` is the execution boundary that confines the browser (`workspace-write` or `host`);
    `directory` receives its profile and `chrome.log` and is the only path it may write outside
    the temporary directories.
    """
    executable = find(env)
    if executable is None:
        raise Unavailable("no Chrome or Chromium found on this host; install one with `agent-browser install` or "
                          "`npx playwright install chromium`, or set FACTORY_BROWSER_BIN")
    directory = Path(directory)
    (directory / "profile").mkdir(parents=True, exist_ok=True)
    log = directory / "chrome.log"
    port, lease = resources.acquire_port(home, holder, *port_range)
    try:
        parts = commands.sandbox_wrap(argv(executable, port, directory), mode, writable=[directory])
        base = {name: env[name] for name in commands.BASELINE_ENV if name in env}
        with open(log, "ab") as handle:
            process = subprocess.Popen(parts, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                                       start_new_session=True, env=base, cwd=str(directory))
    except (commands.CommandError, OSError) as error:
        lease.release()
        raise Unavailable(f"the hosted browser {executable} could not start: {error}") from error
    _, identity = supervise.process_identity(process.pid)
    browser = Browser(executable=executable, port=port, process=process, lease=lease, directory=directory, log=log,
                      identity=identity)
    deadline = time.monotonic() + ready_timeout_s
    while True:
        if version(port) is not None:
            return browser
        if process.poll() is not None:
            stop(browser)
            raise Unavailable(f"the hosted browser {executable.name} exited {process.returncode} before it listened "
                              f"on 127.0.0.1:{port}: {_tail(log)} [full output: {log}]")
        if time.monotonic() >= deadline:
            stop(browser)
            raise Unavailable(f"the hosted browser {executable.name} did not listen on 127.0.0.1:{port} within "
                              f"{ready_timeout_s:g}s: {_tail(log)} [full output: {log}]")
        time.sleep(0.1)


def environment(browser: Browser | None) -> dict:
    """What agents, gate drivers, and services receive so their browser automation attaches to the host's."""
    if browser is None:
        return {}
    # AGENT_BROWSER_ARGS matches how this browser was launched, so a driver that reads it in any boundary sees it.
    return {"AGENT_BROWSER_CDP": str(browser.port), "FACTORY_BROWSER_CDP_URL": browser.cdp_url,
            "AGENT_BROWSER_ARGS": "--no-sandbox"}


def record(browser: Browser) -> dict:
    """The attempt-record entry: enough to reach, audit, and later stop the browser."""
    return {"status": "hosted", "executable": str(browser.executable), "port": browser.port, "cdp_url": browser.cdp_url,
            "pid": browser.process.pid, "identity": browser.identity, "log": str(browser.log)}


def stop(browser: Browser | None, grace: float = 5.0) -> None:
    """End the hosted browser's process group and release its port."""
    if browser is None:
        return
    try:
        _terminate(browser.process.pid, browser.process.poll, browser.process.wait, grace)
    finally:
        browser.lease.release()


def stop_recorded(entry: dict | None, grace: float = 5.0) -> bool:
    """Stop a browser a dead worker left behind, identified by its recorded pid and start identity."""
    if not entry or entry.get("status") != "hosted" or not entry.get("pid"):
        return False
    alive, identity = supervise.process_identity(entry["pid"])
    if not alive or (entry.get("identity") and identity and identity != entry["identity"]):
        return False

    def poll() -> int | None:
        return None if supervise.process_identity(entry["pid"])[0] else 0

    def wait(timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(entry["pid"], timeout)
            time.sleep(0.05)

    _terminate(entry["pid"], poll, wait, grace)
    return True


def _terminate(pid: int, poll, wait, grace: float) -> None:
    if poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            os.kill(pid, sig)
        try:
            wait(grace)
            return
        except subprocess.TimeoutExpired:
            continue


def _tail(log: Path, lines: int = 3) -> str:
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "; ".join(line.strip() for line in text.splitlines()[-lines:] if line.strip())
