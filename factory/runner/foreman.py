"""The foreman: one long-lived Codex session per run that decides what the worker does next.

The worker stays the body: it launches attempts, runs gates, and is the only writer of
run.json. The foreman is the brain: at every event the worker resumes the run's session with
a compact message and reads back one typed decision (factory.decision/1). A turn that fails,
times out, or answers outside the schema falls back to model.decide(), and the runner enforces
caps and hard stops whatever the decision says.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from runner import FACTORY_ROOT, commands, conditions, config, executor, hosts, provenance, records
from runner import worktree as wt
from runner.model import HEADLESS, Run, atomic_write, utc_now

DIR = "foreman"
GENERATED_SKILL = FACTORY_ROOT.parent / "plugins" / "factory" / "skills" / "foreman" / "SKILL.md"
DIGEST_SCHEMA = "factory.digest/1"
SOURCES = ("foreman", "fallback")


@dataclass
class Turn:
    n: int
    dir: Path
    cold: bool
    decision: dict | None
    source: str
    reason: str | None
    thread_id: str | None
    usage: dict
    seconds: float
    receipt: executor.Receipt | None = None
    extra: dict = field(default_factory=dict)


def state(run: Run) -> dict:
    return run.data.setdefault("foreman", {"session_id": None, "turns": 0, "restarts": 0, "usage": None})


# --- what the foreman sees ------------------------------------------------------------------

def caps(run: Run, cfg: config.Config) -> dict:
    """Every cap and how much of it the run has used; the runner, not the session, enforces them."""
    stages = {}
    waits = run.data.get("waits") or {}
    for stage in HEADLESS:
        attempts = run.stage_attempts(stage)
        stages[stage] = {
            "stage_attempts": {"used": sum(1 for a in attempts if a.get("kind", "stage") == "stage"),
                               "max": cfg.max_stage_attempts},
            "repairs": {"used": sum(1 for a in attempts if a.get("kind") == "repair"), "max": cfg.max_repairs_per_stage},
            "wait_minutes": {"used": round(float(waits.get(stage, 0)) / 60, 1), "max": cfg.max_wait_minutes},
        }
    created = run.data.get("created_at")
    hours = 0.0
    if created:
        from runner.model import parse_ts
        started = parse_ts(created)
        if started:
            from datetime import datetime, timezone
            hours = (datetime.now(timezone.utc) - started).total_seconds() / 3600
    return {"stages": stages, "run_hours": {"used": round(hours, 2), "max": cfg.max_run_hours},
            "overrides": {"used": len(run.data.get("overrides") or []), "max": cfg.max_overrides_per_run},
            "retries": dict(run.data["retries"])}


def _file(path: Path) -> str | None:
    return str(path) if path.exists() else None


def digest(run: Run, cfg: config.Config) -> dict:
    """The cold-start view of a run: every attempt, the open conditions, the caps, and the paths to read."""
    attempts = []
    for a in run.data["attempts"]:
        d = run.attempt_dir(a["stage"], a["n"])
        attempts.append({
            "stage": a["stage"], "n": a["n"], "kind": a.get("kind", "stage"), "host": a.get("host"),
            "model": a.get("model"), "effort": a.get("effort"), "outcome": a.get("outcome"), "code": a.get("code"),
            "retryable": a.get("retryable"), "source": a.get("source"),
            "reason": records.bounded(a.get("reason") or "", 300) or None,
            "fingerprint": (a.get("fingerprint") or "")[:12] or None, "tokens": a.get("tokens"),
            "seconds": a.get("seconds"), "started_at": a.get("started_at"), "ended_at": a.get("ended_at"),
            "conditions": a.get("conditions") or [], "warning": a.get("warning"),
            "gate_file": _file(d / "gate.json"), "last_message_file": _file(d / "last-message.md"),
            "stderr_file": _file(d / "stderr.log"), "prompt_file": _file(d / "prompt.txt"),
            "decision": a.get("foreman"),
        })
    git: dict = {}
    try:
        git = {"head": wt.head(run.worktree), "branch": wt.current_branch(run.worktree),
               "dirty_tracked": wt.dirty_tracked(run.worktree)[:10], "changed": wt.changed_paths(run.worktree)[:20],
               "log": wt.git("log", "-8", "--format=%h %s", cwd=run.worktree).splitlines()}
        if run.data.get("branch"):
            git["unpushed"] = wt.unpushed(run.worktree, run.data["remote"], run.data["branch"], fetch=False)
    except wt.GitError as error:
        git = {"error": str(error)}
    result_files = {stage: _file(run.plan_dir / f"{stage}-result.json") for stage in HEADLESS}
    return {
        "schema": DIGEST_SCHEMA,
        "generated_at": utc_now(),
        "run": {key: run.data.get(key) for key in ("id", "repo", "plan", "branch", "base", "base_sha", "status",
                                                    "stage", "created_at", "request_file", "source")},
        "attempts": attempts,
        "conditions": conditions.recorded(run),
        "pr": run.data.get("pr"),
        "operations": (run.data.get("operations") or [])[-5:],
        "decisions": (run.data.get("decisions") or [])[-10:],
        "overrides": run.data.get("overrides") or [],
        "guidance": run.data.get("guidance") or {},
        "operator_note": run.data["human"].get("note"),
        "git": git,
        "caps": caps(run, cfg),
        "paths": {"run_dir": str(run.dir), "worktree": str(run.worktree), "plan_dir": str(run.plan_dir),
                  "report_dir": str(run.report_dir), "evidence_dir": str(run.evidence_dir),
                  "intent_file": str(run.dir / "intent" / "approved.json") if run.data.get("intent") else None,
                  "events": str(run.dir / "events.jsonl"), "results": result_files},
        "policy": {"mode": cfg.foreman, "fallback": "model.decide()", "decision_schema": records.DECISION_SCHEMA},
    }


def write_digest(run: Run, cfg: config.Config) -> Path:
    path = run.dir / DIR / "digest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(digest(run, cfg), indent=2, default=str) + "\n")
    return path


def write_schema(run: Run) -> Path:
    path = run.dir / DIR / "decision.schema.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(records.decision_json_schema(), indent=2) + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        atomic_write(path, text)
    return path


def attempt_event(run: Run, stage: str, attempt: dict, outcome) -> dict:
    """The `attempt.finished` event for a closed attempt, before its transition is chosen."""
    previous = [a for a in run.stage_attempts(stage) if a["n"] < attempt["n"] and a.get("outcome")]
    streak = 1
    for prior in reversed(previous):
        if prior.get("outcome") != "done" and prior.get("code") == outcome.code and prior.get("code") \
                and prior.get("fingerprint") == attempt.get("fingerprint"):
            streak += 1
        else:
            break
    d = run.attempt_dir(stage, attempt["n"])
    return {
        "kind": "attempt.finished", "stage": stage, "n": attempt["n"], "attempt_kind": attempt.get("kind", "stage"),
        "outcome": outcome.outcome, "code": outcome.code, "reason": outcome.reason, "retryable": outcome.retryable,
        "source": outcome.source, "warning": outcome.warning, "conditions": list(outcome.conditions),
        "same_failure_streak": streak if outcome.outcome != "done" else 0,
        "files": {"gate": _file(d / "gate.json"), "last_message": _file(d / "last-message.md"),
                  "stderr": _file(d / "stderr.log"), "result": _file(run.plan_dir / f"{stage}-result.json")},
    }


def _caps_line(run: Run, cfg: config.Config, stage: str) -> str:
    c = caps(run, cfg)
    s = c["stages"].get(stage) or next(iter(c["stages"].values()))
    return (f"{stage} attempts {s['stage_attempts']['used']}/{s['stage_attempts']['max']}, "
            f"repairs {s['repairs']['used']}/{s['repairs']['max']}, "
            f"wait {s['wait_minutes']['used']}/{s['wait_minutes']['max']} min, "
            f"run {c['run_hours']['used']}/{c['run_hours']['max']} h, "
            f"overrides {c['overrides']['used']}/{c['overrides']['max']}, "
            f"retries {c['retries']['used']}/{c['retries']['budget']}")


def event_message(run: Run, cfg: config.Config, event: dict) -> str:
    """One compact turn: what just happened and where to read more. Never the full logs."""
    lines = [f"Factory run {run.id}: event {event['kind']}."]
    if event["kind"] == "attempt.finished":
        lines.append(f"{event['stage']} attempt {event['n']} ({event['attempt_kind']}) ended {event['outcome']}"
                     + (f" [{event['code']}]" if event.get("code") else "")
                     + (f": {records.bounded(event['reason'], 400)}" if event.get("reason") else "."))
        if event.get("warning"):
            lines.append(f"Warning: {records.bounded(event['warning'], 300)}")
        if event.get("same_failure_streak", 0) > 1:
            lines.append(f"Same failure code and unchanged tree {event['same_failure_streak']} attempts in a row.")
        files = event.get("files") or {}
        named = [f"{name} {path}" for name, path in files.items() if path]
        if named:
            lines.append("Files: " + "; ".join(named))
    else:
        for key, value in event.items():
            if key not in ("kind", "files") and value not in (None, "", [], {}):
                lines.append(f"{key}: {records.bounded(json.dumps(value, default=str), 400)}")
    stage = event.get("stage") or run.stage
    open_conditions = conditions.open_for(run, stage)
    if open_conditions:
        lines.append("Open conditions: " + "; ".join(f"{c['id']} {c['code']}: {records.bounded(c['summary'], 200)}"
                                                    for c in open_conditions))
    note = run.data["human"].get("note")
    if note:
        lines.append(f"Operator note: {records.bounded(note, 400)}")
    last_wait = run.data.get("last_wait")
    if last_wait and last_wait.get("stage") == stage:
        lines.append(f"Before this, the run waited {last_wait.get('seconds')}s for "
                     f"{(last_wait.get('probe') or {}).get('kind')}: {last_wait.get('observed')} "
                     f"({'probe passed' if last_wait.get('passed') else 'time ran out'}).")
    lines.append("Caps: " + _caps_line(run, cfg, stage) + ".")
    lines.append(f"Reply with exactly one {records.DECISION_SCHEMA} JSON object.")
    return "\n".join(lines) + "\n"


def start_prompt(run: Run, cfg: config.Config, event: dict, digest_path: Path, bundles: dict) -> str:
    if bundles.get("fallback") or bundles.get("resolution") == "direct-path":
        opening = [f"Follow the skill at {GENERATED_SKILL}.",
                   "Read it and the references it links from that directory; do not use an installed factory plugin.", ""]
    else:
        opening = ["$factory:foreman", ""]
    intro = [f"You are the foreman of factory run {run.id}, in the worktree {run.worktree}.",
             f"Read the digest at {digest_path} first: it lists every attempt so far, the open conditions,",
             "the caps, and the files to read. Then answer the event below.", ""]
    return "\n".join(opening + intro) + event_message(run, cfg, event)


# --- one turn -----------------------------------------------------------------------------------

def _read_decision(path: Path) -> tuple[dict | None, str | None]:
    if not path.exists():
        return None, "the turn wrote no final message"
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return None, "the turn's final message is empty"
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start > 0 or end < len(text) - 1:
        candidates.append(text[start:end + 1] if start >= 0 and end > start else "")
    error = None
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as decode_error:
            error = f"the final message is not JSON: {decode_error}"
            continue
        try:
            return records.validate_decision(data, name="decision"), None
        except records.RecordError as record_error:
            return None, f"{record_error.code}: {record_error}"
    return None, error


def _add_usage(st: dict, usage: dict) -> None:
    total = st.get("usage") or {"reported_turns": 0, "unreported_turns": 0, "input": 0, "cached_input": 0,
                                "output": 0, "total": 0}
    if usage.get("reported"):
        total["reported_turns"] += 1
        for key in ("input", "cached_input", "output", "total"):
            total[key] += int(usage.get(key) or 0)
    else:
        total["unreported_turns"] += 1
    st["usage"] = total


def _turn(run: Run, cfg: config.Config, event: dict, *, n: int, cold: bool, digest_path: Path, home: Path,
          grace: float, cancel_requested) -> Turn:
    st = state(run)
    turn_dir = run.dir / DIR / "turns" / str(n)
    turn_dir.mkdir(parents=True, exist_ok=True)
    schema_path = write_schema(run)
    if cold:
        bundles = provenance.skill_bundles()
        prompt = start_prompt(run, cfg, event, digest_path, bundles)
    else:
        prompt = event_message(run, cfg, event)
    (turn_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    # Hands only when its decisions are applied: a shadow foreman may look but never touch.
    sandbox = "workspace-write" if cfg.foreman == "codex" else "read-only"
    sandbox_env, cache_paths = commands.tool_caches(dict(os.environ), sandbox)
    scratch = run.dir / DIR / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    writable = [run.report_dir, run.evidence_dir, scratch, *wt.sandbox_git_dirs(run.worktree), *cache_paths] \
        if sandbox == "workspace-write" else []
    argv = hosts.codex_foreman_argv(
        prompt=prompt, model=cfg.foreman_model, effort=cfg.foreman_effort, sandbox=sandbox,
        worktree=run.worktree, writable=writable, last_message=turn_dir / "last-message.md", schema=schema_path,
        thread_id=None if cold else st.get("session_id"))
    current = run.stage_attempts(run.stage)
    env = hosts.attempt_env(run_dir=run.dir, plan=run.plan, stage=run.stage,
                            attempt=current[-1]["n"] if current else 0, forward_github_token=True)
    env.update(sandbox_env)
    env.update({"FACTORY_ROLE": "foreman", "FACTORY_TURN": str(n), "FACTORY_FOREMAN_DIGEST": str(digest_path)})
    before = _tree_state(run)
    request = executor.Request(
        token=executor.new_token(), kind="foreman", label=f"foreman-{n}", argv=argv, cwd=str(run.worktree),
        run_dir=str(run.dir), stdout=str(turn_dir / "stdout.jsonl"), stderr=str(turn_dir / "stderr.log"),
        deadline_at=time.time() + cfg.foreman_turn_timeout_s, cancel_file=str(run.dir / "cancel"), grace=grace)
    started = time.monotonic()
    receipt = executor.run(turn_dir, request, env, cancel_requested=cancel_requested)
    seconds = round(time.monotonic() - started, 1)
    stream = hosts.parse_codex_stream(turn_dir / "stdout.jsonl")
    usage = hosts.usage_record(stream)
    decision, reason = None, None
    if receipt.classification not in ("succeeded", "failed"):
        reason = f"turn {n} {receipt.classification}: {receipt.detail or receipt.spawn_error or 'no detail'}"
    elif receipt.exit_code not in (0, None):
        detail = "; ".join(stream.failures + stream.errors)
        reason = f"turn {n}: codex exited {receipt.exit_code}" + (f" ({detail[:300]})" if detail else "")
    else:
        decision, reason = _read_decision(turn_dir / "last-message.md")
        if reason:
            reason = f"turn {n}: {reason}"
    extra: dict = {}
    if sandbox == "workspace-write":
        hands = _hands_delta(run, before)
        extra["hands"] = hands
        if hands.get("committed"):
            extra["boundary"] = (f"turn {n} committed outside .dev/: {', '.join(hands['committed'][:10])}")
        elif hands.get("touched"):
            _revert(run, hands)
            decision, reason = None, (f"turn {n} changed files outside .dev/ ({', '.join(hands['touched'][:10])}); "
                                      "reverted, decision refused: source changes go through a repair or launch")
            extra["reverted"] = hands["touched"]
    turn = Turn(n=n, dir=turn_dir, cold=cold, decision=decision, source="foreman" if decision else "fallback",
                reason=reason, thread_id=stream.thread_id, usage=usage, seconds=seconds, receipt=receipt, extra=extra)
    record = {"schema": "factory.foreman-turn/1", "turn": n, "cold": cold, "event": event, "decision": decision,
              "source": turn.source, "reason": reason, "thread_id": stream.thread_id, "usage": usage,
              "seconds": seconds, "receipt": receipt.to_json(), "sandbox": sandbox, "extra": extra}
    atomic_write(turn_dir / "decision.json", json.dumps(record, indent=2, default=str) + "\n")
    return turn


def _tree_state(run: Run) -> dict:
    try:
        return {"head": wt.head(run.worktree), "changed": set(wt.changed_paths(run.worktree))}
    except wt.GitError as error:
        return {"error": str(error)}


def _hands_delta(run: Run, before: dict) -> dict:
    """What a turn changed in the worktree outside .dev/: touched (uncommitted) and committed paths."""
    if "head" not in before:
        return {"error": before.get("error")}
    try:
        committed = sorted(p for p in wt.committed_paths(run.worktree, before["head"]) if not p.startswith(".dev/"))
        after = wt.stage_delta(run.worktree, before["head"])
    except wt.GitError as error:
        return {"error": str(error)}
    touched = sorted(p for status, p in after if not p.startswith(".dev/") and p not in before["changed"]
                     and p not in committed)
    return {"touched": touched, "committed": committed, "untracked": [p for status, p in after if status == "?"]}


def _revert(run: Run, hands: dict) -> None:
    """Undo a turn's stray uncommitted edits outside .dev/; the next attempt must not inherit them."""
    untracked = set(hands.get("untracked") or [])
    for path in hands.get("touched") or []:
        try:
            if path in untracked:
                wt.git("-C", str(run.worktree), "clean", "-f", "--", path, check=False)
            else:
                wt.git("-C", str(run.worktree), "checkout", "--", path, check=False)
        except wt.GitError:
            continue


MAX_SESSION_TURNS = 40
LOST_SESSION = ("spawn_failed", "refused", "lost", "guard_error")


def consult(run: Run, cfg: config.Config, event: dict, *, home: Path, grace: float,
            cancel_requested=lambda: False) -> Turn:
    """Ask the run's foreman session about an event; up to `foreman_turns_per_event` tries, then a fallback.

    The session restarts from the cold digest when a resume is lost, the thread id changes, the context
    grows past `foreman_context_tokens`, the session is MAX_SESSION_TURNS old, or two turns in a row fell back.
    """
    st = state(run)
    if executor.live(run.dir):
        # A turn a dead worker started may still be running; never put a second execution beside it.
        executor.wait_idle(run.dir, cancel_requested=cancel_requested,
                           deadline_at=time.time() + cfg.foreman_turn_timeout_s, grace=grace)
    digest_path = write_digest(run, cfg)
    tries = max(1, int(cfg.foreman_turns_per_event))
    turn: Turn | None = None
    for _ in range(tries):
        restart = st.pop("restart", None)
        cold = not st.get("session_id") or bool(restart)
        n = int(st.get("turns") or 0) + 1
        turn = _turn(run, cfg, event, n=n, cold=cold, digest_path=digest_path, home=home, grace=grace,
                     cancel_requested=cancel_requested)
        st["turns"] = n
        st["session_turns"] = 1 if cold else int(st.get("session_turns") or 0) + 1
        if cold:
            if restart:
                st["restarts"] = int(st.get("restarts") or 0) + 1
                turn.extra["restarted"] = restart
            if turn.thread_id:
                st["session_id"] = turn.thread_id
        elif turn.thread_id and turn.thread_id != st.get("session_id"):
            st["restarts"] = int(st.get("restarts") or 0) + 1
            turn.extra["restarted"] = f"session {st.get('session_id')} answered as {turn.thread_id}"
            st["session_id"] = turn.thread_id
            st["session_turns"] = 1
        _add_usage(st, turn.usage)
        st["fallbacks"] = 0 if turn.decision is not None else int(st.get("fallbacks") or 0) + 1
        st["last"] = {"turn": n, "source": turn.source, "reason": turn.reason, "at": utc_now()}
        classification = turn.receipt.classification if turn.receipt else None
        if not cold and turn.decision is None and (classification in LOST_SESSION or not turn.thread_id
                                                   or (turn.receipt and turn.receipt.exit_code not in (0, None))):
            st["restart"] = f"resume of {st.get('session_id')} failed: {turn.reason}"
        elif int(turn.usage.get("input") or 0) > cfg.foreman_context_tokens:
            st["restart"] = f"context reached {turn.usage.get('input')} input tokens (limit {cfg.foreman_context_tokens})"
        elif st["session_turns"] >= MAX_SESSION_TURNS:
            st["restart"] = f"session is {st['session_turns']} turns old"
        elif st["fallbacks"] >= 2 and not st.get("restarted_after_fallbacks"):
            st["restart"] = "two turns in a row produced no decision"
            st["restarted_after_fallbacks"] = True
        if turn.decision is not None or cancel_requested():
            break
    assert turn is not None
    return turn


def record(run: Run, attempt: dict, turn: Turn, event: dict) -> None:
    """Attach the turn to its attempt and to the run's decision list. Idempotent per turn."""
    decision = turn.decision or {}
    attempt["foreman"] = {"turn": turn.n, "cold": turn.cold, "source": turn.source, "action": decision.get("action"),
                          "summary": decision.get("summary"), "reason": turn.reason, "thread_id": turn.thread_id,
                          "dir": os.path.relpath(turn.dir, run.dir), "seconds": turn.seconds,
                          "restarted": turn.extra.get("restarted"), "hands": turn.extra.get("hands")}
    decisions = run.data.setdefault("decisions", [])
    if not any(d.get("turn") == turn.n for d in decisions):
        decisions.append({
            "turn": turn.n, "at": utc_now(), "event": event.get("kind"), "stage": attempt["stage"],
            "attempt": attempt["n"], "action": decision.get("action"), "summary": decision.get("summary"),
            "target": decision.get("stage"), "source": turn.source, "reason": turn.reason, "applied": False})


def load_turn(run: Run, attempt: dict) -> Turn | None:
    """The turn already recorded for this attempt, so a worker that died after it does not ask again."""
    summary = attempt.get("foreman")
    if not summary or not summary.get("dir"):
        return None
    turn_dir = run.dir / summary["dir"]
    try:
        data = json.loads((turn_dir / "decision.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    decision = data.get("decision")
    if decision is not None:
        try:
            decision = records.validate_decision(decision)
        except records.RecordError:
            decision = None
    return Turn(n=int(data.get("turn") or summary["turn"]), dir=turn_dir, cold=bool(data.get("cold")),
                decision=decision, source="foreman" if decision else "fallback",
                reason=data.get("reason") if decision is None else None, thread_id=data.get("thread_id"),
                usage=data.get("usage") or {}, seconds=float(data.get("seconds") or 0),
                extra={**(data.get("extra") or {}), **({"restarted": summary["restarted"]} if summary.get("restarted")
                                                       else {})})


def turn_events(attempt: dict, turn: Turn) -> list:
    """The events a turn contributes to its attempt's transition."""
    decision = turn.decision or {}
    events: list = []
    if turn.extra.get("restarted"):
        events.append(["foreman.restarted", attempt["stage"], attempt["n"], {"turn": turn.n, "session": turn.thread_id,
                                                                            "reason": turn.extra["restarted"]}])
    elif turn.cold:
        events.append(["foreman.started", attempt["stage"], attempt["n"], {"turn": turn.n, "session": turn.thread_id}])
    if turn.extra.get("reverted"):
        events.append(["foreman.rejected", attempt["stage"], attempt["n"], {"action": None, "why": turn.reason,
                                                                           "reverted": turn.extra["reverted"]}])
    if turn.decision is not None:
        events.append(["foreman.decided", attempt["stage"], attempt["n"], {
            "turn": turn.n, "action": decision.get("action"), "summary": decision.get("summary"),
            "target": decision.get("stage")}])
    else:
        events.append(["foreman.fallback", attempt["stage"], attempt["n"], {"turn": turn.n, "reason": turn.reason}])
    return events
