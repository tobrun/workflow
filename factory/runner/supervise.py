"""Worker locking, liveness, and heartbeats. Guarded child executions live in runner/executor.py."""

from __future__ import annotations

import fcntl
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from runner.model import atomic_write

KILL_GRACE_SECONDS = 30


class WorkerLock:
    """Exclusive non-blocking flock on runs/{id}/worker.lock; released on process death."""

    def __init__(self, run_dir: Path):
        self.path = Path(run_dir) / "worker.lock"
        self.fd = -1

    def acquire(self) -> bool:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd >= 0:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "WorkerLock":
        if not self.acquire():
            raise LockHeld(self.path)
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class LockHeld(Exception):
    def __init__(self, path: Path):
        super().__init__(f"a live worker holds {path}")
        self.path = path


def worker_alive(run_dir: Path) -> bool:
    """Liveness is lock state; PID files are supporting information only."""
    path = Path(run_dir) / "worker.lock"
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def pid_alive(pid: int | None) -> bool:
    """A running (not zombie) process with this pid exists."""
    return process_identity(pid)[0]


def process_identity(pid: int | None) -> tuple[bool, str | None]:
    """(exists, start identity). The identity is None when the process exists but cannot be verified."""
    if not pid or pid <= 0:
        return False, None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, None
    except PermissionError:
        pass
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            fields = stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            if fields[0] in ("Z", "X"):
                return False, None
            return True, f"proc:{fields[19]}"
        except (OSError, IndexError):
            pass
    try:
        result = subprocess.run(["ps", "-o", "stat=,lstart=", "-p", str(pid)], capture_output=True, text=True,
                                timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return True, None
    parts = result.stdout.split(None, 1)
    if len(parts) != 2:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False, None
        except PermissionError:
            pass
        return True, None
    if parts[0].startswith("Z"):
        return False, None
    return True, "ps:" + " ".join(parts[1].split())


def read_pid(run_dir: Path) -> int | None:
    try:
        return int((Path(run_dir) / "worker.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def write_pid(run_dir: Path) -> None:
    atomic_write(Path(run_dir) / "worker.pid", f"{os.getpid()}\n")


def heartbeat_age(run_dir: Path) -> float | None:
    path = Path(run_dir) / "worker.heartbeat"
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


class Heartbeat:
    """Touch worker.heartbeat on an interval from a daemon thread."""

    def __init__(self, run_dir: Path, interval: float):
        self.path = Path(run_dir) / "worker.heartbeat"
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="factory-heartbeat", daemon=True)

    def beat(self) -> None:
        atomic_write(self.path, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.beat()
            except OSError:
                pass

    def __enter__(self) -> "Heartbeat":
        self.beat()
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=self.interval + 1)
