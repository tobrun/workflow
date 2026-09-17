"""Operator controls shared by the CLI commands and the attached view.

While a worker is alive it is the only writer of run.json, so controls aimed at a
live run only leave intent files (`cancel`, `pause`, `note`) that the worker
applies at its next safe point. Controls that change a parked run (retry,
continue, resume) take the worker lock first, exactly like the worker would.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from runner import config, dashboard, executor, notify, supervise
from runner.model import (CANCELLED, DONE, NEEDS_HUMAN, PAUSED, QUEUED, RUNNING, SCOPING, BudgetExhausted, Run,
                          SchemaError, atomic_write)
from runner.worker import emit_pending, reconcile_orphan, spawn_worker


class ControlError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


@dataclass
class Outcome:
    run: Run
    lines: list[str] = field(default_factory=list)
    started: bool = False
    code: int = 0


def refresh(home: Path, cfg: config.Config) -> None:
    dashboard.safe_regenerate(home, heartbeat_seconds=cfg.heartbeat_seconds,
                              log=lambda message: print(message, file=sys.stderr))


def start_worker(home: Path, run: Run, timeout: float = 10) -> int:
    """Spawn the detached worker and wait until it holds the run lock, so the next read is not stale."""
    pid = spawn_worker(home, run)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not supervise.worker_alive(run.dir):
        if Run.load(run.dir).status in (DONE, NEEDS_HUMAN, CANCELLED, PAUSED):
            break
        time.sleep(0.02)
    return pid


def locked(run_dir: Path) -> tuple[Run, supervise.WorkerLock]:
    lock = supervise.WorkerLock(run_dir)
    if not lock.acquire():
        pid = supervise.read_pid(run_dir)
        raise ControlError(f"a live worker{f' (pid {pid})' if pid else ''} owns {run_dir.name}; "
                           f"`factory cancel {run_dir.name}` stops it")
    try:
        return Run.load(run_dir), lock
    except SchemaError as error:
        lock.release()
        raise ControlError(str(error), 2) from error


def execution_state(run: Run) -> str:
    """How a running run's open attempt stands without a worker: none, live, finished, or lost.

    Raises executor.Ambiguous when a recorded process may be ours but cannot be verified.
    """
    open_attempt = next((a for a in reversed(run.stage_attempts(run.stage)) if a.get("ended_at") is None), None)
    if open_attempt is None or not open_attempt.get("execution"):
        return "none"
    execution = open_attempt["execution"]
    if executor.live(run.dir):
        return "live"
    if executor.read_receipt(run.dir / execution["dir"], execution["token"]) is not None:
        return "finished"
    return "lost"


def settle_orphan(run: Run, cfg: config.Config) -> str:
    """Close a dead worker's open attempt that has nothing left to reattach to, and persist the transition."""
    action = reconcile_orphan(run, cfg)
    run.save()
    emit_pending(run)
    return action


def intent(run_dir: Path, name: str, text: str | None = None) -> None:
    atomic_write(run_dir / name, (text if text is not None else datetime.now().astimezone().isoformat()) + "\n")


# --- controls for a live or parked run ---------------------------------------------

def add_note(run_dir: Path, note: str) -> Outcome:
    """Leave a note for the next attempt; the worker folds it into run state before launching."""
    note = note.strip()
    if not note:
        raise ControlError("the note is empty", 2)
    run = Run.load(run_dir)
    intent(run_dir, "note", note)
    run.event("run.note_added", run.stage, None, note=note, pending=True)
    return Outcome(run, [f"Note saved for the next {run.stage} attempt: {note}"])


def pending_note(run: Run) -> str | None:
    path = run.dir / "note"
    return path.read_text(encoding="utf-8").strip() or None if path.exists() else None


def request_pause(home: Path, cfg: config.Config, run_dir: Path) -> Outcome:
    run = Run.load(run_dir)
    if run.status not in (QUEUED, RUNNING):
        raise ControlError(f"{run.id} is {run.status}; only queued or running runs can pause")
    intent(run_dir, "pause")
    run.event("run.pause_requested", run.stage, None)
    if supervise.worker_alive(run_dir):
        when = "after the current attempt" if run.status == RUNNING else "before the next attempt starts"
        return Outcome(run, [f"Pause requested for {run.id}; it pauses {when}."])
    run, lock = locked(run_dir)
    reattach = False
    try:
        if run.status == RUNNING:
            try:
                state = execution_state(run)
            except executor.Ambiguous as error:
                raise ControlError(f"pause requested, but {error}") from error
            if state in ("live", "finished"):
                reattach = True
            else:
                settle_orphan(run, cfg)
        if run.status == QUEUED and not reattach:
            run.transition("pause")
            run.save()
            run.event("run.paused", run.stage, None)
        refresh(home, cfg)
    finally:
        lock.release()
    if reattach:
        pid = start_worker(home, run)
        return Outcome(run, [f"Pause requested for {run.id}; worker {pid} reattached to its current attempt and "
                             "pauses after it."], started=True)
    return Outcome(run, [f"{run.id} is {run.status} at {run.stage}."])


def withdraw_pause(run_dir: Path) -> Outcome:
    run = Run.load(run_dir)
    path = run_dir / "pause"
    if run.status == PAUSED:
        raise ControlError(f"{run.id} is already paused; continue it with `factory resume {run.id}`")
    if path.exists():
        path.unlink()
        return Outcome(run, [f"Pause withdrawn for {run.id}; it keeps going."])
    return Outcome(run, [f"No pause was pending for {run.id}."])


def cancel(home: Path, cfg: config.Config, run_dir: Path) -> Outcome:
    run = Run.load(run_dir)
    if run.status in (DONE, CANCELLED):
        return Outcome(run, [f"{run.id} is already {run.status}; nothing to cancel."])
    intent(run_dir, "cancel")
    run.event("run.cancel_requested", run.stage, None)
    if supervise.worker_alive(run_dir):
        return Outcome(run, [f"Cancellation requested for {run.id}; the worker will stop its current process."])
    lock = supervise.WorkerLock(run_dir)
    if not lock.acquire():
        return Outcome(run, [f"Cancellation requested for {run.id}; a worker picked the run up and will stop."])
    try:
        try:
            stopped = executor.stop_live(run_dir, grace=supervise.KILL_GRACE_SECONDS)
        except executor.Ambiguous as error:
            return Outcome(run, [f"Cancellation requested for {run.id}, but {error}"], code=1)
        run = Run.load(run_dir)
        if run.status == RUNNING:
            settle_orphan(run, cfg)
        if run.status not in (CANCELLED, DONE):
            run.transition("cancel", reason="cancelled by operator")
            run.save()
            run.event("run.cancelled", run.stage, None, reason="cancelled by operator",
                      executor=stopped)
        run.save()
        refresh(home, cfg)
        notify.for_status(run, enabled=cfg.notify)
    finally:
        lock.release()
    return Outcome(run, [f"Cancelled {run.id} at {run.stage}; worktree and artifacts are kept.",
                         f"> factory retry {run.id}"])


def retry(home: Path, cfg: config.Config, run_dir: Path, *, note: str | None = None,
          reset_budget: bool = False, rescope: bool = False) -> Outcome:
    run, lock = locked(run_dir)
    try:
        if run.status not in (NEEDS_HUMAN, CANCELLED):
            raise ControlError(f"{run.id} is {run.status}; only needs-human or cancelled runs can be retried")
        if executor.live(run_dir):
            raise ControlError(f"an execution of {run.id} is still live; `factory cancel {run.id}` stops it first")
        if note:
            run.data["human"]["note"] = note
        if reset_budget:
            run.data["retries"]["used"] = 0
        if cfg.max_retries < run.data["retries"]["budget"]:
            run.data["retries"]["budget"] = cfg.max_retries
        for flag in ("cancel", "pause"):
            (run.dir / flag).unlink(missing_ok=True)
        if rescope and run.stage != "scope":
            run.data["stage"] = "scope"
        if run.stage == "scope":
            if not run.worktree.is_dir():
                raise ControlError(f"{run.id} never got a worktree; remove it with `factory rm {run.id} --force` "
                                   "and start a new run")
            run.transition("reopen_scope")
            run.save()
            run.event("run.retry_requested", "scope", None, note=note)
            run.event("run.scoping", "scope", None)
            refresh(home, cfg)
            return Outcome(run, [f"Reopened scope for {run.id}.", f"> factory scope {run.id} --resume"])
        if run.retry_needed():
            try:
                run.consume_retry()
            except BudgetExhausted as error:
                raise ControlError(f"{error}; pass --reset-budget to allow another attempt") from error
        run.transition("operator_retry")
        run.save()
        run.event("run.retry_requested", run.stage, None, note=note, reset_budget=reset_budget,
                  used=run.data["retries"]["used"])
        run.event("run.queued", run.stage, run.stage_record(run.stage)["attempts"] + 1)
        refresh(home, cfg)
    finally:
        lock.release()
    pid = start_worker(home, run)
    n = run.stage_record(run.stage)["attempts"] + 1
    return Outcome(run, [f"Queued {run.id} at {run.stage} attempt {n}; worker {pid} started.",
                         f"retries {run.data['retries']['used']}/{run.data['retries']['budget']}"], started=True)


def resume(home: Path, cfg: config.Config, run_dir: Path) -> Outcome:
    """Continue a paused run, or recover a queued or running run whose worker died."""
    if supervise.worker_alive(run_dir):
        pid = supervise.read_pid(run_dir)
        raise ControlError(f"worker{f' {pid}' if pid else ''} is alive for {run_dir.name}; nothing to resume")
    run, lock = locked(run_dir)
    lines: list[str] = []
    try:
        if run.status == PAUSED:
            (run.dir / "pause").unlink(missing_ok=True)
            run.transition("unpause")
            run.save()
            run.event("run.unpaused", run.stage, None)
            action = "unpaused"
        elif run.status in (QUEUED, RUNNING):
            (run.dir / "pause").unlink(missing_ok=True)
            pid = supervise.read_pid(run.dir)
            if pid and pid != os.getpid() and supervise.pid_alive(pid):
                lines.append(f"note: pid {pid} is alive but does not hold the worker lock; it is not this run's "
                             "worker and was left untouched")
            state = "none"
            if run.status == RUNNING:
                try:
                    state = execution_state(run)
                except executor.Ambiguous as error:
                    raise ControlError(f"not resumed: {error}") from error
            if state == "live":
                action = "reattached"
                lines.append(f"The {run.stage} execution is still running; the new worker reattaches to it instead "
                             "of starting another.")
            elif state == "finished":
                action = "reattached"
                lines.append(f"The {run.stage} execution finished while no worker was alive; the new worker gates "
                             "its result.")
            else:
                action = settle_orphan(run, cfg)
            run.save()
            run.event("run.resumed", run.stage, None, action=action)
        elif run.status == SCOPING:
            raise ControlError(f"{run.id} is still in interactive scope, which resume does not run; continue it with "
                               f"`factory scope {run.id} --resume`")
        else:
            raise ControlError(f"{run.id} is {run.status}; resume continues paused runs and recovers queued or "
                               f"running ones (see `factory show {run.id}`)")
        refresh(home, cfg)
        if run.status != QUEUED and action != "reattached":
            notify.for_status(run, enabled=cfg.notify)
            return Outcome(run, lines + [f"{run.id} is {run.status} at {run.stage}: {run.data['human'].get('reason')}"],
                           code=1)
    finally:
        lock.release()
    pid = start_worker(home, run)
    verb = "Continued" if action == "unpaused" else "Resumed"
    detail = " (orphaned attempt recorded as failed)" if action not in ("none", "requeued", "unpaused",
                                                                         "reattached") else ""
    lines += [f"{verb} {run.id} at {run.stage}; worker {pid} started{detail}.",
              f"retries {run.data['retries']['used']}/{run.data['retries']['budget']}"]
    return Outcome(run, lines, started=True)
