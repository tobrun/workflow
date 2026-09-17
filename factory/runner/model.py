"""Durable run model: run.json schema 1, the transition table, and retry accounting.

Every mutation goes through `Run.transition`, which validates the move against
TRANSITIONS before touching state. Callers persist with `Run.save` and record
history through the event names the transition returns.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from runner import events

SCHEMA = 1
DEFAULT_BUDGET = 5

NEW, SCOPING, QUEUED, RUNNING = "new", "scoping", "queued", "running"
NEEDS_HUMAN, CANCELLED, DONE, PAUSED = "needs-human", "cancelled", "done", "paused"
STATUSES = (NEW, SCOPING, QUEUED, RUNNING, PAUSED, NEEDS_HUMAN, CANCELLED, DONE)
PARKED = (PAUSED, NEEDS_HUMAN, CANCELLED, DONE)
OUTCOMES = ("done", "blocked", "failed", "timeout", "cancelled")
SOURCES = ("gate", "skill", "runner", "condition")

STAGES = ("scope", "scope-review", "build", "ship")
HEADLESS = ("scope-review", "build", "ship")

# (from status, action) -> to status. "next" resolves to queued or done.
TRANSITIONS: dict[tuple[str, str], str] = {
    (NEW, "worktree_created"): SCOPING,
    (NEW, "setup_failed"): NEEDS_HUMAN,
    (SCOPING, "handoff"): QUEUED,
    (QUEUED, "slot_acquired"): RUNNING,
    (RUNNING, "stage_passed"): "next",
    (RUNNING, "retry"): QUEUED,
    (RUNNING, "exhausted"): CANCELLED,
    (RUNNING, "park"): NEEDS_HUMAN,
    (NEEDS_HUMAN, "operator_retry"): QUEUED,
    (CANCELLED, "operator_retry"): QUEUED,
    (NEEDS_HUMAN, "reopen_scope"): SCOPING,
    (CANCELLED, "reopen_scope"): SCOPING,
    (QUEUED, "pause"): PAUSED,
    (PAUSED, "unpause"): QUEUED,
    (QUEUED, "resume"): QUEUED,
    (RUNNING, "resume"): QUEUED,
    (DONE, "archive"): DONE,
}
for _status in (NEW, SCOPING, QUEUED, RUNNING, PAUSED, NEEDS_HUMAN):
    TRANSITIONS[(_status, "cancel")] = CANCELLED


class InvalidTransition(Exception):
    def __init__(self, status: str, action: str):
        super().__init__(f"cannot {action} a run in status {status}")
        self.status = status
        self.action = action


class SchemaError(Exception):
    pass


class BudgetExhausted(Exception):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def reason_hash(reason: str | None) -> str:
    normalized = re.sub(r"\s+", " ", (reason or "").strip().lower())
    normalized = re.sub(r"\b\d+(\.\d+)?s\b", "<t>", normalized)
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def next_stage(stage: str) -> str | None:
    index = STAGES.index(stage)
    return STAGES[index + 1] if index + 1 < len(STAGES) else None


class Run:
    """A run directory plus its run.json projection."""

    def __init__(self, run_dir: Path, data: dict):
        self.dir = Path(run_dir)
        self.data = data

    # --- persistence ----------------------------------------------------------

    @classmethod
    def load(cls, run_dir: Path) -> "Run":
        path = Path(run_dir) / "run.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise SchemaError(f"{path}: invalid JSON: {error}") from error
        schema = data.get("schema")
        if not isinstance(schema, int):
            raise SchemaError(f"{path}: missing schema")
        if schema > SCHEMA:
            raise SchemaError(
                f"{path}: run schema {schema} is newer than this runner supports ({SCHEMA}); upgrade the factory plugin"
            )
        if data.get("status") not in STATUSES:
            raise SchemaError(f"{path}: unknown status {data.get('status')!r}")
        return cls(Path(run_dir), data)

    def save(self) -> None:
        self.data["updated_at"] = utc_now()
        atomic_write(self.dir / "run.json", json.dumps(self.data, indent=2, ensure_ascii=False) + "\n")
        taken, self._taken_note = getattr(self, "_taken_note", None), None
        if taken is not None:
            # The note is durable in run.json now; remove the file unless the operator replaced it meanwhile.
            path = self.dir / "note"
            try:
                if path.read_text(encoding="utf-8") == taken:
                    path.unlink()
            except OSError:
                pass

    @classmethod
    def create(cls, runs_root: Path, *, repo: str, request: str, plan: str, remote: str = "origin",
               body: str | None = None, source: dict | None = None, retry_budget: int = DEFAULT_BUDGET,
               now: datetime | None = None) -> "Run":
        if isinstance(retry_budget, bool) or not isinstance(retry_budget, int) or retry_budget <= 0:
            raise ValueError(f"retry_budget must be a positive integer, got {retry_budget!r}")
        stamp = (now or datetime.now().astimezone()).strftime("%Y%m%d-%H%M")
        base_id = f"{stamp}-{plan}"
        runs_root.mkdir(parents=True, exist_ok=True)
        suffix = 1
        while True:
            run_id = base_id if suffix == 1 else f"{base_id}-{suffix}"
            try:
                (runs_root / run_id).mkdir()
                break
            except FileExistsError:
                suffix += 1
        created = utc_now()
        data = {
            "schema": SCHEMA,
            "id": run_id,
            "repo": repo,
            "remote": remote,
            "base": None,
            "base_sha": None,
            "branch": None,
            "plan": plan,
            "request": request,
            "request_file": None,
            "source": source or {"kind": "text"},
            "created_at": created,
            "updated_at": created,
            "status": NEW,
            "stage": "scope",
            "retries": {"used": 0, "budget": retry_budget},
            "scope_session_id": None,
            "stages": {},
            "attempts": [],
            "artifacts": {
                "spec": f".dev/{plan}/spec.md",
                "spec_review": None,
                "notes": None,
                "e2e_report": None,
                "review": None,
                "pr_body": None,
            },
            "pr": {"number": None, "url": None, "draft": None, "checks": None, "merged": None},
            "human": {"reason": None, "since": None, "note": None},
            "tokens_total": 0,
            "archived": False,
        }
        run = cls(runs_root / run_id, data)
        for sub in ("reports", "evidence", "scratch", "attempts"):
            (run.dir / sub).mkdir()
        request_file = run.dir / "request.md"
        atomic_write(request_file, (body if body is not None else request).rstrip() + "\n")
        data["request_file"] = str(request_file)
        run.save()
        events.append(run.dir, run_id, "run.created", "scope", repo=repo, plan=plan, request=request,
                      source=data["source"])
        return run

    # --- accessors -------------------------------------------------------------

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def status(self) -> str:
        return self.data["status"]

    @property
    def stage(self) -> str:
        return self.data["stage"]

    @property
    def plan(self) -> str:
        return self.data["plan"]

    @property
    def worktree(self) -> Path:
        return self.dir / "worktree"

    @property
    def report_dir(self) -> Path:
        return self.dir / "reports"

    @property
    def evidence_dir(self) -> Path:
        return self.dir / "evidence"

    @property
    def plan_dir(self) -> Path:
        return self.worktree / ".dev" / self.plan

    def attempt_dir(self, stage: str, n: int) -> Path:
        return self.dir / "attempts" / f"{stage}-{n}"

    def scratch_dir(self, stage: str, n: int) -> Path:
        return self.dir / "scratch" / f"{stage}-{n}"

    def stage_record(self, stage: str) -> dict:
        return self.data["stages"].setdefault(stage, {"outcome": None, "attempts": 0, "finished_at": None})

    def stage_attempts(self, stage: str) -> list[dict]:
        return [a for a in self.data["attempts"] if a["stage"] == stage]

    def last_attempt(self, stage: str | None = None) -> dict | None:
        attempts = self.stage_attempts(stage) if stage else self.data["attempts"]
        return attempts[-1] if attempts else None

    @property
    def cancel_requested(self) -> bool:
        return (self.dir / "cancel").exists()

    @property
    def pause_requested(self) -> bool:
        return (self.dir / "pause").exists()

    def take_note(self) -> str | None:
        """Move an operator note left in runs/{id}/note into run state; the worker calls this.

        The file is removed by the next save, so a crash before then leaves the note for the next attempt.
        """
        path = self.dir / "note"
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
        note = raw.strip()
        self._taken_note = raw
        if note:
            self.data["human"]["note"] = note
        return note or None

    def event(self, name: str, stage: str | None = None, attempt: int | None = None, **data: object) -> None:
        events.append(self.dir, self.id, name, stage if stage is not None else self.stage, attempt, **data)

    # --- transitions -------------------------------------------------------------

    def transition(self, action: str, *, reason: str | None = None) -> str:
        """Validate and apply one move from the transition table; returns the new status."""
        target = TRANSITIONS.get((self.status, action))
        if target is None:
            raise InvalidTransition(self.status, action)
        if target == "next":
            following = next_stage(self.stage)
            target = DONE if following is None else QUEUED
            if following is not None:
                self.data["stage"] = following
        self.data["status"] = target
        if target == QUEUED:
            self.data["queued_at"] = utc_now()
        if target == NEEDS_HUMAN:
            self.data["human"].update({"reason": reason, "since": utc_now()})
        elif target in (QUEUED, SCOPING, DONE):
            self.data["human"].update({"reason": None, "since": None})
        elif target in (CANCELLED, PAUSED):
            self.data["human"].update({"reason": reason or ("paused by operator" if target == PAUSED else None),
                                       "since": utc_now()})
        return target

    def retry_needed(self, stage: str | None = None) -> bool:
        """Attempt 1 of each headless stage is free; every later attempt costs one retry."""
        stage = stage or self.stage
        return stage in HEADLESS and self.stage_record(stage)["attempts"] >= 1

    def budget_remaining(self) -> int:
        retries = self.data["retries"]
        return retries["budget"] - retries["used"]

    def consume_retry(self) -> None:
        if self.budget_remaining() <= 0:
            raise BudgetExhausted(
                f"retry budget exhausted ({self.data['retries']['used']}/{self.data['retries']['budget']})"
            )
        self.data["retries"]["used"] += 1

    # --- attempts ---------------------------------------------------------------

    def begin_attempt(self, stage: str, *, host: str, model: str, effort: str) -> dict:
        record = self.stage_record(stage)
        record["attempts"] += 1
        queued = parse_ts(self.data.get("queued_at"))
        attempt = {
            "stage": stage,
            "n": record["attempts"],
            "host": host,
            "model": model,
            "effort": effort,
            "started_at": utc_now(),
            "ended_at": None,
            "exit_code": None,
            "session_id": None,
            "outcome": None,
            "reason": None,
            "reason_hash": None,
            "retryable": None,
            "source": None,
            "tokens": 0,
            "seconds": None,
            "queue_seconds": int((datetime.now(timezone.utc) - queued).total_seconds()) if queued else None,
        }
        self.data["attempts"].append(attempt)
        return attempt

    def finish_attempt(self, attempt: dict, *, outcome: str, reason: str | None, retryable: bool,
                       source: str, tokens: int = 0, exit_code: int | None = None,
                       session_id: str | None = None, seconds: float | None = None, **extra: object) -> None:
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome}")
        if source not in SOURCES:
            raise ValueError(f"unknown outcome source {source}")
        ended = utc_now()
        if seconds is None:
            started = parse_ts(attempt["started_at"])
            seconds = (parse_ts(ended) - started).total_seconds() if started else 0
        attempt.update({
            "ended_at": ended,
            "exit_code": exit_code,
            "session_id": session_id or attempt.get("session_id"),
            "outcome": outcome,
            "reason": reason,
            "reason_hash": reason_hash(reason) if reason else None,
            "retryable": retryable,
            "source": source,
            "tokens": int(tokens or 0),
            "seconds": int(seconds),
            **extra,
        })
        self.data["tokens_total"] = sum(int(a.get("tokens") or 0) for a in self.data["attempts"])
        record = self.stage_record(attempt["stage"])
        record["outcome"] = outcome
        record["finished_at"] = ended


def decide(run: Run, attempt: dict, *, stop_on_repeated_reason: bool) -> tuple[str, str | None]:
    """Choose the follow-up action for a finished headless attempt.

    Returns (action, reason) where action is one of stage_passed, retry,
    exhausted, park, or cancel. Pure apart from reading run state. Settings recorded
    on the attempt when it started win over the passed ones, so a configuration edit
    changes the next attempt, never the meaning of one already running.
    """
    semantic = (attempt.get("config") or {}).get("semantic") or {}
    stop_on_repeated_reason = semantic.get("stop_on_repeated_reason", stop_on_repeated_reason)
    outcome = attempt["outcome"]
    if outcome == "cancelled":
        return "cancel", attempt.get("reason") or "cancelled by operator"
    if outcome == "done":
        return "stage_passed", None
    reason = attempt.get("reason") or f"{attempt['stage']} attempt {attempt['n']} ended {outcome}"
    if not attempt.get("retryable"):
        return "park", reason
    previous = [a for a in run.stage_attempts(attempt["stage"]) if a["n"] < attempt["n"]]
    if previous and attempt.get("code") and attempt.get("fingerprint") and previous[-1].get("outcome") != "done" \
            and previous[-1].get("code") == attempt["code"] and previous[-1].get("fingerprint") == attempt["fingerprint"]:
        return "park", (f"no progress: {attempt['stage']} attempts {previous[-1]['n']} and {attempt['n']} both failed with "
                        f"{attempt['code']} and left the commit and plan files unchanged "
                        f"({attempt['fingerprint'][:12]}); last reason: {reason}")
    if stop_on_repeated_reason:
        previous = run.stage_attempts(attempt["stage"])[:-1]
        if previous and previous[-1].get("retryable") and previous[-1].get("outcome") != "done" \
                and previous[-1].get("reason_hash") == attempt.get("reason_hash"):
            return "park", f"same reason twice in a row at {attempt['stage']}: {reason}"
    if run.budget_remaining() <= 0:
        used, budget = run.data["retries"]["used"], run.data["retries"]["budget"]
        return "exhausted", f"retry budget exhausted ({used}/{budget}); last reason: {reason}"
    return "retry", reason
