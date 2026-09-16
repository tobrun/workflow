"""Cross-process semaphore for expensive headless stages: one flock file per slot."""

from __future__ import annotations

import fcntl
import hashlib
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class Slot:
    index: int
    fd: int
    path: Path

    def release(self) -> None:
        if self.fd < 0:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = -1


def scan_order(run_id: str, count: int) -> list[int]:
    """Rotating order derived from the run id, so slot 0 is not always favored."""
    start = int(hashlib.sha256(run_id.encode("utf-8")).hexdigest(), 16) % count
    return [(start + offset) % count for offset in range(count)]


def try_acquire(slots_dir: Path, run_id: str, count: int) -> Slot | None:
    slots_dir.mkdir(parents=True, exist_ok=True)
    for index in scan_order(run_id, count):
        path = slots_dir / f"{index}.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        os.ftruncate(fd, 0)
        os.write(fd, f"{run_id} {os.getpid()}\n".encode("utf-8"))
        return Slot(index, fd, path)
    return None


def acquire(slots_dir: Path, run_id: str, *, count: Callable[[], int], poll_seconds: float,
            should_abort: Callable[[], bool], on_wait: Callable[[], None] = lambda: None,
            sleep: Callable[[float], None] = time.sleep) -> Slot | None:
    """Block until a slot is free; None when should_abort turns true while queued.

    `count` is re-read on every scan so a changed max_concurrent_stages affects new
    acquisitions while existing holders finish normally.
    """
    while True:
        if should_abort():
            return None
        slot = try_acquire(slots_dir, run_id, max(1, count()))
        if slot is not None:
            return slot
        on_wait()
        sleep(poll_seconds + random.uniform(0, min(1.0, poll_seconds / 10)))


def holders(slots_dir: Path) -> dict[int, str | None]:
    """Which slot files are currently locked, with the recorded holder."""
    result: dict[int, str | None] = {}
    if not slots_dir.is_dir():
        return result
    for path in sorted(slots_dir.glob("*.lock")):
        try:
            index = int(path.stem)
        except ValueError:
            continue
        fd = os.open(path, os.O_RDONLY)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                result[index] = path.read_text(encoding="utf-8").strip() or None
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    return result


def prune(slots_dir: Path, count: int) -> list[int]:
    """Remove unlocked slot files at or above count."""
    removed = []
    for path in sorted(slots_dir.glob("*.lock")):
        try:
            index = int(path.stem)
        except ValueError:
            continue
        if index < count:
            continue
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        path.unlink()
        os.close(fd)
        removed.append(index)
    return removed
