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
    (RUNNING, "launch"): QUEUED,
    (RUNNING, "regate"): QUEUED,
    (RUNNING, "publish"): QUEUED,
    (RUNNING, "wait"): QUEUED,
    (RUNNING, "repair"): QUEUED,
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


# Codes no decision maker may lift: the foreman parks on them whatever it decided.
HARD_STOPS = ("secret.found", "action.destructive", "intent.missing", "intent.tampered")
GUIDANCE_MAX_BYTES = 16 * 1024


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

    def transition(self, action: str, *, reason: str | None = None, stage: str | None = None,
                   operator_action: str | None = None) -> str:
        """Validate and apply one move from the transition table; returns the new status.

        `stage` names the stage a `launch` queues; `operator_action` is the exact repair a park asks for.
        """
        target = TRANSITIONS.get((self.status, action))
        if target is None:
            raise InvalidTransition(self.status, action)
        if target == "next":
            following = next_stage(self.stage)
            target = DONE if following is None else QUEUED
            if following is not None:
                self.data["stage"] = following
        if stage is not None:
            if stage not in STAGES:
                raise ValueError(f"unknown stage {stage!r}")
            self.data["stage"] = stage
        self.data["status"] = target
        if target == QUEUED:
            self.data["queued_at"] = utc_now()
        if target == NEEDS_HUMAN:
            self.data["human"].update({"reason": reason, "since": utc_now(), "operator_action": operator_action})
        elif target in (QUEUED, SCOPING, DONE):
            self.data["human"].update({"reason": None, "since": None, "operator_action": None})
        elif target in (CANCELLED, PAUSED):
            self.data["human"].update({"reason": reason or ("paused by operator" if target == PAUSED else None),
                                       "since": utc_now(), "operator_action": operator_action})
        return target

    def undecided_attempt(self) -> dict | None:
        """The closed attempt whose next step is not chosen yet: the run is still running on it."""
        if self.status != RUNNING or self.stage not in HEADLESS:
            return None
        last = self.last_attempt()
        # Only an attempt of the running stage counts: the interactive scope attempt before it never
        # carries a transition record, and a stage whose attempt has not begun has nothing to decide.
        if last and last["stage"] == self.stage and last.get("ended_at") is not None and "transition" not in last:
            return last
        return None

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

    def begin_attempt(self, stage: str, *, host: str, model: str, effort: str, kind: str = "stage") -> dict:
        """An attempt of `kind` stage (an agent runs the skill), repair (a bounded agent fix), regate, or publish."""
        record = self.stage_record(stage)
        record["attempts"] += 1
        queued = parse_ts(self.data.get("queued_at"))
        attempt = {
            "stage": stage,
            "n": record["attempts"],
            "kind": kind,
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


# --- the foreman's decision under the runner's caps ---------------------------------------

def caps_exhausted(run: Run, cfg, target: str) -> str | None:
    """Why another agent attempt at `target` is not allowed, or None. The runner enforces these, not the session."""
    base = int((run.data.get("caps_base") or {}).get(target, 0))
    attempts = [a for a in run.stage_attempts(target)[base:] if a.get("kind", "stage") == "stage"]
    if len(attempts) >= cfg.max_stage_attempts:
        return f"cap reached: {target} has used {len(attempts)} of {cfg.max_stage_attempts} stage attempts"
    started = parse_ts(run.data.get("created_at"))
    if started is not None:
        hours = (datetime.now(timezone.utc) - started).total_seconds() / 3600
        if hours >= cfg.max_run_hours:
            return f"cap reached: the run is {hours:.1f} hours old, the limit is {cfg.max_run_hours}"
    recent = attempts[-3:]
    if len(recent) == 3 and all(a.get("outcome") != "done" and a.get("code") and a.get("fingerprint") for a in recent) \
            and len({(a["code"], a["fingerprint"]) for a in recent}) == 1:
        return (f"no progress: {target} attempts {recent[0]['n']}, {recent[1]['n']} and {recent[2]['n']} all failed with "
                f"{recent[0]['code']} and left the commit and plan files unchanged ({recent[0]['fingerprint'][:12]})")
    return None


def failure_codes(run: Run, attempt: dict) -> set[str]:
    """The gate code and every open condition code an attempt failed with."""
    codes = {attempt.get("code")}
    by_id = {c.get("id"): c for c in run.data.get("conditions") or []}
    for cid in attempt.get("conditions") or []:
        condition = by_id.get(cid) or {}
        if condition.get("status") != "resolved":
            codes.add(condition.get("code"))
    return {code for code in codes if code}


def hard_stop(run: Run, attempt: dict) -> str | None:
    """A code on this attempt or its open conditions that no decision may lift."""
    codes = [attempt.get("code")]
    by_id = {c.get("id"): c for c in run.data.get("conditions") or []}
    for cid in attempt.get("conditions") or []:
        condition = by_id.get(cid) or {}
        if condition.get("status", "open") != "resolved":
            codes.append(condition.get("code"))
    return next((code for code in codes if code in HARD_STOPS), None)


def enforce(run: Run, attempt: dict, decision: dict, cfg) -> tuple[str, str | None, str | None]:
    """Turn a validated foreman decision into a transition action the runner allows.

    Returns (action, reason, rejection). A rejection names why the decision was refused; the caller
    then falls back to `decide()`. Hard stops and caps win over the decision and never count as rejections.
    """
    outcome = attempt.get("outcome")
    stage = attempt["stage"]
    if outcome == "cancelled":
        return "cancel", attempt.get("reason") or "cancelled by operator", None
    stop = hard_stop(run, attempt)
    if stop:
        return "park", f"hard stop {stop}: {attempt.get('reason')}", None
    action = decision["action"]
    if action == "advance":
        if decision.get("stage") != stage:
            return "", None, f"advance names {decision.get('stage')!r} but the finished attempt is {stage}"
        if outcome == "done":
            return "stage_passed", None, None
        override = decision.get("override")
        if not override:
            return "", None, (f"advance needs a done outcome or an override naming the failed code; this attempt "
                              f"ended {outcome} [{attempt.get('code')}]")
        used = len(run.data.get("overrides") or [])
        if used >= cfg.max_overrides_per_run:
            return "", None, f"override refused: {used} of {cfg.max_overrides_per_run} overrides already used"
        codes = failure_codes(run, attempt)
        if override["gate_code"] not in codes:
            return "", None, (f"override names {override['gate_code']!r} but this attempt failed with "
                              f"{', '.join(sorted(codes)) or 'no code'}")
        return "override", None, None
    if action == "launch":
        target = decision["stage"]
        if HEADLESS.index(target) > HEADLESS.index(stage) and outcome != "done":
            return "", None, f"launch of {target} would skip the failed {stage} gate; advance it or launch {stage} again"
        cap = caps_exhausted(run, cfg, target)
        if cap:
            return "park", f"{cap}; last reason: {attempt.get('reason')}", None
        return ("retry" if target == stage else "launch"), decision.get("summary"), None
    if action == "repair":
        target = decision["stage"]
        history = run.stage_attempts(target)
        if not any(a.get("kind", "stage") == "stage" for a in history):
            return "", None, f"{target} has no attempt to repair; launch it first"
        base = int((run.data.get("caps_base") or {}).get(target, 0))
        repairs = sum(1 for a in history[base:] if a.get("kind") == "repair")
        if repairs >= cfg.max_repairs_per_stage:
            return "", None, f"{target} already used {repairs} of {cfg.max_repairs_per_stage} repairs; launch it instead"
        return "repair", decision.get("summary"), None
    if action in ("regate", "publish"):
        target = decision.get("stage") or ("ship" if action == "publish" else stage)
        if action == "publish" and target != "ship":
            return "", None, "publish applies to ship only: it pushes the run branch and opens the pull request"
        history = run.stage_attempts(target)
        if not any(a.get("kind", "stage") == "stage" for a in history):
            return "", None, f"{target} has no attempt to judge again; launch it first"
        trailing = 0
        for prior in reversed(history):
            if prior.get("kind") in ("regate", "publish"):
                trailing += 1
            else:
                break
        if trailing >= 2:
            return "", None, (f"{target} already had {trailing} gate-only attempts in a row since an agent worked on it; "
                              "launch or repair it instead")
        return action, decision.get("summary"), None
    if action == "wait":
        used = float((run.data.get("waits") or {}).get(stage, 0))
        remaining = cfg.max_wait_minutes * 60 - used
        if remaining <= 0:
            return "park", (f"cap reached: {stage} has waited {used / 60:.1f} of {cfg.max_wait_minutes} minutes; "
                            f"last reason: {attempt.get('reason')}"), None
        return "wait", decision.get("summary"), None
    if action == "park":
        return "park", decision["park"]["reason"], None
    if action == "cancel":
        return "cancel", decision["reason"], None
    if action == "rescope":
        return "rescope", decision["reason"], None
    return "", None, f"action {action!r} is not available yet"


def remember_guidance(run: Run, stage: str, heading: str, text: str) -> str:
    """Append the foreman's guidance for a stage, oldest sections dropped past GUIDANCE_MAX_BYTES."""
    entries = run.data.setdefault("guidance", {})
    section = f"## {heading}\n\n{text.strip()}\n"
    combined = (entries.get(stage) or "").rstrip()
    combined = f"{combined}\n\n{section}" if combined else section
    while len(combined.encode("utf-8")) > GUIDANCE_MAX_BYTES and combined.count("\n## ") >= 1:
        combined = combined[combined.index("\n## ") + 1:]
    entries[stage] = combined
    return combined
