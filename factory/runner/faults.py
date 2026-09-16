"""Deterministic crash injection for recovery tests.

`FACTORY_FAULT=<point>` makes the process that reaches that point exit immediately with
status 86, as if killed. With `FACTORY_FAULT_ONCE=<path>` it fires only while that file
is absent and creates it, so the recovering process runs past the same point.
"""

from __future__ import annotations

import os
from pathlib import Path

EXIT_CODE = 86


def point(*names: str) -> None:
    """Crash here when FACTORY_FAULT names this point; pass a general and a specific name to allow either."""
    wanted = os.environ.get("FACTORY_FAULT")
    if not wanted or wanted not in names:
        return
    once = os.environ.get("FACTORY_FAULT_ONCE")
    if once:
        marker = Path(once)
        try:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return
        os.write(fd, wanted.encode("utf-8"))
        os.close(fd)
    os._exit(EXIT_CODE)
