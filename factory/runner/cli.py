"""`factory` command line: every command prints the outcome first, then evidence and next action."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from runner import (FACTORY_ROOT, browser, commands, conditions, config, control, dashboard, events, executor, foreman,
                    gates,
                    hosts, intent, pricing, provenance, records, requests, slots, supervise, watch)
from runner import worktree as wt
from runner.model import (CANCELLED, DONE, NEEDS_HUMAN, NEW, PAUSED, QUEUED, RUNNING, SCOPING, InvalidTransition,
                          Run, SchemaError, parse_ts, utc_now)
from runner.history import HistoryLocked
from runner.pipeline import PIPELINE, gate_context, write_run_context

STOPWORDS = {"a", "an", "the", "to", "for", "of", "in", "on", "and", "or", "with", "by", "from", "into", "at",
             "is", "be", "this", "that", "it", "as", "we", "our", "should", "so", "when"}
PLAN_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class CliError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def out(line: str = "") -> None:
    print(line, flush=True)


def local(ts: str | None) -> str:
    parsed = parse_ts(ts)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M") if parsed else "-"


def derive_plan(request: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", request.lower()) if w not in STOPWORDS]
    slug = ""
    for word in words[:5]:
        candidate = f"{slug}-{word}" if slug else word
        if len(candidate) > 40:
            break
        slug = candidate
    return slug or "change"


# --- run lookup ------------------------------------------------------------------

def resolve(home: Path, ident: str, *, include_archive: bool = False) -> Path:
    if not ident or "/" in ident or any(ch in ident for ch in "*?[]") or ident in (".", ".."):
        raise CliError(f"not a run id: {ident!r}; pass an exact id or an unambiguous prefix", 2)
    roots = [home / "runs"] + ([home / "archive"] if include_archive else [])
    candidates = [p for root in roots if root.is_dir() for p in root.iterdir() if p.is_dir()]
    exact = [p for p in candidates if p.name == ident]
    if exact:
        return exact[0]
    matches = sorted(p for p in candidates if p.name.startswith(ident))
    if not matches:
        raise CliError(f"no run matches {ident!r}", 1)
    if len(matches) > 1:
        raise CliError("ambiguous run id; matches:\n" + "\n".join(f"  {p.name}" for p in matches), 1)
    return matches[0]


def load_run(home: Path, ident: str, **kwargs: bool) -> Run:
    try:
        return Run.load(resolve(home, ident, **kwargs))
    except SchemaError as error:
        raise CliError(str(error), 2) from error


def attach(cfg: config.Config, run: Run, since: int | None = None) -> int:
    """Stay in the terminal and render the run until it finishes; the worker owns the run."""
    keys = watch.TerminalKeys() if watch.TerminalKeys.available() else None
    return watch.Watcher(run.dir, since=since, heartbeat_seconds=cfg.heartbeat_seconds,
                         poll=min(1.0, cfg.stage_poll_seconds), keys=keys, home=run.dir.parent.parent, cfg=cfg).run()


def cmd_watch(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    run = load_run(home, args.id)
    interactive = watch.TerminalKeys.available()
    if not supervise.worker_alive(run.dir) and (run.status == DONE or (run.status in (NEEDS_HUMAN, CANCELLED, PAUSED)
                                                                      and not interactive)):
        show_text(home, cfg, run)
        return 0 if run.status == DONE else 1
    if run.status in (NEW, SCOPING):
        raise CliError(f"{run.id} is {run.status}; nothing runs unattended until scope hands off "
                       f"(`factory scope {run.id} --resume`)")
    out(f"Watching {run.id}; q or Ctrl-C detaches and the run keeps going."
        + (" Press ? for controls." if interactive else ""))
    return attach(cfg, run)


refresh = control.refresh
start_worker = control.start_worker


# --- preflight ---------------------------------------------------------------------

def which(name: str) -> str | None:
    return shutil.which(config.binary(name))


def check_binaries(names: list[str]) -> list[tuple[str, str]]:
    failures = []
    for name in names:
        if not which(name):
            failures.append((f"{name} is not installed or not on PATH ({config.binary(name)})",
                             f"install {name} or set FACTORY_{name.upper()}_BIN"))
    return failures


def check_gh_auth() -> tuple[str, str] | None:
    code, stdout, stderr = gates.gh(["auth", "status"], Path.cwd(), timeout=60)
    if code != 0:
        return f"gh is not authenticated: {gates.first_lines(stderr or stdout, 2)}", "gh auth login"
    return None


def check_codex_plugin() -> tuple[str, str] | None:
    """Codex must run, and the generated skills attempts may fall back to must match factory/."""
    try:
        subprocess.run([config.binary("codex"), "plugin", "list"], capture_output=True, text=True, timeout=60,
                       stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"codex plugin list failed: {error}", "codex --version"
    current, detail = provenance.generated_is_current()
    if not current:
        return (f"plugins/factory is out of date with factory/: {gates.first_lines(detail, 2)}",
                f"python3 {FACTORY_ROOT.parent / 'scripts' / 'build_codex_plugin.py'} --plugin factory")
    return None


def check_claude_plugin() -> tuple[str, str] | None:
    if not (FACTORY_ROOT / ".claude-plugin" / "plugin.json").is_file():
        return f"no Claude plugin manifest at {FACTORY_ROOT / '.claude-plugin/plugin.json'}", "reinstall the workflow repo"
    return None


def preflight_new(repo: str, remote: str, jira: bool = False, base: str | None = None) -> list[tuple[str, str]]:
    failures = check_binaries(["git", "claude", "codex", "gh"] + (["acli"] if jira else []))
    if failures:
        return failures
    for check in (check_gh_auth, check_codex_plugin, check_claude_plugin):
        failure = check()
        if failure:
            failures.append(failure)
    try:
        root = wt.toplevel(repo)
        wt.require_remote(root, remote)
    except wt.GitError as error:
        failures.append((str(error), error.repair or f"git -C {repo} status"))
        return failures
    return failures


def tracked_plans(root: Path, remote: str, base: str | None, plan: str | None) -> tuple[list[str], list[str]]:
    """(the run's own plan files the base tracks, other tracked .dev/ files).

    A run writes only `.dev/{plan}`, which must be untracked so its plan never reaches the PR; plan files of
    other plans that the base tracks are left alone, and the stage gates reject any change to them.
    """
    try:
        sha = wt.resolve_base(root, remote, base or wt.default_branch(root, remote))
        tracked = wt.tracked_under(root, ".dev", ref=sha)
    except wt.GitError:
        return [], []
    own = [path for path in tracked if plan and path.startswith(f".dev/{plan}/")]
    return own, [path for path in tracked if path not in own]


def listed(paths: list[str]) -> str:
    return ", ".join(paths[:5]) + (f" (+{len(paths) - 5} more)" if len(paths) > 5 else "")


TRACKED_PLANS_REPAIR = "move them out of .dev/ or stop tracking them (`git rm -r --cached .dev`) in a normal commit on the base"


# --- scope -------------------------------------------------------------------------

def scope_session(home: Path, cfg: config.Config, run: Run, lock: supervise.WorkerLock, *, resume: bool,
                  yes: bool, detach: bool = False) -> int:
    stage = PIPELINE["scope"]
    attempt = run.begin_attempt("scope", host=stage.host, model=stage.model, effort=stage.effort)
    n = attempt["n"]
    resuming = resume and bool(run.data.get("scope_session_id"))
    if not resuming:
        run.data["scope_session_id"] = str(uuid.uuid4())
    attempt["session_id"] = run.data["scope_session_id"]
    # The gate judges only what this scope session changes; after a rescope the branch already carries build's commits.
    attempt["baseline"] = {"head": wt.head(run.worktree), "branch": wt.current_branch(run.worktree)}
    write_run_context(run, "scope", n, interactive=True)
    run.save()
    run.event("scope.launched", "scope", n, session_id=run.data["scope_session_id"], resume=resuming)
    argv = hosts.claude_argv(prompt=stage.build_prompt(run, n), model=stage.model, effort=stage.effort,
                             plugin_root=FACTORY_ROOT, run_dir=run.dir, session_id=run.data["scope_session_id"],
                             resume=resuming)
    env = hosts.attempt_env(run_dir=run.dir, plan=run.plan, stage="scope", attempt=n)
    try:
        code = subprocess.run(argv, cwd=run.worktree, env=env).returncode
    except OSError as error:
        run.finish_attempt(attempt, outcome="failed", reason=f"cannot launch claude: {error}", retryable=False,
                           source="runner")
        run.save()
        raise CliError(f"Scope could not start: {error}", 2) from error

    ctx = gate_context(run, "scope", n, cancel_file=None)
    gate = gates.scope_gate(ctx)
    if not gate.passed:
        run.finish_attempt(attempt, outcome="blocked", reason=gate.reason, retryable=True, source="gate",
                           exit_code=code)
        run.save()
        run.event("scope.gate_failed", "scope", n, failures=gate.failures)
        refresh(home, cfg)
        out(f"Scope is not ready to hand off: {len(gate.failures)} gate check(s) failed.")
        for failure in gate.failures:
            out(f"  - {failure}")
        out()
        out(f"> factory scope {run.id} --resume")
        return 1

    if run.cancel_requested:
        run.finish_attempt(attempt, outcome="cancelled", reason="cancelled by operator during scope", retryable=False,
                           source="runner", exit_code=code)
        run.transition("cancel", reason="cancelled by operator during scope")
        run.save()
        run.event("run.cancelled", "scope", n, reason="cancelled by operator during scope")
        refresh(home, cfg)
        out(f"Cancelled {run.id} during scope; the spec was not committed and the worktree is kept.")
        return 1

    if not yes:
        if not sys.stdin.isatty():
            run.finish_attempt(attempt, outcome="blocked", reason="handoff not confirmed", retryable=True,
                               source="runner", exit_code=code)
            run.save()
            out("Scope passed its gate, but no terminal is available to confirm the handoff.")
            out(f"> factory scope {run.id} --resume --yes")
            return 1
        answer = input("Hand this run to the factory? [Y/n] ").strip().lower()
        if answer not in ("", "y", "yes"):
            run.finish_attempt(attempt, outcome="blocked", reason="operator declined the handoff", retryable=True,
                               source="runner", exit_code=code)
            run.save()
            out("Run stays in scoping; nothing was committed.")
            out(f"> factory scope {run.id} --resume")
            return 0

    missing = handoff_environment_gaps(run)
    if missing:
        run.finish_attempt(attempt, outcome="blocked", reason=missing, retryable=True, source="runner", exit_code=code)
        run.save()
        out(f"Scope passed its gate, but the unattended stages could not run: {missing}")
        out(f"> export the variable(s), then: factory scope {run.id} --resume --yes")
        return 1

    paths, _ = gates.scope_commit_paths(ctx)
    try:
        sha = wt.commit_paths(run.worktree, paths, f"docs(scope): record decisions for {run.plan}")
    except wt.GitError as error:
        run.finish_attempt(attempt, outcome="failed", reason=f"scope commit failed: {error}", retryable=True,
                           source="runner", exit_code=code)
        run.save()
        out(f"Scope passed its gate, but committing the spec failed: {error}")
        out(f"> factory scope {run.id} --resume")
        return 1
    run.event("scope.committed", "scope", n, commit=sha, paths=paths, nothing_to_commit=sha is None)
    try:
        approved = intent.snapshot_handoff(run)
    except (OSError, ValueError) as error:
        run.finish_attempt(attempt, outcome="failed", reason=f"approved-intent snapshot failed: {error}", retryable=True,
                           source="runner", exit_code=code)
        run.save()
        out(f"Scope passed its gate, but sealing the approved intent failed: {error}")
        out(f"> factory scope {run.id} --resume")
        return 1
    run.event("intent.approved", "scope", n, version=approved["version"], sha256=approved["content_sha256"],
              scenarios=[s["id"] for s in approved["scenarios"]], non_goals=len(approved["non_goals"]))
    run.finish_attempt(attempt, outcome="done", reason=None, retryable=True, source="gate", exit_code=code)
    run.transition("handoff")
    run.data["stage"] = "scope-review"
    run.save()
    run.event("run.queued", "scope-review", None, previous="scope")
    refresh(home, cfg)
    lock.release()
    since = len(events.read(run.dir))
    pid = start_worker(home, run)
    if detach:
        out(f"Handed off: worker {pid} is running {run.id} unattended.")
        out()
        show_text(home, cfg, Run.load(run.dir))
        return 0
    out(f"Handed off to worker {pid}. Watching the assembly line; Ctrl-C detaches and the run keeps going."
        + (" Press ? for controls." if watch.TerminalKeys.available() else ""))
    return attach(cfg, run, since)


def handoff_environment_gaps(run: Run) -> str | None:
    """Environment names the contract requires that the worker, started from this process, would not have."""
    try:
        contract = records.load_contract(run.worktree / records.CONTRACT_PATH)
    except records.RecordError:
        return None
    names = set(contract.get("environment", {}).get("required", []))
    for section in ("setup", "validation", "services"):
        for command in contract.get(section, []):
            names |= set(command.get("env", []))
    missing = sorted(name for name in names if not os.environ.get(name))
    return f"required environment variable(s) unset: {', '.join(missing)}" if missing else None


def check_request_args(args: argparse.Namespace) -> None:
    given = [name for name, value in (("request", args.request), ("--file", args.file), ("--jira", args.jira)) if value]
    if len(given) != 1:
        raise CliError("give exactly one change request: a quoted string, --file PATH, or --jira KEY"
                       + (f" (got {', '.join(given)})" if given else ""), 2)


def load_request(args: argparse.Namespace) -> requests.Request:
    if args.file:
        return requests.from_file(args.file)
    if args.jira:
        return requests.from_jira(args.jira)
    return requests.from_text(args.request)


def cmd_new(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    check_request_args(args)
    failures = preflight_new(args.repo, args.remote, jira=bool(args.jira), base=args.base)
    if failures:
        out(f"Preflight failed: {len(failures)} problem(s); no run was created.")
        for problem, repair in failures:
            out(f"  - {problem}")
            out(f"    repair: {repair}")
        return 2
    try:
        request = load_request(args)
    except requests.RequestError as error:
        out(f"Could not read the change request: {error}; no run was created.")
        if error.repair:
            out(f"> {error.repair}")
        return 2
    plan = args.plan or derive_plan(request.title)
    if not PLAN_SLUG.match(plan):
        raise CliError(f"--plan must be kebab-case, got {plan!r}", 2)
    repo = str(wt.toplevel(args.repo))
    own, others = tracked_plans(Path(repo), args.remote, args.base, plan)
    if own:
        out("Preflight failed: 1 problem(s); no run was created.")
        out(f"  - the base already tracks this run's plan directory .dev/{plan}/: {listed(own)}")
        out(f"    repair: choose another name with --plan, or {TRACKED_PLANS_REPAIR}")
        return 2
    if others:
        out(f"Note: the base tracks other plan files under .dev/ ({listed(others)}); this run leaves them "
            "alone, and a stage that changes them fails its gate.")
    summary = request.body if request.source["kind"] == "text" else request.title
    run = Run.create(home / "runs", repo=repo, request=summary, plan=plan, remote=args.remote,
                     body=request.markdown(), source=request.source, retry_budget=cfg.max_retries)
    lock = supervise.WorkerLock(run.dir)
    if not lock.acquire():
        raise CliError(f"could not lock new run {run.id}", 2)
    try:
        try:
            created = wt.create(repo, run.worktree, plan, remote=args.remote, base=args.base)
        except wt.GitError as error:
            reason = f"worktree setup failed: {error}"
            run.data["human"]["note"] = f"repair: {error.repair}" if error.repair else None
            run.transition("setup_failed", reason=reason)
            run.save()
            run.event("run.needs_human", "scope", None, reason=reason, repair=error.repair)
            refresh(home, cfg)
            out(f"Run {run.id} needs you: {reason}")
            if error.repair:
                out(f"> {error.repair}")
            return 1
        run.data.update({"base": created.base, "base_sha": created.base_sha, "branch": created.branch})
        run.transition("worktree_created")
        run.save()
        run.event("worktree.created", "scope", None, worktree=str(created.worktree), branch=created.branch,
                  base=created.base, base_sha=created.base_sha)
        run.event("run.scoping", "scope", None)
        refresh(home, cfg)
        out(f"Created run {run.id} on {created.branch} (base {created.base} {created.base_sha[:12]}).")
        return scope_session(home, cfg, run, lock, resume=False, yes=args.yes, detach=args.detach)
    finally:
        lock.release()


def cmd_scope(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    run = load_run(home, args.id)
    if run.status != SCOPING:
        raise CliError(f"{run.id} is {run.status}, not scoping; `factory show {run.id}` names the next step")
    lock = supervise.WorkerLock(run.dir)
    if not lock.acquire():
        raise CliError(f"{run.id} is locked by another factory process")
    try:
        return scope_session(home, cfg, Run.load(run.dir), lock, resume=args.resume, yes=args.yes,
                             detach=args.detach)
    finally:
        lock.release()


# --- inspection ------------------------------------------------------------------------

def cmd_ls(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    rows = dashboard.summaries(home, heartbeat_seconds=cfg.heartbeat_seconds, include_archive=args.all)
    if not args.all:
        rows = [r for r in rows if r["group"] != "done"]
    if args.json:
        out(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    needs = sum(1 for r in rows if r["group"] == "needs")
    flight = sum(1 for r in rows if r["group"] == "flight")
    done = sum(1 for r in rows if r["group"] == "done")
    headline = f"{needs} need you, {flight} in flight"
    out(headline + (f", {done} done" if args.all else "") + ".")
    for row in rows:
        marker = {"needs": "!", "flight": "~", "done": " "}[row["group"]]
        status = row["status"] + ((" (worker down, execution alive)" if row.get("execution_alive") else " (worker down)")
                                  if row.get("stale") else "")
        out()
        out(f"{marker} {row['id']}  {row.get('stage') or '-'}  {status}  retries {row.get('retries') or '-'}")
        details = []
        if row.get("blocker"):
            details.append(row["blocker"])
        pr = row.get("pr")
        if pr:
            details.append(f"PR #{pr['number']}{' draft' if pr.get('draft') else ''}")
        if row.get("duration"):
            details.append(row["duration"])
        if details:
            out("  " + "  ".join(details))
        if row["group"] == "needs" and row.get("next"):
            out()
            out(f"> {row['next']}")
    return 0


def outcome_sentence(run: Run, summary: dict) -> str:
    status = run.status
    if summary.get("stale"):
        return f"Worker for {run.id} is not running while the run is {status} at {run.stage}."
    if status == DONE:
        return f"Done: pull request {run.data['pr'].get('url') or '(not recorded)'} is ready."
    if status == NEEDS_HUMAN:
        return f"Needs you at {run.stage}: {run.data['human'].get('reason')}"
    if status == CANCELLED:
        return f"Cancelled at {run.stage}: {run.data['human'].get('reason')}"
    if status == RUNNING:
        return f"Running {run.stage} attempt {run.stage_record(run.stage)['attempts']}."
    if status == QUEUED:
        return f"Queued for {run.stage}, waiting for a stage slot."
    if status == PAUSED:
        return f"Paused before {run.stage}; nothing starts until you continue it."
    if status == SCOPING:
        return "Scoping: interactive scope has not handed off yet."
    return f"Status {status} at {run.stage}."


def show_text(home: Path, cfg: config.Config, run: Run) -> None:
    summary = dashboard.summarize(run.dir, run, None, heartbeat_seconds=cfg.heartbeat_seconds)
    data = run.data
    out(outcome_sentence(run, summary))
    out()
    out(f"run        {run.id}")
    out(f"repository {data['repo']}")
    source = data.get("source") or {"kind": "text"}
    if source.get("kind") == "jira":
        out(f"request    Jira {source.get('key')} {source.get('url') or ''}".rstrip())
    elif source.get("kind") == "file":
        out(f"request    file {source.get('path')}")
    if data.get("request_file"):
        out(f"           {data['request_file']}")
    out(f"branch     {data.get('branch') or '-'}")
    out(f"worktree   {run.worktree}")
    out(f"base       {data.get('base') or '-'} {(data.get('base_sha') or '')[:12]}")
    out(f"stage      {run.stage} ({run.status})")
    operation_retries = sum(max(0, op.get("tries", 1) - 1) for op in data.get("operations", []))
    out(f"retries    {data['retries']['used']}/{data['retries']['budget']} paid"
        + (f"; {operation_retries} deterministic operation retr{'y' if operation_retries == 1 else 'ies'} "
           "(not charged)" if operation_retries else ""))
    if cfg.foreman != "off" or data.get("foreman"):
        caps = foreman.caps(run, cfg)
        stage_caps = caps["stages"].get(run.stage) or caps["stages"]["build"]
        out(f"caps       {run.stage} attempts {stage_caps['stage_attempts']['used']}/{stage_caps['stage_attempts']['max']}, "
            f"repairs {stage_caps['repairs']['used']}/{stage_caps['repairs']['max']}, "
            f"wait {stage_caps['wait_minutes']['used']}/{stage_caps['wait_minutes']['max']} min, "
            f"run {caps['run_hours']['used']}/{caps['run_hours']['max']} h, "
            f"overrides {caps['overrides']['used']}/{caps['overrides']['max']}")
    latest = run.last_attempt()
    if latest and latest.get("ended_at") is None and run.status == RUNNING:
        current = intent.read_json(run.attempt_dir(latest["stage"], latest["n"]) / "subphase.json")
        if current:
            out(f"subphase   {latest['stage']}: {current['current']} ({current['decision']})")
    if latest and latest.get("checkpoints"):
        decisions = ", ".join(f"{c['subphase']} {c['decision'].split(':')[0]}" for c in latest["checkpoints"])
        out(f"checkpoint {latest['stage']}-{latest['n']}: {decisions}")
    for op in data.get("operations", [])[-3:]:
        out(f"operation  {op['kind']} {op['result']} after {op['tries']} tr{'y' if op['tries'] == 1 else 'ies'} "
            f"on {op['revision'][:12]}: {op['details'][-1] if op['details'] else ''}")
    usage = data.get("usage")
    if usage:
        costs = [a.get("cost") or {} for a in data["attempts"] if a.get("host") == "codex"]
        known = [c["amount"] for c in costs if c.get("amount") is not None]
        cost = (f"estimated {sum(known):.2f} {costs[0].get('currency', 'USD')}" if known and len(known) == len(costs)
                else "estimated cost unknown" + (f" ({costs[-1]['why']})" if costs and costs[-1].get("why") else ""))
        out(f"usage      {pricing.describe(usage)}; scope usage unknown; {cost}")
    else:
        out(f"tokens     {data.get('tokens_total', 0)} (categories not recorded for this run)")
    if data["human"].get("reason"):
        out(f"blocker    {data['human']['reason']}")
    if data["human"].get("operator_action"):
        out(f"action     {data['human']['operator_action']}")
    if data["human"].get("note"):
        out(f"note       {data['human']['note']}")
    if control.pending_note(run):
        out(f"next note  {control.pending_note(run)} (applied at the next attempt)")
    if (run.dir / "pause").exists() and run.status in (QUEUED, RUNNING):
        out("pause      requested; takes effect before the next attempt")
    age = summary.get("heartbeat_age")
    worker = "alive" if summary["worker_alive"] else "not running"
    execution = {True: "; its execution is still alive and a resumed worker reattaches to it",
                 None: "; an execution process may be alive but cannot be verified"}.get(summary.get("execution_alive"), "")
    out(f"worker     {worker}" + (f", heartbeat {age}s ago" if age is not None else "") + execution)
    pr = data["pr"]
    if pr.get("url"):
        checks = pr.get("checks") or {}
        check_text = ", ".join(f"{k} {len(v)}" for k, v in checks.items() if isinstance(v, list)) or "not recorded"
        out(f"pr         #{pr['number']} {pr['url']} ({'draft' if pr.get('draft') else 'ready'}; checks {check_text}; "
            f"merged {pr.get('merged')})")
    state = data.get("foreman")
    if state:
        usage = state.get("usage") or {}
        out()
        out(f"foreman    {cfg.foreman} mode, session {state.get('session_id') or 'none'}, {state.get('turns', 0)} turn(s), "
            f"{state.get('restarts', 0)} restart(s), in {usage.get('input', 0)} / out {usage.get('output', 0)} tokens")
        if state.get("last"):
            out(f"           last turn {state['last'].get('turn')}: {state['last'].get('source')}"
                + (f" ({state['last'].get('reason')})" if state["last"].get("reason") else ""))
    if data.get("decisions"):
        out()
        out("decisions")
        for decision in data["decisions"][-8:]:
            if decision.get("source") == "foreman":
                applied = "applied" if decision.get("applied") else "recorded, not applied"
                target = decision.get("target")
                where = f" {target}" if target and target != decision.get("stage") else ""
                out(f"  {decision['stage']}-{decision['attempt']} turn {decision['turn']}: {decision['action']}{where} "
                    f"({applied}): {decision.get('summary')}")
            else:
                out(f"  {decision['stage']}-{decision['attempt']} turn {decision['turn']}: no decision, the runner chose "
                    f"({decision.get('reason')})")
    if data.get("overrides"):
        out()
        out("overrides")
        for entry in data["overrides"]:
            out(f"  {entry['stage']}-{entry['attempt']} {entry['gate_code']} (turn {entry.get('turn')}): "
                f"{entry['justification']}")
        if data.get("overrides_noted"):
            out(f"  {data['overrides_noted']}")
    if data.get("conditions"):
        out()
        out("conditions")
        for condition in data["conditions"]:
            where = f"{condition['stage']} attempt {condition['attempt']}"
            out(f"  {condition['id']} {condition['code']} ({condition['status']}, raised at {where}): "
                f"{condition['summary']}")
            for item in condition.get("evidence", [])[:5]:
                out(f"     evidence: {item}")
            for check in condition.get("checks", [])[-2:]:
                out(f"     re-check at attempt {check['attempt']}: {check['observed']}")
            resolution = condition.get("resolution")
            if resolution:
                out(f"     resolved at attempt {resolution['attempt']} by {resolution['by']}: "
                    f"{'; '.join(resolution['evidence']) or 'no evidence'}")
            else:
                out(f"     next: {conditions.next_action(condition, run.id)}")
    out()
    out("attempts")
    if not data["attempts"]:
        out("  none yet")
    for attempt in data["attempts"]:
        seconds = attempt.get("seconds")
        duration = dashboard.human_duration(seconds) if seconds is not None else "in progress"
        timing = ""
        if attempt.get("host_seconds") is not None:
            timing = (f" (queue {attempt.get('queue_seconds') or 0}s, agent {attempt['host_seconds']:.0f}s, "
                      f"gate {attempt.get('gate_seconds', 0):.0f}s of which verification "
                      f"{attempt.get('verification_seconds', 0):.0f}s and lease wait {attempt.get('lease_wait_seconds', 0):.0f}s)")
        kind = f" ({attempt['kind']})" if attempt.get("kind", "stage") != "stage" else ""
        out(f"  {attempt['stage']}-{attempt['n']}{kind}  {attempt.get('outcome') or 'running'}  {duration}{timing}  "
            f"{attempt['model']}/{attempt['effort']}  tokens {attempt.get('tokens', 0)}")
        if attempt.get("reason"):
            out(f"    {attempt['reason']}" + (f" [{attempt['code']}]" if attempt.get("code") else ""))
        if attempt.get("foreman"):
            decision = attempt["foreman"]
            if decision.get("source") == "foreman":
                out(f"    foreman turn {decision['turn']}: {decision.get('action')}"
                    + (f" -> {decision['applied']}" if decision.get("applied") else "")
                    + (f" (refused: {decision['rejected']})" if decision.get("rejected") else ""))
            else:
                out(f"    foreman turn {decision.get('turn')}: no decision ({decision.get('reason')})")
    out()
    out("artifacts")
    for key, value in data["artifacts"].items():
        if value:
            out(f"  {key:<11} {value}")
    out(f"  {'reports':<11} {run.report_dir}")
    if summary.get("next"):
        out()
        out(f"> {summary['next']}")


def cmd_show(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    run = load_run(home, args.id, include_archive=True)
    if args.json:
        summary = dashboard.summarize(run.dir, run, None, heartbeat_seconds=cfg.heartbeat_seconds)
        out(json.dumps({"summary": summary, "run": run.data}, indent=2, sort_keys=True))
        return 0
    show_text(home, cfg, run)
    return 0


def cmd_foreman(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    """The foreman session's state and decisions, or one turn's prompt, answer, and record."""
    run = load_run(home, args.id, include_archive=True)
    state = run.data.get("foreman") or {}
    turns_dir = run.dir / foreman.DIR / "turns"
    if args.turn is not None:
        turn_dir = turns_dir / str(args.turn)
        record = intent.read_json(turn_dir / "decision.json")
        if record is None:
            out(f"No foreman turn {args.turn} for {run.id}.")
            return 1
        if args.json:
            out(json.dumps(record, indent=2))
            return 0
        out(f"Foreman turn {args.turn} of {run.id}: {record.get('source')}"
            + (f" ({record.get('reason')})" if record.get("reason") else "") + f", {record.get('seconds')}s, "
            f"{'cold start' if record.get('cold') else 'resumed'} session {record.get('thread_id')}")
        out(f"policy     {(record.get('policy') or {}).get('sha256') or 'unknown'}")
        out()
        out("prompt")
        for line in (turn_dir / "prompt.txt").read_text(encoding="utf-8").splitlines() if (turn_dir / "prompt.txt").exists() else []:
            out(f"  {line}")
        out()
        out("decision")
        out(json.dumps(record.get("decision"), indent=2) if record.get("decision") else "  none")
        hands = (record.get("extra") or {}).get("hands") or {}
        if hands.get("touched") or hands.get("committed"):
            out()
            out(f"hands      touched {hands.get('touched')}, committed {hands.get('committed')}")
        return 0
    if args.json:
        out(json.dumps({"foreman": state, "decisions": run.data.get("decisions") or [],
                        "overrides": run.data.get("overrides") or [], "caps": foreman.caps(run, cfg)}, indent=2))
        return 0
    if not state:
        out(f"No foreman session for {run.id} (foreman mode is {cfg.foreman}).")
        return 0
    usage = state.get("usage") or {}
    out(f"Foreman of {run.id}: {cfg.foreman} mode, session {state.get('session_id') or 'none'}, "
        f"{state.get('turns', 0)} turn(s), {state.get('restarts', 0)} restart(s), "
        f"in {usage.get('input', 0)} / out {usage.get('output', 0)} tokens.")
    out()
    out("decisions")
    from runner import outcomes
    policies = outcomes.turn_policies(run.dir)
    for decision in run.data.get("decisions") or []:
        policy = f"policy {policies.get(decision['turn'], 'unknown')[:12]}"
        if decision.get("source") == "foreman":
            applied = "applied" if decision.get("applied") else "recorded, not applied"
            target = decision.get("target")
            where = f" {target}" if target and target != decision.get("stage") else ""
            out(f"  turn {decision['turn']:>3}  {decision['stage']}-{decision['attempt']}  {decision['action']}{where} "
                f"({applied}, {policy}): {decision.get('summary')}")
        else:
            out(f"  turn {decision['turn']:>3}  {decision['stage']}-{decision['attempt']}  no decision ({policy}): "
                f"{decision.get('reason')}")
    if not run.data.get("decisions"):
        out("  none yet")
    if run.data.get("overrides"):
        out()
        out("overrides")
        for entry in run.data["overrides"]:
            out(f"  {entry['stage']}-{entry['attempt']} {entry['gate_code']}: {entry['justification']}")
    out()
    out(f"> factory foreman {run.id} --turn N")
    return 0


def cmd_intent(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    run = load_run(home, args.id, include_archive=True)
    try:
        document = intent.load_approved(run.dir, (run.data.get("intent") or {}).get("sha256"))
    except intent.IntentError as error:
        out(f"No verified approved intent for {run.id}: {error}")
        if error.repair:
            out(f"> {error.repair}")
        return 1
    scenarios = intent.catalog(run.dir)
    deltas = sorted((intent.intent_dir(run.dir) / "deltas").glob("*.json"))
    if args.json:
        out(json.dumps({"approved": document, "scenarios": scenarios,
                        "deltas": [json.loads(p.read_text(encoding="utf-8")) for p in deltas]}, indent=2))
        return 0
    out(f"Approved intent v{document['version']} for {run.id}, sealed {document['handoff_at']} "
        f"(sha256 {document['content_sha256'][:12]}).")
    out()
    out(f"request    {document['request']}")
    out("non-goals")
    for goal in document["non_goals"] or ["none recorded"]:
        out(f"  - {goal}")
    out("scenarios")
    for scenario in scenarios:
        state = "approved" if scenario.get("approved") else f"added by {scenario.get('origin')}"
        gone = "" if scenario.get("present") else ", no longer in the spec"
        repro = " [repro]" if scenario.get("repro") else ""
        out(f"  {scenario['id']:<4} [{scenario['layer']}]{repro} {scenario['requirement']} ({state}{gone})")
    if deltas:
        out("decision deltas")
    for path in deltas:
        delta = json.loads(path.read_text(encoding="utf-8"))
        decisions = delta["decisions"]
        changed = [d["slug"] for d in decisions["changed"]] + [f"+{d['slug']}" for d in decisions["added"]] + \
                  [f"-{d['slug']}" for d in decisions["removed"]]
        out(f"  {delta['stage']}-{delta['attempt']}: decisions {', '.join(changed) or 'unchanged'}; "
            f"scenarios added {', '.join(delta['scenarios']['added']) or 'none'}; "
            f"affected {', '.join(delta['affected_scenarios']) or 'none'}; "
            f"{len(delta['auto_decided'])} auto-decided")
    return 0


def cmd_inputs(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    run = load_run(home, args.id, include_archive=True)
    attempt = pick_attempt(run, args.stage, args.attempt)
    if attempt is None:
        raise CliError(f"no {args.stage} attempt matches")
    inputs = run.attempt_dir(attempt["stage"], attempt["n"]) / "inputs"
    manifest = intent.read_json(inputs / "manifest.json")
    if manifest is None:
        raise CliError(f"{attempt['stage']}-{attempt['n']} has no input snapshot (it predates snapshots)")
    if args.file:
        target = inputs / Path(args.file).name
        if not target.is_file():
            raise CliError(f"{args.file} was not an input of {attempt['stage']}-{attempt['n']}")
        print(target.read_text(encoding="utf-8"), end="")
        return 0
    outputs = intent.read_json(run.attempt_dir(attempt["stage"], attempt["n"]) / "outputs.json") or {"files": {}}
    out(f"{attempt['stage']}-{attempt['n']} started from revision {(manifest['source_revision'] or '-')[:12]} "
        f"with approved intent {(manifest['intent_sha256'] or 'none')[:12]}.")
    for name, info in manifest["files"].items():
        after = outputs["files"].get(name)
        change = "unchanged" if after and after["sha256"] == info["sha256"] else ("changed" if after else "removed")
        out(f"  {name:<28} {info['sha256'][:12]}  {change if outputs['files'] else 'no outputs recorded'}")
    for name in sorted(set(outputs["files"]) - set(manifest["files"])):
        out(f"  {name:<28} {'-':<12}  added")
    out(f"> factory inputs {run.id} --stage {attempt['stage']} --attempt {attempt['n']} --file spec.md")
    return 0


def pick_attempt(run: Run, stage: str | None, number: int | None) -> dict | None:
    attempts = [a for a in run.data["attempts"] if stage is None or a["stage"] == stage]
    if number is not None:
        attempts = [a for a in attempts if a["n"] == number]
    return attempts[-1] if attempts else None


def cmd_logs(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    run = load_run(home, args.id, include_archive=True)
    attempt = pick_attempt(run, args.stage, args.attempt)
    if attempt is None:
        out("No attempts match.")
        return 1
    if not args.follow:
        print_attempt(run, attempt, args.raw)
        return 0
    return follow(home, run, attempt, args.raw)


def attempt_header(attempt: dict) -> str:
    return (f"== {attempt['stage']}-{attempt['n']} {attempt.get('outcome') or 'running'} "
            f"({attempt['model']}/{attempt['effort']}) ==")


def print_attempt(run: Run, attempt: dict, raw: bool) -> None:
    out(attempt_header(attempt))
    stream = run.attempt_dir(attempt["stage"], attempt["n"]) / "stdout.jsonl"
    if attempt["host"] == "claude":
        out("interactive scope session; no captured stream")
    for line in hosts.render_stream(stream, raw=raw):
        out(line)
    if attempt.get("reason"):
        out(f"-- {attempt.get('outcome')}: {attempt['reason']}")


def follow(home: Path, run: Run, attempt: dict, raw: bool, poll: float = 1.0) -> int:
    key = (attempt["stage"], attempt["n"])
    position = 0
    out(attempt_header(attempt))
    while True:
        stream = run.attempt_dir(*key) / "stdout.jsonl"
        if stream.exists():
            with stream.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(position)
                chunk = handle.read()
                complete = chunk.rfind("\n") + 1
                for line in chunk[:complete].splitlines():
                    if raw:
                        out(line)
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rendered = hosts.render_event(event) if isinstance(event, dict) else None
                    if rendered:
                        out(rendered)
                position += len(chunk[:complete].encode("utf-8"))
        current = Run.load(run.dir)
        latest = current.last_attempt()
        if latest and (latest["stage"], latest["n"]) != key and latest.get("host") == "codex":
            finished = pick_attempt(current, key[0], key[1])
            if finished and finished.get("reason"):
                out(f"-- {finished.get('outcome')}: {finished['reason']}")
            key, position = (latest["stage"], latest["n"]), 0
            out(attempt_header(latest))
            continue
        if current.status in (DONE, NEEDS_HUMAN, CANCELLED):
            finished = pick_attempt(current, key[0], key[1])
            if finished and finished.get("reason"):
                out(f"-- {finished.get('outcome')}: {finished['reason']}")
            out(outcome_sentence(current, {"stale": False}))
            return 0
        time.sleep(poll)


# --- operator actions ---------------------------------------------------------------------

def emit(outcome: control.Outcome) -> None:
    for line in outcome.lines:
        out(line)


def cmd_retry(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    run_dir = resolve(home, args.id)
    since = len(events.read(run_dir))
    outcome = control.retry(home, cfg, run_dir, note=args.note, reset_budget=args.reset_budget, rescope=args.rescope)
    emit(outcome)
    if not outcome.started:
        return outcome.code
    if args.detach:
        out(f"> factory watch {outcome.run.id}")
        return 0
    return attach(cfg, outcome.run, since)


def cmd_resume(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    run_dir = resolve(home, args.id)
    since = len(events.read(run_dir))
    outcome = control.resume(home, cfg, run_dir)
    emit(outcome)
    if not outcome.started:
        return outcome.code
    if args.detach:
        out(f"> factory watch {outcome.run.id}")
        return 0
    return attach(cfg, outcome.run, since)


def cmd_cancel(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    emit(control.cancel(home, cfg, resolve(home, args.id)))
    return 0


def cmd_pause(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    run_dir = resolve(home, args.id)
    emit(control.withdraw_pause(run_dir) if args.undo else control.request_pause(home, cfg, run_dir))
    return 0


def cmd_note(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    emit(control.add_note(resolve(home, args.id), args.text))
    return 0


def pr_merged(run: Run) -> tuple[bool | None, str | None]:
    number = run.data["pr"].get("number")
    if not number:
        return None, "no pull request recorded"
    cwd = run.worktree if run.worktree.is_dir() else Path(run.data["repo"])
    code, stdout, stderr = gates.gh(["pr", "view", str(number), "--json", "number,state,mergedAt"], cwd)
    if code != 0:
        return None, f"gh pr view failed: {gates.first_lines(stderr or stdout, 2)}"
    try:
        info = json.loads(stdout)
    except json.JSONDecodeError:
        return None, "gh pr view returned invalid JSON"
    return info.get("state") == "MERGED" or bool(info.get("mergedAt")), None


def export_dir(home: Path, run_id: str) -> Path:
    return home / "exports" / run_id


def cmd_export(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    from runner import outcomes
    if args.verify:
        problems = outcomes.verify(Path(args.id))
        for problem in problems:
            out(f"  {problem}")
        out(f"{args.id}: {'verified' if not problems else f'{len(problems)} problem(s)'}")
        return 0 if not problems else 1
    run_dir = resolve(home, args.id)
    run = Run.load(run_dir)
    dest = Path(args.out).resolve() if args.out else export_dir(home, run.id)
    manifest = outcomes.export(run_dir, run, dest)
    bundle_digest = outcomes.digest(dest)
    out(f"Exported {len(manifest['files'])} files to {dest}")
    out(f"manifest SHA-256 {bundle_digest}")
    if args.comment:
        number = (run.data.get("pr") or {}).get("number")
        if not number:
            raise CliError(f"{run.id} has no pull request to link the bundle from")
        cwd = run.worktree if run.worktree.is_dir() else Path(run.data["repo"])
        body = dest / "pr-comment.md"
        body.write_text(outcomes.pr_comment(manifest, bundle_digest), encoding="utf-8")
        code, stdout, stderr = gates.gh(["pr", "comment", str(number), "--body-file", str(body)], cwd)
        if code != 0:
            raise CliError(f"gh pr comment failed: {gates.first_lines(stderr or stdout, 2)}")
        out(f"Linked the bundle from PR #{number}")
    return 0


def cmd_outcome(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    from runner import outcomes
    run_dir = resolve(home, args.id)
    run = Run.load(run_dir)
    if args.annotate:
        entry = outcomes.append(run_dir, {"kind": "annotation", "annotation": args.annotate, "note": args.note,
                                          "active_minutes": args.active_minutes, "eval_case": args.eval_case})
        out(f"Recorded {entry['annotation']} for {run.id}")
        return 0
    if args.active_minutes is not None:
        outcomes.append(run_dir, {"kind": "annotation", "annotation": None, "note": args.note,
                                  "active_minutes": args.active_minutes, "eval_case": args.eval_case})
    number = (run.data.get("pr") or {}).get("number")
    info, problem = None, "no pull request recorded"
    if number:
        cwd = run.worktree if run.worktree.is_dir() else Path(run.data["repo"])
        code, stdout, stderr = gates.gh(["pr", "view", str(number), "--json",
                                         "number,state,mergedAt,headRefOid,reviewDecision"], cwd)
        try:
            info, problem = (json.loads(stdout), None) if code == 0 else (None, gates.first_lines(stderr or stdout, 2))
        except json.JSONDecodeError:
            info, problem = None, "gh pr view returned invalid JSON"
    entry = outcomes.append(run_dir, outcomes.observation(run, info, problem))
    out(f"{run.id}: {entry['state']}" + (f" ({problem})" if problem else "")
        + ("; the PR head moved after completion" if entry["moved_since_completion"] else ""))
    for past in outcomes.read_outcomes(run_dir)[:-1]:
        label = past.get("state") if past["kind"] == "observation" else past.get("annotation") or "active time"
        out(f"  {past['at']}  {past['kind']:<11} {label}" + (f": {past['note']}" if past.get("note") else ""))
    return 0


def cmd_report(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    from runner import outcomes
    runs = [(d, r) for root in (home / "runs", home / "archive") for d, r, _ in dashboard.load_runs(root) if r]
    if args.by_policy:
        groups = outcomes.by_policy(runs)
        if args.json:
            out(json.dumps(groups, indent=2))
            return 0

        def ratio(entry: dict) -> str:
            return f"{entry['count']}/{entry['n']}" + (f" ({entry['rate']:.0%})" if entry["rate"] is not None else "")

        out(f"{len(groups)} foreman policy group(s); `unknown` is turns recorded before the policy hash")
        for sha, entry in groups.items():
            out(f"{sha[:12] if sha != 'unknown' else 'unknown':<12}  runs {entry['runs']}  turns {entry['turns']}  "
                f"intervention {ratio(entry['intervention'])}  override {ratio(entry['override'])}  "
                f"fallback {ratio(entry['fallback'])}")
        return 0
    summary = outcomes.summarize(runs)
    if args.json:
        out(json.dumps(summary, indent=2))
        return 0

    def rate(name: str) -> str:
        entry = summary[name]
        return f"{entry['count']}/{entry['n']}" + (f" ({entry['rate']:.0%})" if entry["rate"] is not None else "")

    active = summary["active_operator_minutes"]
    out(f"runs          {summary['runs']}")
    out(f"success       {rate('success')} of finished runs")
    if summary["legacy_done"]["count"]:
        out(f"legacy        {summary['legacy_done']['count']} done run(s) predate completion records; "
            "counted as historical, not reverified")
    out(f"intervention  {rate('intervention')} of runs (parked for a person, or annotated)")
    out(f"rework        {rate('rework')}   rejection {rate('rejection')}   regression {rate('regression')} of done runs")
    out(f"PR outcomes   {', '.join(f'{k} {v}' for k, v in sorted(summary['pr_states'].items())) or 'none'}")
    out(f"failures      {', '.join(f'{k} {v}' for k, v in summary['failure_categories'].items()) or 'none'}")
    out(f"tokens        {summary['tokens']}   cost {summary['cost']['amount']} over "
        f"{summary['cost']['attempts_priced']} priced attempts")
    out(f"operator time {active['total']:g} min measured on {active['runs_measured']} run(s); "
        f"unknown for {active['runs_unknown']}")
    out(f"scope elapsed {summary['elapsed_interactive_scope_seconds'] / 3600:.1f} h wall time (not active effort)")
    return 0


def cmd_history(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    from runner import history
    if args.history_command == "ls":
        return history_ls(home)
    with history.HistoryLock(home):
        if args.history_command == "build":
            return history_build(args, home)
        if args.history_command == "label":
            return history_label(args, home, cfg)
    raise CliError(f"unknown history command {args.history_command}", 2)


def history_ls(home: Path) -> int:
    from runner import history
    worlds = history.worlds(home)
    if not worlds:
        out("No worlds yet; `factory history build --all` builds them from finished runs.")
        return 0
    out(f"{len(worlds)} world(s) under {history.root(home)}")
    for world_dir in worlds:
        try:
            world = history.load_world(world_dir)
        except (OSError, json.JSONDecodeError, records.RecordError) as error:
            out(f"  {world_dir.name}  unreadable: {error}")
            continue
        labels = history.load_labels(world_dir)
        labelled = len((labels or {}).get("labels") or {})
        scorable = sum(1 for p in world["points"] if p["scorable"])
        out(f"  {world_dir.name}  {len(world['points'])} point(s), {scorable} scorable, {labelled} labelled, "
            f"{world['terminal_status']}, {world['repository']}")
    return 0


def history_build(args: argparse.Namespace, home: Path) -> int:
    from runner import history
    if not args.ids and not args.all:
        raise CliError("name run ids or pass --all", 2)
    dirs = history.run_dirs(home) if args.all else [resolve(home, ident, include_archive=True) for ident in args.ids]
    counts = {"built": 0, "unchanged": 0, "skipped": 0}
    for run_dir in dirs:
        result = history.build(run_dir, home)
        counts[result.status] += 1
        if result.status == "built":
            out(f"built {result.run_id}")
        for note in result.notes:
            out(f"  {note}")
    out(f"History: {counts['built']} built, {counts['unchanged']} unchanged, {counts['skipped']} skipped.")
    return 0


def find_world(home: Path, ident: str) -> Path:
    from runner import history
    path = history.root(home) / ident
    if not (path / "world.json").is_file():
        matches = [w for w in history.worlds(home) if w.name.startswith(ident)]
        if len(matches) != 1:
            raise CliError(f"no single world matches {ident!r}; `factory history ls` lists them", 1)
        path = matches[0]
    return path


def history_label(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    if args.set:
        return label_by_hand(args, home)
    if args.check_cases:
        return label_check_cases(home)
    return label_worlds(args, home, cfg)


def label_by_hand(args: argparse.Namespace, home: Path) -> int:
    from runner import history
    ident, point = args.set
    if not args.accept:
        raise CliError("--set needs --accept with at least one action", 2)
    try:
        entry = history.set_label(find_world(home, ident), point, args.accept, args.reject, args.allow_override,
                                  args.note)
    except history.LabelError as error:
        raise CliError(str(error), 1) from error
    out(f"Labelled {ident} {point} by hand: accept {', '.join(entry['accept'])}"
        + (f"; reject {', '.join(entry['reject'])}" if entry["reject"] else ""))
    return 0


def label_check_cases(home: Path) -> int:
    from runner import history
    report = history.check_hand_cases(home)
    out(f"Hand cases: {report['cases']} total, {len(report['matched'])} matched, "
        f"{len(report['unmatched'])} unmatched, {len(report['unlabelled'])} matched but unlabelled.")
    for name in report["unmatched"]:
        out(f"  unmatched {name}: no world point for its run, stage, and attempt yet")
    for line in report["disagreements"]:
        out(f"  disagreement {line}")
    out(f"mean accept-set size {report['mean_accept_size'] if report['mean_accept_size'] is not None else '-'}")
    return 0


def label_worlds(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    from runner import history
    if not args.ids and not args.all:
        raise CliError("name world ids or pass --all", 2)
    dirs = history.worlds(home) if args.all else [find_world(home, ident) for ident in args.ids]
    failed = []
    for path in dirs:
        result = history.label(path, cfg, relabel=args.relabel)
        if result.status == "failed":
            failed.append(result.run_id)
            out(f"failed {result.run_id}: stays unlabelled and is excluded from scoring")
            for problem in result.problems:
                out(f"  {problem}")
        else:
            out(f"{result.status} {result.run_id}" + (f" ({result.kept_human} human label(s) kept)"
                                                       if result.kept_human else ""))
    if failed:
        out(f"Labelling failed for {len(failed)} world(s): {', '.join(failed)}")
        return 1
    return 0


DEPLOY_FAILURES = ("unreplayed", "dirty", "branch", "unpushed_commits", "stale_plugins", "incumbent_changed",
                   "build_failed", "validate", "commit_failed", "push_failed")


def cmd_dream(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    """Score the foreman skill on history, ask for revisions, and deploy a winner that clears every gate. Paid."""
    from runner import dream, history
    check_dream_args(args, home)
    with history.HistoryLock(home):
        _, decision = dream.run(home, cfg, rounds=args.rounds, repeats=args.repeats, max_calls=args.max_calls,
                                deploy_enabled=not args.no_deploy, world_ids=args.worlds, echo=out)
    out(f"decision {decision['reason']}" + (f": {decision['cause']}" if decision.get("cause") else ""))
    return 1 if decision["reason"] in DEPLOY_FAILURES else 0


def check_dream_args(args: argparse.Namespace, home: Path) -> None:
    from runner import history
    if args.repeats is not None and args.repeats < 1:
        raise CliError(f"--repeats must be at least 1, got {args.repeats}", 2)
    if args.rounds < 0:
        raise CliError(f"--rounds must be 0 or more, got {args.rounds}", 2)
    if args.max_calls < 1:
        raise CliError(f"--max-calls must be at least 1, got {args.max_calls}", 2)
    known = [w.name for w in history.worlds(home)]
    unknown = [w for w in args.worlds or [] if w not in known]
    if unknown:
        raise CliError(f"unknown world(s) {', '.join(unknown)}; known: {', '.join(known) or 'none yet'}", 2)


def cmd_gc(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    archived, kept = [], []
    for run_dir, run, error in dashboard.load_runs(home / "runs"):
        if run is None or run.status != DONE:
            continue
        merged, problem = pr_merged(run)
        if not merged:
            kept.append((run.id, problem or f"PR #{run.data['pr'].get('number')} is not merged"))
            continue
        if args.dry_run:
            archived.append(run.id)
            continue
        lock = supervise.WorkerLock(run_dir)
        if not lock.acquire():
            kept.append((run.id, "a worker holds the lock"))
            continue
        try:
            plan_dir = run.plan_dir
            if plan_dir.is_dir():
                shutil.copytree(plan_dir, run_dir / "plan", dirs_exist_ok=True)
            from runner import outcomes
            outcomes.export(run_dir, run, export_dir(home, run.id))
            actions = wt.remove(Path(run.data["repo"]), run.worktree, run.data.get("branch"))
            run.data["archived"] = True
            run.data["pr"]["merged"] = True
            run.transition("archive")
            run.save()
            run.event("gc.archived", run.stage, None, actions=actions)
            for transient in ("scratch", "cancel", "worker.pid", "worker.heartbeat"):
                path = run_dir / transient
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                elif path.exists():
                    path.unlink()
            target = home / "archive" / run.id
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(run_dir), str(target))
            (target / "worker.lock").unlink(missing_ok=True)
            archived.append(run.id)
        finally:
            lock.release()
    verb = "Would archive" if args.dry_run else "Archived"
    out(f"{verb} {len(archived)} merged run(s); kept {len(kept)} completed run(s).")
    for run_id in archived:
        out(f"  {'would archive' if args.dry_run else 'archived'} {run_id}")
    for run_id, why in kept:
        out(f"  kept {run_id}: {why}")
    if archived and not args.dry_run:
        refresh(home, cfg)
    return 0


def cmd_rm(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    if not args.force:
        raise CliError("factory rm removes a run and its worktree regardless of PR state; pass --force", 2)
    run_dir = resolve(home, args.id)
    lock = supervise.WorkerLock(run_dir)
    if not lock.acquire():
        raise CliError(f"a live worker owns {run_dir.name}; `factory cancel {run_dir.name}` first")
    try:
        try:
            execution_live = executor.live(run_dir)
        except executor.Ambiguous as error:
            raise CliError(f"not removed: {error}") from error
        if execution_live:
            raise CliError(f"an execution of {run_dir.name} is still live; `factory cancel {run_dir.name}` first")
        try:
            run = Run.load(run_dir)
            repo, branch = Path(run.data["repo"]), run.data.get("branch")
        except (SchemaError, OSError):
            run, repo, branch = None, None, None
        worktree = run_dir / "worktree"
        out(f"Removing run {run_dir.name}")
        out(f"  run directory {run_dir}")
        if worktree.exists():
            out(f"  worktree      {worktree}")
        if branch:
            out(f"  local branch  {branch} (remote branch and pull request are untouched)")
        if repo is not None and repo.is_dir():
            wt.remove(repo, worktree, branch)
        elif worktree.exists():
            shutil.rmtree(worktree, ignore_errors=True)
    finally:
        lock.release()
    if not wt.path_is_within(run_dir, home / "runs"):
        raise CliError(f"refusing to delete {run_dir}: not under {home / 'runs'}", 2)
    shutil.rmtree(run_dir)
    refresh(home, cfg)
    out(f"Removed {run_dir.name}.")
    return 0


def cmd_dashboard(args: argparse.Namespace, home: Path, cfg: config.Config) -> int:
    path = dashboard.regenerate(home, heartbeat_seconds=cfg.heartbeat_seconds)
    if path is None:
        raise CliError("dashboard.lock stayed busy; try again")
    out(f"Dashboard written: {path}")
    if args.open:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run([opener, str(path)], check=False)
    return 0


# --- doctor --------------------------------------------------------------------------------

def doctor_checks(home: Path) -> list[dict]:
    results: list[dict] = []

    def add(name: str, status: str, detail: str, repair: str | None = None) -> None:
        results.append({"check": name, "status": status, "detail": detail, "repair": repair})

    ok = sys.version_info >= (3, 10)
    add("python", "ok" if ok else "fail", sys.version.split()[0], None if ok else "install Python 3.10 or newer")
    for name in ("git", "codex", "claude", "gh"):
        path = which(name)
        add(f"binary:{name}", "ok" if path else "fail", path or f"{config.binary(name)} not found",
            None if path else f"install {name} or set FACTORY_{name.upper()}_BIN")
    if which("codex"):
        failure = check_codex_plugin()
        bundles = provenance.skill_bundles()
        if failure:
            add("codex:plugin", "fail", failure[0], failure[1])
        elif bundles.get("fallback"):
            add("codex:plugin", "warn", f"{bundles['fallback']}; attempts read the generated skills by path instead",
                f"codex plugin marketplace add {FACTORY_ROOT.parent} && codex plugin add factory@nurbot")
        else:
            add("codex:plugin", "ok", f"factory@nurbot installed; Codex loads {bundles['skills_id']}")
        cached = intent.read_json(home / "capabilities.json") or {}
        for stage in ("scope-review", "build", "ship"):
            model = PIPELINE[stage].model
            seen = any(entry.get("model") == model and entry.get("ok") for entry in cached.values())
            add(f"codex:model:{stage}", "ok" if seen else "warn",
                f"{model} answered a cached smoke check" if seen else
                f"{model} is unchecked; a real call costs tokens",
                None if seen else "factory doctor --smoke")
    acli = which("acli")
    add("binary:acli", "ok" if acli else "warn", acli or "not found; only `factory new --jira` needs it",
        None if acli else "install the Atlassian CLI (acli) to start runs from Jira tickets")
    chrome = browser.find(dict(os.environ))
    add("browser", "ok" if chrome else "warn",
        f"{chrome} hosted per attempt over CDP" if chrome else "no Chrome or Chromium found; frontend e2e has no browser",
        None if chrome else "agent-browser install, or npx playwright install chromium, or set FACTORY_BROWSER_BIN")
    failure = check_claude_plugin()
    add("claude:plugin", "fail" if failure else "ok", failure[0] if failure else str(FACTORY_ROOT),
        failure[1] if failure else None)
    if which("gh"):
        failure = check_gh_auth()
        add("gh:auth", "fail" if failure else "ok", failure[0] if failure else "authenticated",
            failure[1] if failure else None)
    try:
        cfg = config.load(home)
        add("config", "ok", str(home / "config.json") + ("" if (home / "config.json").exists() else " (defaults)"))
    except config.ConfigError as error:
        cfg = None
        add("config", "fail", str(error), f"fix or remove {home / 'config.json'}")
    try:
        config.ensure_home(home)
        with tempfile.NamedTemporaryFile("w", dir=home, delete=False) as handle:
            handle.write("probe")
        os.replace(handle.name, home / ".doctor-probe")
        (home / ".doctor-probe").unlink()
        add("runtime", "ok", f"{home} writable with atomic rename")
    except OSError as error:
        add("runtime", "fail", f"{home}: {error}", f"mkdir -p {home} && chmod u+rwx {home}")
    try:
        first, second = supervise.WorkerLock(home), supervise.WorkerLock(home)
        first.path = second.path = home / ".doctor.lock"
        works = first.acquire() and not second.acquire()
        first.release()
        second.release()
        (home / ".doctor.lock").unlink(missing_ok=True)
        add("flock", "ok" if works else "fail",
            "exclusive non-blocking flock works" if works else "a second flock was not refused",
            None if works else "use a local filesystem for FACTORY_HOME")
    except OSError as error:
        add("flock", "fail", str(error), "use a local filesystem for FACTORY_HOME")
    if cfg is not None:
        held = slots.holders(home / "slots")
        extra = sorted(i for i in held if i >= cfg.max_concurrent_stages)
        detail = f"{len(held)}/{cfg.max_concurrent_stages} slot(s) held"
        add("slots", "warn" if extra else "ok", detail + (f"; slots {extra} above the limit are still held" if extra else ""))
        for row in dashboard.summaries(home, heartbeat_seconds=cfg.heartbeat_seconds):
            if row.get("stale"):
                add(f"worker:{row['id']}", "warn", f"{row['status']} with no live worker or a stale heartbeat",
                    f"factory resume {row['id']}")
            if row["status"] == "invalid":
                add(f"run:{row['id']}", "fail", row.get("blocker") or "unreadable run.json",
                    f"inspect {home / 'runs' / row['id'] / 'run.json'}")
    repos: set[str] = set()
    for run_dir, run, _ in dashboard.load_runs(home / "runs"):
        if run is None:
            continue
        repos.add(run.data["repo"])
        if run.status == NEW:
            continue
        repo = Path(run.data["repo"])
        if not repo.is_dir():
            add(f"worktree:{run.id}", "warn", f"repository {repo} is gone", f"factory rm {run.id} --force")
            continue
        registered = run.worktree.resolve() in wt.registered_worktrees(repo)
        if not run.worktree.is_dir() or not registered:
            add(f"worktree:{run.id}", "fail", f"{run.worktree} is missing or not registered with {repo}",
                f"git -C {repo} worktree prune && factory rm {run.id} --force")
    if cfg is not None:
        repos.update(cfg.repos)
    for repo in sorted(repos):
        try:
            exclude = wt.common_dir(Path(repo)) / "info"
            writable = os.access(exclude if exclude.exists() else exclude.parent, os.W_OK)
            add(f"excludes:{repo}", "ok" if writable else "fail", str(exclude),
                None if writable else f"chmod u+w {exclude}")
        except wt.GitError as error:
            add(f"excludes:{repo}", "warn", str(error))
    try:
        dashboard.render(dashboard.summaries(home, heartbeat_seconds=cfg.heartbeat_seconds if cfg else 30))
        add("dashboard", "ok", "renders")
    except Exception as error:  # noqa: BLE001
        add("dashboard", "fail", f"render failed: {error}")
    return results


SMOKE_TOKEN = "FACTORY-SMOKE-OK"


def repo_checks(home: Path, repo_arg: str, *, remote: str, run_setup: bool, smoke: bool) -> list[dict]:
    """Whether unattended stages can really work in this repository, checked through the runner's own paths."""
    results: list[dict] = []

    def add(name: str, status: str, detail: str, repair: str | None = None) -> None:
        results.append({"check": f"repo:{name}", "status": status, "detail": detail, "repair": repair})

    try:
        root = wt.toplevel(repo_arg)
        wt.require_remote(root, remote)
    except wt.GitError as error:
        add("git", "fail", str(error), error.repair)
        return results
    add("git", "ok", f"{root} with remote {remote}")
    _, tracked = tracked_plans(root, remote, None, None)
    if tracked:
        add("plans", "warn", f"the base tracks plan files under .dev/: {listed(tracked)}; runs leave them alone, but "
            "a run whose plan name matches one of these directories is refused", TRACKED_PLANS_REPAIR)
    contract_file = root / records.CONTRACT_PATH
    try:
        contract = records.load_contract(contract_file)
    except records.RecordError as error:
        add("contract", "fail", f"{error} [{error.code}]",
            "record the repository's checks in .factory/contract.json (factory/references/repo-contract.md)")
        contract = None
    if contract is not None:
        ci = contract["ci"]
        add("contract", "ok", f"{len(contract['validation'])} validation command(s); CI "
            + (f"requires {', '.join(ci['required_checks'])}" if "required_checks" in ci else f"none: {ci['none']}"))
        names = set(contract.get("environment", {}).get("required", []))
        for section in ("setup", "validation", "services"):
            for command in contract.get(section, []):
                names |= set(command.get("env", []))
        missing = sorted(name for name in names if not os.environ.get(name))
        add("environment", "fail" if missing else "ok",
            f"unset: {', '.join(missing)}" if missing else f"{len(names)} required name(s) set",
            f"export {' '.join(missing)} in the shell that runs `factory`" if missing else None)
        for section in ("setup", "validation", "services"):
            for command in contract.get(section, []):
                if "script" in command:
                    present = (root / command["script"]).is_file()
                    add(f"command:{command['id']}", "ok" if present else "fail",
                        command["script"] + (" exists" if present else " does not exist"),
                        None if present else f"commit {command['script']}")
                else:
                    tool = command["run"][0]
                    found = shutil.which(tool) or (root / tool).is_file()
                    add(f"command:{command['id']}", "ok" if found else "fail",
                        f"{tool} {'found' if found else 'not found on PATH'}",
                        None if found else f"install {tool} for the runner's user")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or hosts.github_token(dict(os.environ))
    add("gh:token-forwarding", "ok" if token else "fail",
        "a GitHub token reaches sandboxed attempts" if token else "no token for GH_TOKEN; gh inside the sandbox gets HTTP 401",
        None if token else "gh auth login, or export GH_TOKEN")
    probe_root = home / "doctor" / uuid.uuid4().hex[:12]
    linked = probe_root / "worktree"
    branch = f"factory-doctor/{probe_root.name}"
    try:
        probe_root.mkdir(parents=True)
        base = wt.resolve_base(root, remote, wt.default_branch(root, remote))
        wt.git("-C", str(root), "worktree", "add", "--quiet", "-b", branch, str(linked), base)
        wt.configure_upstream(linked, remote, branch)
        (wt.common_dir(root) / "logs").mkdir(exist_ok=True)
        grants = [linked, *wt.sandbox_git_dirs(linked)]
        (linked / ".factory-doctor-probe").write_text("probe\n", encoding="utf-8")
        steps = [["git", "add", ".factory-doctor-probe"],
                 ["git", "-c", "user.name=factory doctor", "-c", "user.email=doctor@factory.invalid",
                  "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "factory doctor probe"]]
        failure = None
        try:
            for step in steps:
                result = subprocess.run(commands.sandbox_wrap(step, "workspace-write", writable=grants), cwd=linked,
                                        capture_output=True, text=True, timeout=120)
                if result.returncode != 0:
                    failure = f"`{' '.join(step[:2])}` failed in the sandbox: {gates.first_lines(result.stderr, 2)}"
                    break
        except commands.CommandError as error:
            add("worktree-git", "warn", f"sandbox enforcement unavailable: {error}", error.repair)
        else:
            add("worktree-git", "fail" if failure else "ok",
                failure or "a linked worktree commits under the narrowed sandbox grants",
                "check that the repository's .git directory is on a local, writable filesystem" if failure else None)
        if run_setup and contract is not None:
            ctx = gates.GateContext(worktree=linked, plan="doctor", stage="doctor", report_dir=probe_root,
                                    attempt_dir=probe_root / "attempt", run_dir=probe_root)
            for command in contract.get("setup", []):
                exec_dir = gates.next_exec_dir(ctx, f"setup-{command['id']}")
                mode = commands.boundary(command, contract)
                try:
                    parts = commands.argv(command, linked)
                    env = commands.environment(command, contract, dict(os.environ))
                    wrapped, env = commands.confine(parts, mode, writable=[linked, exec_dir], env=env)
                    cwd = commands.resolve_inside(linked, command.get("cwd", "."), "cwd")
                except commands.CommandError as error:
                    add(f"setup:{command['id']}", "fail", str(error), error.repair)
                    continue
                receipt = gates.execute(ctx, wrapped, label=f"setup-{command['id']}", kind="setup", cwd=cwd, env=env,
                                        timeout_cap=commands.timeout_s(command), boundary=mode, exec_dir=exec_dir)
                ok = receipt.classification == "succeeded"
                add(f"setup:{command['id']}", "ok" if ok else "fail",
                    f"{shlex.join(parts)} {gates.describe(receipt)} in the {mode} boundary"
                    + ("" if ok else f": {gates.first_lines(gates.output_of(receipt)[-1500:], 2)}"),
                    None if ok else "fix the setup command in .factory/contract.json or the environment it needs")
    except (wt.GitError, OSError) as error:
        add("worktree-git", "fail", f"could not create a probe worktree: {error}", "git worktree prune")
    finally:
        if linked.exists():
            wt.git("-C", str(root), "worktree", "remove", "--force", str(linked), check=False)
        wt.git("-C", str(root), "branch", "-D", branch, check=False)
        shutil.rmtree(probe_root, ignore_errors=True)
    if smoke:
        results.extend(smoke_checks(home))
    return results


def smoke_checks(home: Path) -> list[dict]:
    """One real, paid Codex call per headless model: does `$factory:{stage}` resolve and does the model answer?

    Successful checks are cached by Codex version, skills identity, model, and sandbox mode.
    """
    results: list[dict] = []
    cache_path = home / "capabilities.json"
    cache = intent.read_json(cache_path) or {}
    bundles = provenance.skill_bundles()
    codex_version = provenance.host_versions().get("codex")
    for stage_name in ("scope-review", "build"):
        stage = PIPELINE[stage_name]
        key = records.canonical_sha256([codex_version, bundles["skills_id"], stage.model, stage.effort, "workspace-write"])
        name = f"codex:smoke:{stage.model}"
        if cache.get(key, {}).get("ok"):
            results.append({"check": name, "status": "ok", "detail": f"cached from {cache[key]['at']}", "repair": None})
            continue
        with tempfile.TemporaryDirectory(prefix="factory-smoke-") as tmp:
            last = Path(tmp) / "last.md"
            prompt = (f"$factory:{stage_name}\n\nThis is a factory capability smoke test. Do not run any tool and "
                      f"do not follow the skill. Reply with exactly `{SMOKE_TOKEN} <the skill's name field>`.\n")
            argv = hosts.codex_argv(prompt=prompt, model=stage.model, effort="low", sandbox="workspace-write",
                                    worktree=Path(tmp), writable=[], last_message=last)
            code, output = provenance.run_quiet(argv, timeout=300)
            answer = last.read_text(encoding="utf-8").strip() if last.is_file() else ""
        ok = code == 0 and answer.startswith(f"{SMOKE_TOKEN} {stage_name}")
        if ok:
            cache[key] = {"ok": True, "at": utc_now(), "model": stage.model, "codex": codex_version,
                          "skills_id": bundles["skills_id"]}
            intent.write_json(cache_path, cache)
        results.append({"check": name, "status": "ok" if ok else "fail",
                        "detail": f"answered {answer[:80]!r}" if ok else f"exit {code}: {answer[:80] or gates.first_lines(output, 2)}",
                        "repair": None if ok else f"codex exec -m {stage.model} '$factory:{stage_name}'"})
    return results


def cmd_doctor(args: argparse.Namespace, home: Path, cfg: config.Config | None) -> int:
    results = doctor_checks(home)
    if args.repo:
        results += repo_checks(home, args.repo, remote=args.remote, run_setup=args.run_setup, smoke=args.smoke)
    elif args.smoke:
        results += smoke_checks(home)
    failed = [r for r in results if r["status"] == "fail"]
    warned = [r for r in results if r["status"] == "warn"]
    if args.json:
        out(json.dumps({"ok": not failed, "checks": results}, indent=2))
        return 1 if failed else 0
    out("A new run is unsafe: fix the failures below." if failed else
        f"Ready: {len(results)} checks, {len(warned)} warning(s).")
    for row in results:
        if row["status"] == "ok" and not args.verbose:
            continue
        out(f"  [{row['status']}] {row['check']}: {row['detail']}")
        if row.get("repair"):
            out(f"         repair: {row['repair']}")
    return 1 if failed else 0


# --- entry -----------------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="factory", description="Turn a change request into a ready pull request.")
    sub = root.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="create a run, scope it interactively, and hand it off")
    new.add_argument("repo")
    new.add_argument("request", nargs="?", help="the change request as a quoted string")
    new.add_argument("--file", help="read the change request from a text or Markdown file")
    new.add_argument("--jira", metavar="KEY", help="fetch the change request from a Jira ticket via acli")
    new.add_argument("--plan")
    new.add_argument("--base")
    new.add_argument("--remote", default="origin")
    new.add_argument("--yes", action="store_true")
    new.add_argument("--detach", action="store_true", help="return after the handoff instead of watching the run")
    new.set_defaults(func=cmd_new)

    scope = sub.add_parser("scope", help="relaunch interactive scope and offer the handoff again")
    scope.add_argument("id")
    scope.add_argument("--resume", action="store_true")
    scope.add_argument("--yes", action="store_true")
    scope.add_argument("--detach", action="store_true", help="return after the handoff instead of watching the run")
    scope.set_defaults(func=cmd_scope)

    ls = sub.add_parser("ls", help="list runs, actionable first")
    ls.add_argument("--all", action="store_true")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_ls)

    show = sub.add_parser("show", help="show one run in detail")
    show.add_argument("id")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=cmd_show)

    logs = sub.add_parser("logs", help="render an attempt's Codex events")
    logs.add_argument("id")
    logs.add_argument("--stage", choices=list(PIPELINE))
    logs.add_argument("--attempt", type=int)
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("--raw", action="store_true")
    logs.set_defaults(func=cmd_logs)

    retry = sub.add_parser("retry", help="queue a new attempt for a parked run")
    retry.add_argument("id")
    retry.add_argument("--reset-budget", action="store_true")
    retry.add_argument("--rescope", action="store_true",
                       help="reopen interactive scope to change the approved intent, then hand off again")
    retry.add_argument("--note")
    retry.add_argument("--detach", action="store_true", help="return after queueing instead of watching the run")
    retry.set_defaults(func=cmd_retry)

    resume = sub.add_parser("resume", help="continue a paused run, or recover one whose worker died")
    resume.add_argument("id")
    resume.add_argument("--detach", action="store_true", help="return after resuming instead of watching the run")
    resume.set_defaults(func=cmd_resume)

    pause = sub.add_parser("pause", help="pause a run before its next attempt starts")
    pause.add_argument("id")
    pause.add_argument("--undo", action="store_true", help="withdraw a pause that has not taken effect yet")
    pause.set_defaults(func=cmd_pause)

    note = sub.add_parser("note", help="leave a note for the run's next attempt")
    note.add_argument("id")
    note.add_argument("text")
    note.set_defaults(func=cmd_note)

    boss = sub.add_parser("foreman", help="show the foreman session, its decisions, or one turn in full")
    boss.add_argument("id")
    boss.add_argument("--turn", type=int, help="print one turn's prompt, decision, and record")
    boss.add_argument("--json", action="store_true")
    boss.set_defaults(func=cmd_foreman)

    shown = sub.add_parser("intent", help="show the approved intent, added scenarios, and decision deltas")
    shown.add_argument("id")
    shown.add_argument("--json", action="store_true")
    shown.set_defaults(func=cmd_intent)

    inputs = sub.add_parser("inputs", help="show exactly which plan files an attempt started from")
    inputs.add_argument("id")
    inputs.add_argument("--stage", choices=list(PIPELINE), required=True)
    inputs.add_argument("--attempt", type=int)
    inputs.add_argument("--file", help="print one snapshotted input file")
    inputs.set_defaults(func=cmd_inputs)

    watcher = sub.add_parser("watch", help="attach to a run and follow the assembly line live")
    watcher.add_argument("id")
    watcher.set_defaults(func=cmd_watch)

    cancel = sub.add_parser("cancel", help="cancel a run")
    cancel.add_argument("id")
    cancel.set_defaults(func=cmd_cancel)

    gc = sub.add_parser("gc", help="archive completed runs whose pull request merged")
    gc.add_argument("--dry-run", action="store_true")
    gc.set_defaults(func=cmd_gc)

    exporter = sub.add_parser("export", help="write a portable, checksummed evidence bundle for a run")
    exporter.add_argument("id", help="a run id, or a bundle directory with --verify")
    exporter.add_argument("--out", help="bundle directory (default ~/.factory/exports/{run-id})")
    exporter.add_argument("--verify", action="store_true", help="check a bundle's files against its manifest")
    exporter.add_argument("--comment", action="store_true", help="link the bundle from the run's pull request")
    exporter.set_defaults(func=cmd_export)

    outcome = sub.add_parser("outcome", help="record the PR's current state, or annotate what happened after it")
    outcome.add_argument("id")
    outcome.add_argument("--annotate", choices=("rework", "rejection", "regression", "intervention"))
    outcome.add_argument("--note")
    outcome.add_argument("--active-minutes", type=float, help="measured active operator time to record")
    outcome.add_argument("--eval-case", help="the eval or contract case this outcome led to")
    outcome.set_defaults(func=cmd_outcome)

    report = sub.add_parser("report", help="summarize outcomes across runs, with sample sizes")
    report.add_argument("--json", action="store_true")
    report.add_argument("--by-policy", action="store_true",
                        help="group intervention, override, and fallback rates by the foreman policy hash")
    report.set_defaults(func=cmd_report)

    hist = sub.add_parser("history", help="turn finished runs into replay worlds for the dream loop")
    hist_sub = hist.add_subparsers(dest="history_command", required=True)
    hist_build = hist_sub.add_parser("build", help="build or refresh the world of each finished run")
    hist_build.add_argument("ids", nargs="*", help="run ids (default: none; use --all)")
    hist_build.add_argument("--all", action="store_true", help="every run under runs/ and archive/")
    hist_sub.add_parser("ls", help="list built worlds with their points and labels")
    hist_label = hist_sub.add_parser("label", help="label worlds with hindsight (paid), or record a label by hand")
    hist_label.add_argument("ids", nargs="*", help="world (run) ids")
    hist_label.add_argument("--all", action="store_true", help="every built world")
    hist_label.add_argument("--relabel", action="store_true", help="regenerate model labels; human labels stay")
    hist_label.add_argument("--check-cases", action="store_true",
                            help="compare labels with the hand-labelled eval cases")
    hist_label.add_argument("--set", nargs=2, metavar=("ID", "POINT"), help="record a human label for one point")
    hist_label.add_argument("--accept", nargs="+", help="with --set: actions a correct foreman may choose")
    hist_label.add_argument("--reject", nargs="*", default=[], help="with --set: actions that would be wrong")
    hist_label.add_argument("--allow-override", action="store_true", help="with --set: an override is justified")
    hist_label.add_argument("--note", help="with --set: why")
    hist.set_defaults(func=cmd_history)

    dreamer = sub.add_parser("dream", help="improve the foreman skill offline from history and deploy a winner (paid)")
    dreamer.add_argument("--rounds", type=int, default=3, help="revisions to ask for (default 3)")
    dreamer.add_argument("--repeats", type=int,
                         help="replays per point; overrides both defaults (1 on selection, 2 on confirmation)")
    dreamer.add_argument("--max-calls", type=int, default=1000, help="cap on fresh replay calls (default 1000)")
    dreamer.add_argument("--no-deploy", action="store_true", help="report and keep candidates, never commit")
    dreamer.add_argument("--worlds", nargs="+", help="dream over these world ids only")
    dreamer.set_defaults(func=cmd_dream)

    rm = sub.add_parser("rm", help="remove one run and its worktree")
    rm.add_argument("id")
    rm.add_argument("--force", action="store_true")
    rm.set_defaults(func=cmd_rm)

    board = sub.add_parser("dashboard", help="regenerate the static dashboard")
    board.add_argument("--open", action="store_true")
    board.set_defaults(func=cmd_dashboard)

    doctor = sub.add_parser("doctor", help="check that new runs are safe")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("-v", "--verbose", action="store_true")
    doctor.add_argument("--repo", help="also check that unattended stages can work in this repository")
    doctor.add_argument("--remote", default="origin", help="the repository remote runs use (with --repo)")
    doctor.add_argument("--run-setup", action="store_true",
                        help="with --repo: run the contract's setup commands in a throwaway worktree")
    doctor.add_argument("--smoke", action="store_true",
                        help="make one real Codex call per headless model to confirm skill and model resolution "
                             "(paid; cached per Codex version, skills, and model)")
    doctor.set_defaults(func=cmd_doctor)

    worker = sub.add_parser("_worker")
    worker.add_argument("id")
    worker.set_defaults(func=None)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "_worker":
        from runner.worker import main as worker_main
        return worker_main(args.id)
    home = config.factory_home()
    try:
        if args.command == "doctor":
            return cmd_doctor(args, home, None)
        cfg = config.load(home)
        config.ensure_home(home)
        return args.func(args, home, cfg)
    except config.ConfigError as error:
        print(f"Config error: {error}", file=sys.stderr)
        return 2
    except HistoryLocked as error:
        print(str(error), file=sys.stderr)
        return 3
    except (CliError, control.ControlError, InvalidTransition, wt.GitError) as error:
        print(str(error), file=sys.stderr)
        return getattr(error, "code", 1)
    except KeyboardInterrupt:
        return 130
