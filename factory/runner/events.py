"""Append-only event log: one JSON object per line in runs/{id}/events.jsonl."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

NAMES = frozenset({
    "run.created",
    "worktree.created",
    "run.scoping",
    "run.queued",
    "slot.acquired",
    "stage.started",
    "process.spawned",
    "process.exited",
    "gate.evaluated",
    "stage.finished",
    "retry.scheduled",
    "slot.released",
    "run.needs_human",
    "run.cancel_requested",
    "run.cancelled",
    "run.resumed",
    "run.done",
    "notify.sent",
    "notify.failed",
    "gc.archived",
    # Supporting events beyond the required set.
    "scope.launched",
    "scope.gate_failed",
    "scope.committed",
    "stage.committed",
    "stage.warning",
    "run.retry_requested",
    "run.pause_requested",
    "run.paused",
    "run.unpaused",
    "run.note_added",
    "run.reattached",
    "intent.approved",
    "intent.drift",
    "config.changed",
    "skills.fallback",
    "browser.hosted",
    "browser.unavailable",
    "foreman.started",
    "foreman.decided",
    "foreman.fallback",
    "foreman.restarted",
    "foreman.rejected",
    "regate.scheduled",
    "publish.scheduled",
    "wait.scheduled",
    "wait.elapsed",
    "repair.scheduled",
    "override.recorded",
})


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append(run_dir: Path, run_id: str, event: str, stage: str | None = None,
           attempt: int | None = None, **data: object) -> dict:
    if event not in NAMES:
        raise ValueError(f"unknown event name: {event}")
    entry = {"ts": utc_now(), "run": run_id, "event": event, "stage": stage, "attempt": attempt, "data": data}
    line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
    fd = os.open(run_dir / "events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return entry


def read(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    entries = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return entries
