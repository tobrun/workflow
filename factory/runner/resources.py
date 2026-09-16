"""Host-wide leases for runner-executed work: heavy commands and network ports.

Leases are flock files under `~/.factory/resources/`, like stage slots. The process that
runs the work (the executor's guard) inherits the lease's file descriptor, so a lease stays
held while that work runs even if the worker that took it dies, and is released only when
every holder has exited.
"""

from __future__ import annotations

import fcntl
import os
import socket
import time
from pathlib import Path
from typing import Callable

from runner import slots


def acquire(home: Path, kind: str, holder: str, *, count: int, should_abort: Callable[[], bool],
            poll_seconds: float = 0.2, sleep: Callable[[float], None] = time.sleep) -> slots.Slot | None:
    """Block for one of `count` leases of `kind`; None when should_abort turns true first."""
    return slots.acquire(Path(home) / "resources" / kind, holder, count=lambda: count, poll_seconds=poll_seconds,
                         should_abort=should_abort, sleep=sleep)


def holders(home: Path, kind: str) -> dict[int, str | None]:
    return slots.holders(Path(home) / "resources" / kind)


def acquire_port(home: Path, holder: str, low: int, high: int) -> tuple[int, slots.Slot]:
    """A port no other factory execution holds and nothing on this host is listening on."""
    directory = Path(home) / "resources" / "ports"
    directory.mkdir(parents=True, exist_ok=True)
    start = low + (os.getpid() % max(1, high - low))
    for offset in range(high - low + 1):
        port = low + (start - low + offset) % (high - low + 1)
        path = directory / f"{port}.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
                continue
        os.ftruncate(fd, 0)
        os.write(fd, f"{holder} {os.getpid()}\n".encode("utf-8"))
        return port, slots.Slot(port, fd, path)
    raise RuntimeError(f"no free port in {low}-{high}")
