"""macOS notifications. Failures are recorded and never change workflow status."""

from __future__ import annotations

import os
import shutil
import subprocess

from runner.model import Run

SCRIPT = [
    "-e", "on run argv",
    "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
    "-e", "end run",
]


def send(run: Run, title: str, message: str, *, enabled: bool) -> bool:
    if not enabled:
        return False
    osascript = os.environ.get("FACTORY_OSASCRIPT_BIN") or shutil.which("osascript")
    if not osascript:
        run.event("notify.failed", error="osascript is not available", title=title)
        return False
    # Text travels as argv items, never interpolated into AppleScript source or a shell.
    try:
        result = subprocess.run([osascript, *SCRIPT, title[:200], message[:900]], capture_output=True, text=True,
                                timeout=15, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        run.event("notify.failed", error=str(error), title=title)
        return False
    if result.returncode != 0:
        run.event("notify.failed", error=result.stderr.strip() or f"exit {result.returncode}", title=title)
        return False
    run.event("notify.sent", title=title, message=message)
    return True


def for_status(run: Run, *, enabled: bool) -> None:
    status = run.status
    reason = run.data["human"].get("reason") or ""
    if status == "needs-human":
        send(run, f"Factory needs you: {run.id}", f"{run.stage}: {reason}", enabled=enabled)
    elif status == "cancelled":
        send(run, f"Factory cancelled: {run.id}", reason or "cancelled", enabled=enabled)
    elif status == "done":
        send(run, f"Factory done: {run.id}", run.data["pr"].get("url") or "pull request ready", enabled=enabled)
