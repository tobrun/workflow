"""Deterministic stage gates and the skill-result merge rule.

A gate reads files, git, commands, reports, and GitHub state. Model prose never
decides an outcome. Each gate returns the first failing check (scope returns
all of them, because a human is present to fix them in one pass).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from runner import FACTORY_ROOT, checkpoints, commands, executor, intent, publication, records, resources
from runner import worktree as wt
from runner.config import binary

LINT_SPEC = FACTORY_ROOT / "skills" / "scope" / "scripts" / "lint-spec.py"
PR_EVIDENCE = FACTORY_ROOT / "skills" / "ship" / "scripts" / "pr-evidence.py"

SCRIPT_TIMEOUT_S = 10 * 60
GH_TIMEOUT_S = 2 * 60
FETCH_TIMEOUT_S = 10 * 60
REVIEW_VERDICTS = ("APPROVED", "APPROVED WITH DEFERRALS")
SHIP_MANDATORY_LENSES = ("simplify", "spec-conformance")
SHIP_VERDICTS = ("PASS", "CONCERNS", "BLOCK")
DEFERRAL_KINDS = ("premise", "new-effort", "unanswered")


@dataclass
class GateResult:
    passed: bool
    outcome: str = "done"
    reason: str | None = None
    retryable: bool = True
    precondition: bool = False
    failures: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)
    code: str | None = None
    # Hygiene the gate noticed but that does not make the evidence wrong; surfaced, never blocking.
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "passed": self.passed,
            "outcome": self.outcome,
            "reason": self.reason,
            "retryable": self.retryable,
            "precondition": self.precondition,
            "failures": self.failures,
            "data": self.data,
            "code": self.code,
            "warnings": self.warnings,
        }


def passed(**data: object) -> GateResult:
    return GateResult(True, "done", None, True, False, [], dict(data))


def reports_warnings(gate):
    """A stage gate whose result carries every hygiene warning its checks left on the context."""
    def wrapped(ctx: GateContext) -> GateResult:
        result = gate(ctx)
        result.warnings = [*ctx.warnings, *(w for w in result.warnings if w not in ctx.warnings)]
        return result
    wrapped.__name__, wrapped.__doc__ = gate.__name__, gate.__doc__
    return wrapped


def blocked(reason: str, *, retryable: bool = True, outcome: str = "blocked", precondition: bool = False,
            code: str = "gate.blocked", **data: object) -> GateResult:
    """A failing gate with a stable failure code; the reason is for people, the code is for the runner."""
    return GateResult(False, outcome, reason, retryable and not precondition, precondition, [reason], dict(data),
                      code)


def stopped(stop: str, what: str, **data: object) -> GateResult:
    cancelled = stop == "cancelled by operator"
    return blocked(f"{stop} before {what}", retryable=not cancelled,
                   code="attempt.cancelled" if cancelled else "attempt.deadline", **data)


@dataclass
class GateContext:
    """What a gate needs from the run, independent of the worker."""
    worktree: Path
    plan: str
    stage: str
    report_dir: Path
    attempt_dir: Path
    remote: str = "origin"
    branch: str | None = None
    base_sha: str | None = None
    base: str | None = None
    baseline: dict = field(default_factory=dict)
    deadline: float | None = None  # epoch seconds; the attempt's shared deadline
    poll_seconds: float = 10
    sleep: Callable[[float], None] = time.sleep
    cancel_requested: Callable[[], bool] = lambda: False
    run_dir: Path | None = None
    cancel_file: Path | None = None
    grace: float = 30
    inherit_fds: list[int] = field(default_factory=list)
    base_env: dict | None = None
    executions: list[dict] = field(default_factory=list)
    intent_sha256: str | None = None
    require_intent: bool = False
    attempt: int = 0
    home: Path | None = None
    max_heavy_commands: int | None = None
    port_range: tuple = (20000, 29999)
    # The worker passes the configured value; a context built without a config hosts no browser.
    browser: str = "off"
    checkpoint_log: list[dict] = field(default_factory=list)
    # Collected by checks that find hygiene problems (tracked plan files, a test mapped outside its layer's
    # globs, a declared setup output that never appeared): reported with the outcome, never a failure.
    warnings: list[str] = field(default_factory=list)
    _runner_sha: str | None = None

    def runner_sha(self) -> str:
        """The runner's own content hash: a change to the tools that judge evidence invalidates checkpoints."""
        if self._runner_sha is None:
            from runner import provenance
            self._runner_sha = provenance.runner_identity()["content_sha256"]
        return self._runner_sha
    approved: dict | None = None

    def stop_reason(self) -> str | None:
        """Why no further gate work may start: operator cancellation or the attempt deadline."""
        if self.cancel_requested() or (self.cancel_file is not None and self.cancel_file.exists()):
            return "cancelled by operator"
        if self.deadline is not None and time.time() >= self.deadline:
            return "the attempt deadline passed"
        return None

    def env(self) -> dict:
        return dict(os.environ if self.base_env is None else self.base_env)

    @property
    def plan_dir(self) -> Path:
        return self.worktree / ".dev" / self.plan

    @property
    def spec(self) -> Path:
        return self.plan_dir / "spec.md"

    def result_path(self, stage: str | None = None) -> Path:
        return self.plan_dir / f"{stage or self.stage}-result.json"


# --- helpers ------------------------------------------------------------------

HEAVY_KINDS = ("tests", "e2e", "validation", "gauntlet", "setup")


def execute(ctx: GateContext, argv: list[str], *, label: str, kind: str, cwd: Path | None = None,
            env: dict | None = None, timeout_cap: float, boundary: str = "host",
            exec_dir: Path | None = None, extra_fds: list[int] | None = None) -> executor.Receipt:
    """Run one gate command as a guarded execution bounded by the attempt deadline and cancel file.

    Heavy commands (tests, e2e, validation, gauntlet, setup) first take one of
    `max_heavy_commands` host-wide leases; the guard inherits it, so a worker crash cannot
    free it while the command still runs.
    """
    exec_dir = exec_dir or next_exec_dir(ctx, label)
    lease, waited = None, 0.0
    if kind in HEAVY_KINDS and ctx.home is not None and ctx.max_heavy_commands:
        started = time.monotonic()
        lease = resources.acquire(ctx.home, "heavy", f"{ctx.plan}:{label}", count=ctx.max_heavy_commands,
                                  should_abort=lambda: ctx.stop_reason() is not None)
        waited = time.monotonic() - started
        if lease is None:
            stop = ctx.stop_reason() or "stopped"
            receipt = executor.Receipt(token=executor.new_token(), kind=kind, label=label,
                                       classification="cancelled" if stop.startswith("cancelled") else "timeout",
                                       exit_code=None, started_at=None, ended_at=executor.utc_now(), seconds=0.0,
                                       spawned=False, detail=f"{stop} while waiting for a heavy-command lease")
            ctx.executions.append({"label": label, "kind": kind, "dir": str(exec_dir),
                                   "classification": receipt.classification, "exit_code": None, "seconds": 0.0,
                                   "boundary": boundary, "lease_wait_seconds": round(waited, 2)})
            return receipt
    try:
        return _execute(ctx, argv, label=label, kind=kind, cwd=cwd, env=env, timeout_cap=timeout_cap, boundary=boundary,
                        exec_dir=exec_dir, extra_fds=[*(extra_fds or []), *([lease.fd] if lease else [])],
                        lease_wait=waited)
    finally:
        if lease is not None:
            lease.release()


def _execute(ctx: GateContext, argv: list[str], *, label: str, kind: str, cwd: Path | None, env: dict | None,
             timeout_cap: float, boundary: str, exec_dir: Path, extra_fds: list[int], lease_wait: float) -> executor.Receipt:
    cap = time.time() + timeout_cap
    deadline = cap if ctx.deadline is None else min(cap, ctx.deadline)
    request = executor.Request(
        token=executor.new_token(), kind=kind, label=label, argv=argv, cwd=str(cwd or ctx.worktree),
        run_dir=str(ctx.run_dir or ctx.attempt_dir), stdout=str(exec_dir / "stdout.log"),
        stderr=str(exec_dir / "stderr.log"), deadline_at=deadline,
        cancel_file=str(ctx.cancel_file) if ctx.cancel_file else None, grace=min(ctx.grace, 10),
        inherit_fds=[*ctx.inherit_fds, *extra_fds], boundary=boundary,
    )
    receipt = executor.run(exec_dir, request, ctx.env() if env is None else env,
                           cancel_requested=ctx.cancel_requested)
    ctx.executions.append({"label": label, "kind": kind, "dir": str(exec_dir), "classification": receipt.classification,
                           "exit_code": receipt.exit_code, "seconds": receipt.seconds, "boundary": boundary,
                           "lease_wait_seconds": round(lease_wait, 2)})
    return receipt


def next_exec_dir(ctx: GateContext, label: str) -> Path:
    """A fresh directory per gate command; a re-run gate (after recovery) never reuses an earlier one."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", label)[:60]
    index = len(ctx.executions) + 1
    while True:
        candidate = ctx.attempt_dir / "gate" / f"{index:02d}-{safe}"
        if not candidate.exists():
            return candidate
        index += 1


def output_of(receipt: executor.Receipt) -> str:
    parts = []
    for stream in (receipt.stdout, receipt.stderr):
        path = Path(stream["path"]) if stream else None
        if path is not None and path.is_file():
            with path.open("rb") as handle:
                parts.append(handle.read(64 * 1024).decode("utf-8", "replace"))
    return "".join(parts).strip()


def full_output(exec_dir: Path) -> str:
    """Where a failed command's complete output is, since a gate reason carries only its last lines."""
    return f" [full output: {exec_dir}/stdout.log, stderr.log]"


def describe(receipt: executor.Receipt) -> str:
    """How a command ended, in words a retry prompt can act on."""
    if receipt.classification in ("succeeded", "failed"):
        return f"exited {receipt.exit_code}"
    if receipt.classification == "timeout":
        return "timed out" if receipt.spawned else "did not start before its deadline"
    if receipt.classification == "spawn_failed":
        return f"could not start: {receipt.spawn_error}"
    return f"ended {receipt.classification}" + (f" ({receipt.detail})" if receipt.detail else "")


def interrupted(ctx: GateContext, receipt: executor.Receipt, what: str, **data: object) -> GateResult | None:
    """A blocked result when a command stopped for cancellation or the attempt deadline, else None."""
    if receipt.classification == "cancelled":
        return blocked(f"cancelled by operator during {what}", retryable=False, code="attempt.cancelled", **data)
    if receipt.classification == "timeout" and ctx.deadline is not None and time.time() >= ctx.deadline - 1:
        return blocked(f"the attempt deadline passed during {what}", code="attempt.deadline", **data)
    return None


def run_script(ctx: GateContext, argv: list[str], label: str) -> tuple[int, str, executor.Receipt]:
    receipt = execute(ctx, argv, label=label, kind="gate-script", timeout_cap=SCRIPT_TIMEOUT_S)
    code = receipt.exit_code if receipt.classification in ("succeeded", "failed") else 124
    return code, output_of(receipt), receipt


def first_lines(text: str, limit: int = 5) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    shown = "; ".join(lines[:limit])
    return shown + (f" (+{len(lines) - limit} more)" if len(lines) > limit else "")


def sections(text: str, level: int = 2) -> dict[str, str]:
    """Map lowercased heading text to its body for headings of exactly this level."""
    marker = "#" * level
    pattern = re.compile(rf"^{marker}\s+(.+?)\s*$", re.M)
    matches = list(pattern.finditer(text))
    found: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        found.setdefault(match.group(1).strip().lower(), text[match.end():end])
    return found


def plan_body(text: str) -> str:
    return sections(text).get("change plan", "")


def count_e2e(spec_text: str) -> int:
    count = 0
    for line in plan_body(spec_text).splitlines():
        match = re.match(r"^\s*tests:\s*(.*)$", line, re.I)
        if not match:
            continue
        count += sum(1 for scenario in match.group(1).split(";") if scenario.strip().startswith("[e2e]"))
    return count


def read_result(ctx: GateContext, stage: str | None = None) -> tuple[dict | None, str | None]:
    """(result, error): both None when absent; error set when present but not a valid schema-2 result."""
    stage = stage or ctx.stage
    path = ctx.result_path(stage)
    if not path.exists():
        return None, None
    try:
        return records.load_result(path, stage=stage), None
    except records.RecordError as error:
        return None, f"{error} [{error.code}]"


def highest_index(plan_dir: Path, prefix: str) -> tuple[int, Path | None]:
    best, best_path = 0, None
    if not plan_dir.is_dir():
        return best, best_path
    pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)\.md$")
    for path in plan_dir.iterdir():
        match = pattern.match(path.name)
        if match and int(match.group(1)) > best:
            best, best_path = int(match.group(1)), path
    return best, best_path


def verdict(text: str) -> str | None:
    match = re.search(r"^\s*\**Verdict:?\**:?\s*(.+?)\s*$", text, re.M | re.I)
    if not match:
        return None
    value = re.sub(r"[*_`]", "", match.group(1)).strip().upper()
    return value


def spec_flags(text: str) -> tuple[int, int]:
    return text.count("⚑"), len(re.findall(r"\[open\]", text))


def relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


# --- stage boundaries -----------------------------------------------------------------

STATUS_WORDS = {"A": "added", "M": "modified", "D": "deleted", "T": "type changed", "?": "untracked"}


def stage_allows(stage: str, path: str) -> bool:
    """The paths each stage may change. Plan files under .dev/ are untracked and judged separately."""
    if stage == "scope":
        return path.startswith("docs/") or path == records.CONTRACT_PATH
    if stage == "scope-review":
        return path in ("docs/decisions.md", "docs/contracts.md")
    return path != records.CONTRACT_PATH


def boundary(ctx: GateContext) -> GateResult | None:
    """The stage's complete Git delta against where its attempt started: branch, history, paths, and plan files.

    Committed changes count as much as staged, unstaged, deleted, renamed, and untracked
    ones. A violation already in history, a branch switch, or a rewritten history cannot
    be undone by retrying without rewriting history, so it parks; uncommitted ones retry.
    """
    start = ctx.baseline.get("head") or ctx.base_sha
    problems: list[str] = []
    permanent = False
    branch = wt.current_branch(ctx.worktree)
    if ctx.branch and branch != ctx.branch:
        problems.append(f"the worktree is on {branch or 'a detached HEAD'}, not the run branch {ctx.branch}")
        permanent = True
    if start and not wt.is_ancestor(ctx.worktree, start):
        problems.append(f"history was rewritten: {start[:12]} is no longer an ancestor of HEAD")
        permanent = True
    elif start:
        committed = wt.committed_paths(ctx.worktree, start)
        outside = [(status, path) for status, path in wt.stage_delta(ctx.worktree, start)
                   if not path.startswith(".dev/") and not stage_allows(ctx.stage, path)]
        if outside:
            permanent = permanent or any(path in committed for _, path in outside)
            listed = ", ".join(f"{path} ({STATUS_WORDS.get(status, status)}"
                               f"{', committed' if path in committed else ''})" for status, path in outside[:10])
            more = f" (+{len(outside) - 10} more)" if len(outside) > 10 else ""
            problems.append(f"{ctx.stage} changed paths outside its contract: {listed}{more}")
    before = set(wt.tracked_under(ctx.worktree, ".dev", ref=ctx.base_sha)) if ctx.base_sha else set()
    if start and before and not any("history was rewritten" in problem for problem in problems):
        committed = wt.committed_paths(ctx.worktree, start)
        touched = [path for _, path in wt.stage_delta(ctx.worktree, start) if path in before]
        if touched:
            ctx.warnings.append(f"{ctx.stage} changed plan files the base tracks: {', '.join(touched[:5])}; "
                                "they belong to other plans")
    # Plan files in Git are untidy, not wrong: the evidence the gate checks is unaffected, so this only warns.
    tracked = sorted(set(wt.tracked_under(ctx.worktree, ".dev")) | set(wt.tracked_under(ctx.worktree, ".dev", "HEAD")))
    added = [path for path in tracked if path not in before]
    if added:
        ctx.warnings.append(f"plan files are now tracked by Git: {', '.join(added[:5])}; .dev/ is meant to stay "
                            "out of commits")
    if not problems:
        return None
    gate = blocked(problems[0], retryable=not permanent, code="boundary.violation")
    gate.failures = problems
    gate.reason = "; ".join(problems)
    return gate


def intent_gate(ctx: GateContext) -> GateResult | None:
    """The sealed approved intent must verify, and the current spec must still carry its locked fields."""
    if not ctx.require_intent or ctx.run_dir is None:
        return None
    try:
        ctx.approved = intent.load_approved(ctx.run_dir, ctx.intent_sha256)
    except intent.IntentError as error:
        return blocked(f"{error}; repair: {error.repair}", retryable=False, code=error.code)
    if not ctx.spec.is_file():
        return None
    problems = intent.check_spec(ctx.run_dir, ctx.approved, ctx.spec.read_text(encoding="utf-8"), stage=ctx.stage,
                                 attempt=ctx.attempt)
    if problems:
        gate = blocked("; ".join(problems[:5]) + "; restore the approved text (the sealed spec is "
                       f"intent/versions/{ctx.approved['version']}/spec.md) or defer the change as a premise",
                       code="intent.scenario_changed")
        gate.failures = problems
        return gate
    return None


def contract_path(ctx: GateContext) -> Path:
    """The approved contract snapshot once one exists; the worktree file before the handoff."""
    if ctx.approved is not None:
        sealed_contract = intent.approved_contract(ctx.run_dir, ctx.approved)
        if sealed_contract is not None:
            return sealed_contract
    return ctx.worktree / records.CONTRACT_PATH


# --- scope ----------------------------------------------------------------------

def scope_commit_paths(ctx: GateContext) -> tuple[list[str], list[str]]:
    """(paths to commit, unexpected paths) for the interactive scope stage.

    Plan files under .dev/ are excluded from Git and never committed; only the
    project docs scope maintains (the ledger, contracts, overview) are.
    """
    allowed, unexpected = [], []
    for path in wt.changed_paths(ctx.worktree):
        (allowed if path.startswith("docs/") or path == records.CONTRACT_PATH else unexpected).append(path)
    return allowed, unexpected


@reports_warnings
def scope_gate(ctx: GateContext) -> GateResult:
    failures: list[str] = []
    precondition = False
    if not ctx.spec.is_file():
        failures.append(f"{relative(ctx.spec, ctx.worktree)} does not exist")
        precondition = True
    else:
        text = ctx.spec.read_text(encoding="utf-8")
        code, output, _ = run_script(ctx, [sys.executable, str(LINT_SPEC), str(ctx.spec)], "lint-spec")
        if code != 0:
            failures.append(f"lint-spec.py exited {code}: {first_lines(output)}")
        flags, opens = spec_flags(text)
        if flags:
            failures.append(f"spec contains {flags} ⚑ mark(s); resolve each or turn it into a ⊘ line")
        if opens:
            failures.append(f"spec contains {opens} [open] marker(s)")
        failures.extend(contract_failures(ctx, text))
    result, error = read_result(ctx)
    if error:
        failures.append(error)
    elif result is not None and result["status"] != "done":
        failures.append(f"scope-result.json says {result['status']}: {result['reason']}")
    if ctx.worktree.is_dir():
        violation = boundary(ctx)
        if violation is not None:
            failures.extend(violation.failures)
    if failures:
        gate = blocked(failures[0], precondition=precondition, code="scope.gate")
        gate.failures = failures
        return gate
    return passed()


def validation_block(spec_text: str) -> str:
    """The body of `### Validation` up to the next heading; a `# comment` inside a code fence is not a heading."""
    lines = spec_text.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() == "### validation":
            body, fenced = [], False
            for rest in lines[index + 1:]:
                if rest.strip().startswith("```"):
                    fenced = not fenced
                elif not fenced and re.match(r"^#{1,3}\s", rest):
                    break
                body.append(rest)
            return "\n".join(body)
    return ""


def contract_failures(ctx: GateContext, spec_text: str) -> list[str]:
    """Problems with the repository contract scope must hand off with, and its rendering in the spec."""
    path = ctx.worktree / records.CONTRACT_PATH
    if not path.is_file():
        return [f"{records.CONTRACT_PATH} does not exist; record the repository's validation commands there as "
                "argv or script records (see factory/references/repo-contract.md)"]
    try:
        contract = records.load_contract(path)
    except records.RecordError as error:
        return [f"{error} [{error.code}]"]
    gaps = records.contract_gaps(contract, records.parse_scenarios(spec_text))
    if gaps:
        return [f"{gap} (see factory/references/repo-contract.md)" for gap in gaps]
    block = validation_block(spec_text)
    missing = [c["id"] for c in contract["validation"]
               if not re.search(rf"(?<![\w-]){re.escape(c['id'])}(?![\w-])", block)]
    if missing:
        return [f"the spec's ### Validation block does not name contract command(s) {', '.join(missing)}"]
    return []


# --- scope-review ------------------------------------------------------------------

def scope_review_allowed(ctx: GateContext, path: str) -> bool:
    return stage_allows("scope-review", path)


def deferred_items(text: str) -> list[tuple[str, str | None]]:
    body = sections(text).get("deferred", "")
    items: list[tuple[str, str | None]] = []
    for chunk in re.split(r"^###\s+", body, flags=re.M)[1:]:
        title = chunk.splitlines()[0].strip() if chunk.strip() else "untitled"
        kind = re.search(r"^\s*(?:[-*]\s*)?\**Kind:?\**:?\s*([a-z-]+)", chunk, re.M | re.I)
        value = kind.group(1).lower() if kind else None
        items.append((title, value if value in DEFERRAL_KINDS else (f"invalid:{value}" if value else None)))
    return items


@reports_warnings
def scope_review_gate(ctx: GateContext) -> GateResult:
    if not ctx.spec.is_file():
        return blocked(f"{relative(ctx.spec, ctx.worktree)} does not exist", precondition=True, code="input.spec_missing")
    stop = ctx.stop_reason()
    if stop:
        return stopped(stop, "the scope-review gate")
    violation = boundary(ctx) or intent_gate(ctx)
    if violation is not None:
        return violation
    baseline = int(ctx.baseline.get("spec_review_index", 0))
    index, report = highest_index(ctx.plan_dir, "spec-review")
    if index <= baseline or report is None:
        return blocked(f"no new spec-review_N.md was written (highest index {index}, baseline {baseline})",
                       outcome="failed", code="review.missing")
    data = {"spec_review": relative(report, ctx.worktree)}
    code, output, receipt = run_script(ctx, [sys.executable, str(LINT_SPEC), str(ctx.spec)], "lint-spec")
    halted = interrupted(ctx, receipt, "lint-spec.py", **data)
    if halted:
        return halted
    if code != 0:
        return blocked(f"lint-spec.py exited {code} after review: {first_lines(output)}", code="spec.lint", **data)
    text = report.read_text(encoding="utf-8")
    value = verdict(text)
    if value not in REVIEW_VERDICTS:
        return blocked(f"{report.name} has no parseable Verdict: line (got {value!r})", code="review.verdict", **data)
    panel = report.with_suffix(".json")
    try:
        records.load_review(panel, required_lenses=records.SPEC_REVIEW_LENSES)
    except records.RecordError as error:
        return blocked(f"{error} [{error.code}]; rerun only the panel tasks `aggregate-findings.py pending` lists",
                       code="review.incomplete", **data)
    data["spec_review_record"] = relative(panel, ctx.worktree)
    if value == "APPROVED WITH DEFERRALS":
        items = deferred_items(text)
        if not items:
            return blocked(f"{report.name} is APPROVED WITH DEFERRALS but lists no ### item under ## Deferred",
                           code="review.deferral", **data)
        missing = [title for title, kind in items if kind is None or kind.startswith("invalid:")]
        if missing:
            return blocked(
                f"deferred item(s) without a valid 'Kind: premise | new-effort | unanswered' line: {'; '.join(missing)}",
                code="review.deferral", **data,
            )
        hard = [f"{title} ({kind})" for title, kind in items if kind in ("premise", "new-effort")]
        if hard:
            return blocked(f"scope review deferred work a human must decide: {'; '.join(hard)}", retryable=False,
                           code="premise.deferred",
                           **data)
        return blocked(
            f"scope review left unanswered question(s): {'; '.join(title for title, _ in items)}",
            code="review.unanswered", **data
        )
    result, error = read_result(ctx)
    if error:
        # The gate verified the evidence itself; a result file it cannot read is untidy, not a failure.
        ctx.warnings.append(f"the stage result is invalid: {error}")
    return passed(**data)


# --- build ---------------------------------------------------------------------------

def load_e2e_data(sidecar: Path) -> tuple[dict | None, str | None]:
    """(record, error) for a factory.e2e/1 sidecar: strict JSON, schema, and evidence hashes. Never executes it."""
    try:
        record = records.load_e2e(sidecar)
    except records.RecordError as error:
        return None, f"{error} [{error.code}]"
    problems = records.verify_e2e_evidence(record, sidecar.parent)
    if problems:
        return None, f"{sidecar.name} evidence is invalid: {'; '.join(problems[:5])}"
    return record, None


@reports_warnings
def build_gate(ctx: GateContext) -> GateResult:
    if not ctx.spec.is_file():
        return blocked(f"{relative(ctx.spec, ctx.worktree)} does not exist", precondition=True, code="input.spec_missing")
    violation = boundary(ctx) or intent_gate(ctx)
    if violation is not None:
        return violation
    start = ctx.baseline.get("build_start_sha") or ctx.base_sha
    if start and wt.commits_after(ctx.worktree, start) == 0:
        return blocked(f"no commits after {start[:12]}; build committed nothing", code="build.no_commits")
    dirty = wt.dirty_tracked(ctx.worktree)
    if dirty:
        return blocked(f"tracked worktree is dirty: {', '.join(dirty[:10])}", code="worktree.dirty")
    stop = ctx.stop_reason()
    if stop:
        return stopped(stop, "the build gate")
    data: dict = {"notes": f".dev/{ctx.plan}/implementation-notes.md"}
    evidence = scenario_evidence(ctx, data)
    if evidence is not None:
        return evidence
    validation = run_validation(ctx, data)
    if validation is not None:
        return validation
    result, error = read_result(ctx)
    if error:
        # The gate verified the evidence itself; a result file it cannot read is untidy, not a failure.
        ctx.warnings.append(f"the stage result is invalid: {error}")
    return passed(**data)


# Files whose content decides what a setup command installs. Setup reruns only when one of them changes, the
# contract's setup records change, or the runner changes; a worktree that never ran setup always runs it.
DEPENDENCY_FILES = ("package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
                    "bun.lockb", "uv.lock", "pyproject.toml", "poetry.lock", "Pipfile.lock", "requirements.txt",
                    "Gemfile.lock", "go.sum", "Cargo.lock", "composer.lock", ".nvmrc", ".python-version")


def dependency_fingerprint(root: Path) -> dict[str, str]:
    listing = wt.git("-C", str(root), "ls-files", "-s", check=False)
    found = {}
    for line in listing.splitlines():
        meta, _, path = line.partition("\t")
        name = path.rsplit("/", 1)[-1]
        if name in DEPENDENCY_FILES or (name.startswith("requirements") and name.endswith(".txt")):
            target = root / path
            found[path] = records.sha256_file(target) if target.is_file() else meta.split()[1]
    return found


def run_setup(ctx: GateContext, contract: dict, data: dict, *, root: Path | None = None,
              label: str = "setup") -> GateResult | None:
    """Run the contract's setup commands in a worktree, as the runner, before anything there needs dependencies.

    The run's own worktree checkpoints the result under its dependency fingerprint; a throwaway worktree (the
    [repro] base) always runs it.
    """
    setup = contract.get("setup") or []
    if not setup:
        return None
    root = root or ctx.worktree
    reusable_here = root == ctx.worktree
    inputs = {"setup": setup, "dependencies": dependency_fingerprint(root), "worktree": str(root),
              "environment": contract.get("environment", {}), "runner": ctx.runner_sha()}
    if reusable_here:
        reused, decision = checkpoints.reusable(ctx.run_dir, "setup", inputs)
        gone = [Path(p) for p in (reused or {}).get("data", {}).get("produces", []) if not Path(p).exists()]
        if gone:
            reused, decision = None, f"computed: setup output {gone[0].relative_to(root)} is missing"
        checkpoints.note(ctx, "setup", decision)
        if reused is not None:
            data["setup"] = {**reused["data"], "reused_from": reused["completed_at"]}
            return None
    summary, produced = [], []
    for command in setup:
        stop = ctx.stop_reason()
        if stop:
            return stopped(stop, "the setup commands", **data)
        exec_dir = next_exec_dir(ctx, f"{label}-{command['id']}")
        mode = commands.boundary(command, contract)
        try:
            parts = commands.argv(command, root)
            cwd = commands.resolve_inside(root, command.get("cwd", "."), "cwd")
            env = commands.environment(command, contract, ctx.env())
            wrapped, env = commands.confine(parts, mode, writable=[root, exec_dir], env=env)
            outputs, declared = commands.produces(command, root)
        except commands.CommandError as error:
            repair = f"; repair: {error.repair}" if error.repair else ""
            return blocked(f"setup command {command['id']} cannot run: {error}{repair}", retryable=False,
                           code="contract.unrunnable", **data)
        receipt = execute(ctx, wrapped, label=f"{label}-{command['id']}", kind="setup", cwd=cwd, env=env,
                          timeout_cap=commands.timeout_s(command), boundary=mode, exec_dir=exec_dir)
        summary.append({"id": command["id"], "argv": parts, "boundary": mode, "classification": receipt.classification,
                        "exit_code": receipt.exit_code, "receipt": str(exec_dir / "receipt.json")})
        halted = interrupted(ctx, receipt, f"setup command {command['id']}", **data)
        if halted:
            return halted
        if receipt.classification != "succeeded":
            tail = first_lines(output_of(receipt)[-2000:], 3)
            data["setup"] = {"commands": summary}
            return blocked(f"Setup command {command['id']} ({shlex.join(parts)}) {describe(receipt)} in the {mode} "
                           f"boundary" + (f": {tail}" if tail else "") + full_output(exec_dir),
                           retryable=False, code="setup.failed", **data)
        missing = [output for output in outputs if declared and not output.exists()]
        if missing:
            ctx.warnings.append(f"setup command {command['id']} succeeded but did not produce "
                                f"{', '.join(str(m.relative_to(root)) for m in missing)}, which its contract record "
                                "declares")
        produced.extend(output for output in outputs if output.exists())
    data["setup"] = {"commands": summary, "produces": [str(output) for output in produced]}
    if reusable_here:
        # Installed dependencies never belong in a commit and must not look like dirt to an agent tidying up.
        if produced:
            wt.install_excludes(root, [wt.exclude_pattern(root, output) for output in produced])
        checkpoints.save(ctx.run_dir, "setup", inputs, revision=wt.head(root), outputs=[], receipt=None,
                         data=data["setup"])
    return None


def scenario_evidence(ctx: GateContext, data: dict) -> GateResult | None:
    """Every present scenario proven by an execution the runner performed on this revision."""
    from runner import verification
    if ctx.run_dir is None or ctx.approved is None:
        return blocked("scenario evidence needs the run's approved intent", retryable=False, code="intent.missing",
                       **data)
    try:
        contract = records.load_contract(contract_path(ctx))
    except records.RecordError as error:
        return blocked(f"{records.CONTRACT_PATH}: {error} [{error.code}]", retryable=False, code="contract.invalid",
                       **data)
    setup = run_setup(ctx, contract, data)
    if setup is not None:
        return setup
    scenarios = verification.present_scenarios(ctx.run_dir)
    mapping, problem = verification.load_map(ctx, scenarios)
    if problem is not None:
        problem.data.update(data)
        return problem
    data["scenario_map"] = f".dev/{ctx.plan}/scenario-map.json"
    for step in (verification.check_tests(ctx, contract, mapping, data),):
        if step is not None:
            return step
    for check in (lambda: verification.check_repro(ctx, contract, mapping, scenarios, data),
                  lambda: verification.check_e2e(ctx, contract, mapping, scenarios, data)):
        stop = ctx.stop_reason()
        if stop:
            return stopped(stop, "the remaining scenario evidence", **data)
        result = check()
        if result is not None:
            return result
    return None


def run_validation(ctx: GateContext, data: dict) -> GateResult | None:
    """Run the contract's validation commands in order; None when every one succeeded."""
    try:
        contract = records.load_contract(contract_path(ctx))
    except records.RecordError as error:
        return blocked(f"{records.CONTRACT_PATH}: {error} [{error.code}]", retryable=False, code="contract.invalid",
                       **data)
    summary: list[dict] = []
    data["validation"] = summary
    tree = checkpoints.tree(ctx.worktree)
    for command in contract["validation"]:
        stop = ctx.stop_reason()
        if stop:
            return stopped(stop, f"validation command {command['id']}", **data)
        name = f"validation:{command['id']}"
        declared = sorted(set(command.get("env", [])) | set(contract.get("environment", {}).get("required", [])))
        inputs = {"tree": tree, "command": command, "boundary": contract.get("boundary", "host"),
                  "environment": contract.get("environment", {}), "runner": ctx.runner_sha(),
                  "environment_values_sha256": records.sha256_bytes(json.dumps(
                      [(n, ctx.env().get(n)) for n in declared]).encode("utf-8"))}
        reused, decision = checkpoints.reusable(ctx.run_dir, name, inputs)
        checkpoints.note(ctx, name, decision)
        if reused is not None:
            summary.append({**reused["data"], "reused_from": reused["completed_at"]})
            continue
        exec_dir = next_exec_dir(ctx, f"validation-{command['id']}")
        mode = commands.boundary(command, contract)
        try:
            parts = commands.argv(command, ctx.worktree)
            cwd = commands.resolve_inside(ctx.worktree, command.get("cwd", "."), "cwd")
            env = commands.environment(command, contract, ctx.env())
            wrapped, env = commands.confine(parts, mode, writable=[ctx.worktree, exec_dir], env=env)
        except commands.CommandError as error:
            repair = f"; repair: {error.repair}" if error.repair else ""
            return blocked(f"validation command {command['id']} cannot run: {error}{repair}", retryable=False,
                           code="contract.unrunnable", **data)
        receipt = execute(ctx, wrapped, label=f"validation-{command['id']}", kind="validation", cwd=cwd, env=env,
                          timeout_cap=commands.timeout_s(command), boundary=mode, exec_dir=exec_dir)
        summary.append({"id": command["id"], "argv": parts, "boundary": mode, "classification": receipt.classification,
                        "exit_code": receipt.exit_code, "receipt": str(exec_dir / "receipt.json")})
        halted = interrupted(ctx, receipt, f"validation command {command['id']}", **data)
        if halted:
            return halted
        if receipt.classification != "succeeded":
            tail = first_lines(output_of(receipt)[-2000:], 3)
            return blocked(f"Validation command {command['id']} ({shlex.join(parts)}) {describe(receipt)}"
                           + (f": {tail}" if tail else "") + full_output(exec_dir), code="validation.failed", **data)
        checkpoints.save(ctx.run_dir, name, inputs, revision=wt.head(ctx.worktree), outputs=[],
                         receipt=str(exec_dir / "receipt.json"), data=summary[-1])
    return None


# --- ship -----------------------------------------------------------------------------

def gauntlet_evidence(ctx: GateContext, head: str, data: dict) -> GateResult | None:
    """Every mandatory gauntlet check re-executed by the runner on this revision, or legitimately inapplicable."""
    path = ctx.plan_dir / "gauntlet.json"
    try:
        record = records.load_gauntlet(path)
    except records.RecordError as error:
        return blocked(f"{error} [{error.code}]", code="gauntlet.invalid", **data)
    data["gauntlet"] = {"record": relative(path, ctx.worktree), "checks": {}}
    if record["revision"] != head:
        return blocked(f"gauntlet.json covers {record['revision'][:12]}, not the current {head[:12]}",
                       code="gauntlet.stale", **data)
    try:
        contract = records.load_contract(contract_path(ctx))
    except records.RecordError as error:
        return blocked(f"{records.CONTRACT_PATH}: {error} [{error.code}]", retryable=False, code="contract.invalid",
                       **data)
    pinned = contract.get("gauntlet", {})
    summary = data["gauntlet"]["checks"]
    for entry in record["checks"]:
        check_id = entry["id"]
        rule = pinned.get(check_id, {})
        if "not_applicable" in rule:
            summary[check_id] = {"applicable": False, "reason": f"contract: {rule['not_applicable']}"}
            continue
        if not entry["applicable"]:
            if check_id == "dependency-rules" and not (ctx.worktree / "docs" / "dependencies.md").is_file():
                summary[check_id] = {"applicable": False, "reason": "the repository has no docs/dependencies.md"}
                continue
            return blocked(f"gauntlet check {check_id} was reported inapplicable ({entry.get('reason')}), but only the "
                           "repository contract can make a check inapplicable; unavailable tools and failed runs are "
                           "not inapplicability", code="gauntlet.unjustified", **data)
        if entry["surviving"]:
            return blocked(f"gauntlet check {check_id} has {len(entry['surviving'])} surviving violation(s): "
                           f"{'; '.join(entry['surviving'][:3])}", code="gauntlet.violations", **data)
        command = {"id": check_id, **(rule.get("command") or entry["command"])}
        stop = ctx.stop_reason()
        if stop:
            return stopped(stop, f"gauntlet check {check_id}", **data)
        name = f"gauntlet:{check_id}"
        inputs = {"tree": checkpoints.tree(ctx.worktree), "command": command, "runner": ctx.runner_sha(),
                  "boundary": contract.get("boundary", "host")}
        reused, decision = checkpoints.reusable(ctx.run_dir, name, inputs)
        checkpoints.note(ctx, name, decision)
        if reused is not None:
            summary[check_id] = {**reused["data"], "reused_from": reused["completed_at"]}
            continue
        exec_dir = next_exec_dir(ctx, f"gauntlet-{check_id}")
        mode = commands.boundary(command, contract)
        try:
            parts = commands.argv(command, ctx.worktree)
            env = commands.environment(command, contract, ctx.env())
            wrapped, env = commands.confine(parts, mode, writable=[ctx.worktree, exec_dir], env=env)
            cwd = commands.resolve_inside(ctx.worktree, command.get("cwd", "."), "cwd")
        except commands.CommandError as error:
            return blocked(f"gauntlet check {check_id} cannot run: {error}", code="gauntlet.unrunnable", **data)
        receipt = execute(ctx, wrapped, label=f"gauntlet-{check_id}", kind="gauntlet", cwd=cwd, env=env,
                          timeout_cap=commands.timeout_s(command), boundary=mode, exec_dir=exec_dir)
        summary[check_id] = {"applicable": True, "command": parts, "pinned": "command" in rule,
                             "tool_version": entry["tool_version"], "scope": entry["scope"],
                             "threshold_source": entry["threshold_source"], "classification": receipt.classification,
                             "receipt": str(exec_dir / "receipt.json")}
        halted = interrupted(ctx, receipt, f"gauntlet check {check_id}", **data)
        if halted:
            return halted
        if receipt.classification != "succeeded":
            tail = first_lines(output_of(receipt)[-1500:], 2)
            return blocked(f"gauntlet check {check_id} ({shlex.join(parts)}) {describe(receipt)} on {head[:12]}"
                           + (f": {tail}" if tail else "") + full_output(exec_dir), code="gauntlet.failed", **data)
        checkpoints.save(ctx.run_dir, name, inputs, revision=head, outputs=[], receipt=str(exec_dir / "receipt.json"),
                         data=summary[check_id])
    return None


def gh(args: list[str], cwd: Path, timeout: float = 120) -> tuple[int, str, str]:
    """A direct gh call for CLI commands outside an attempt."""
    try:
        result = subprocess.run([binary("gh"), *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL)
    except OSError as error:
        return 127, "", str(error)
    except subprocess.TimeoutExpired:
        return 124, "", f"gh {' '.join(args)} timed out"
    return result.returncode, result.stdout, result.stderr


def gh_gate(ctx: GateContext, args: list[str], label: str) -> tuple[int, str, str, executor.Receipt]:
    """gh inside a gate: a guarded execution under the attempt deadline."""
    receipt = execute(ctx, [binary("gh"), *args], label=label, kind="gate-gh", timeout_cap=GH_TIMEOUT_S)
    code = receipt.exit_code if receipt.classification in ("succeeded", "failed") else 124
    streams = []
    for stream in (receipt.stdout, receipt.stderr):
        path = Path(stream["path"]) if stream else None
        streams.append(path.read_text(encoding="utf-8", errors="replace") if path and path.is_file() else "")
    if receipt.classification == "spawn_failed":
        streams[1] = receipt.spawn_error or ""
        code = 127
    return code, streams[0], streams[1], receipt


def open_prs(ctx: GateContext) -> tuple[list[dict] | None, str | None]:
    code, out, err, receipt = gh_gate(ctx, ["pr", "list", "--head", ctx.branch or "", "--state", "open",
                                            "--json", "number,url,isDraft,headRefName,state"], "gh-pr-list")
    if receipt.classification in ("cancelled", "timeout"):
        return None, f"gh pr list {describe(receipt)}"
    if code != 0:
        return None, f"gh pr list failed ({code}): {first_lines(err or out)}"
    try:
        prs = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None, "gh pr list returned invalid JSON"
    return [pr for pr in prs if pr.get("headRefName", ctx.branch) == ctx.branch], None


def pr_checks(ctx: GateContext, number: int) -> tuple[list[dict] | None, str | None]:
    code, out, err, receipt = gh_gate(ctx, ["pr", "checks", str(number), "--json", "name,state,bucket"],
                                      "gh-pr-checks")
    if receipt.classification in ("cancelled", "timeout"):
        return None, f"gh pr checks {describe(receipt)}"
    try:
        return json.loads(out), None
    except json.JSONDecodeError:
        if "no checks" in (err + out).lower():
            return [], None
        return None, f"gh pr checks failed ({code}): {first_lines(err or out)}"


def blocker_titles(text: str) -> list[str]:
    body = sections(text).get("blockers", "")
    titles = []
    for line in body.splitlines():
        match = re.match(r"^\s*\[[^\]]*\]\s*\S+\s+-\s+(.+?)\s*$", line)
        if match:
            titles.append(match.group(1))
        elif line.startswith("### "):
            titles.append(line[4:].strip())
    return titles


def pr_record(pr: dict, checks: list[dict] | None = None) -> dict:
    record = {"number": pr.get("number"), "url": pr.get("url"), "draft": bool(pr.get("isDraft")),
              "merged": False, "head_sha": pr.get("headRefOid"), "base": pr.get("baseRefName")}
    if checks is not None:
        record["checks"] = summarize_checks(checks)
    return record


def summarize_checks(checks: list[dict]) -> dict:
    buckets: dict[str, list[str]] = {}
    for check in checks:
        bucket = (check.get("bucket") or check.get("state") or "unknown").lower()
        buckets.setdefault(bucket, []).append(check.get("name") or "?")
    return {"total": len(checks), **{key: sorted(value) for key, value in buckets.items()}}


PR_FIELDS = "number,url,isDraft,headRefName,headRefOid,baseRefName,body,state,isCrossRepository"
SHIP_ROUNDS = 3


class Restart(Exception):
    """The candidate revision or pull request changed while it was being verified."""


def pr_view(ctx: GateContext, number: int) -> tuple[dict | None, str | None]:
    code, out, err, receipt = gh_gate(ctx, ["pr", "view", str(number), "--json", PR_FIELDS], "gh-pr-view")
    if receipt.classification in ("cancelled", "timeout"):
        return None, f"gh pr view {describe(receipt)}"
    if code != 0:
        return None, f"gh pr view failed ({code}): {first_lines(err or out)}"
    try:
        return json.loads(out), None
    except json.JSONDecodeError:
        return None, "gh pr view returned invalid JSON"


def repo_slug(ctx: GateContext) -> str | None:
    url = wt.git("-C", str(ctx.worktree), "remote", "get-url", ctx.remote, check=False)
    match = re.search(r"github\.com[:/]([^/]+/[^/\s]+?)(?:\.git)?$", url)
    if match:
        return match.group(1)
    code, out, _, _ = gh_gate(ctx, ["repo", "view", "--json", "nameWithOwner"], "gh-repo-view")
    try:
        return json.loads(out)["nameWithOwner"] if code == 0 else None
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def evidence_text(body: str) -> str:
    match = re.search(r"^##\s+Evidence\s*$(.*?)(?=^##\s|\Z)", body or "", re.M | re.S)
    return records.normalize_text(match.group(1)) if match else ""


def synchronized(ctx: GateContext, pr: dict, head: str, data: dict) -> GateResult | None:
    """The PR is this run's, on the run's base, and its head, the refreshed remote branch, and local HEAD agree."""
    slug = repo_slug(ctx)
    pr_slug = re.search(r"github\.com/([^/]+/[^/]+)/pull/", pr.get("url") or "")
    if pr.get("isCrossRepository") or slug is None or not pr_slug or pr_slug.group(1) != slug:
        return blocked(f"PR #{pr.get('number')} ({pr.get('url')}) is not a same-repository pull request for "
                       f"{slug or 'the run remote'}", retryable=False, code="pr.wrong_repository", **data)
    if ctx.base and pr.get("baseRefName") != ctx.base:
        return blocked(f"PR #{pr['number']} targets {pr.get('baseRefName')}, not the run base {ctx.base}",
                       retryable=False, code="pr.wrong_base", **data)
    if pr.get("headRefName") != ctx.branch:
        return blocked(f"PR #{pr['number']} comes from {pr.get('headRefName')}, not {ctx.branch}", retryable=False,
                       code="pr.wrong_head", **data)
    fetch = execute(ctx, [binary("git"), "-C", str(ctx.worktree), "fetch", "--quiet", ctx.remote,
                          f"+refs/heads/{ctx.branch}:refs/remotes/{ctx.remote}/{ctx.branch}"],
                    label="git-fetch", kind="gate-git", timeout_cap=FETCH_TIMEOUT_S)
    halted = interrupted(ctx, fetch, "git fetch", **data)
    if halted:
        return halted
    if fetch.classification != "succeeded":
        return blocked(f"could not refresh {ctx.remote}/{ctx.branch} ({describe(fetch)}: "
                       f"{first_lines(output_of(fetch), 2)}); a stale remote-tracking ref proves nothing",
                       code="branch.refresh_failed", **data)
    remote = wt.git("-C", str(ctx.worktree), "rev-parse", "--verify", "--quiet",
                    f"refs/remotes/{ctx.remote}/{ctx.branch}", check=False)
    if not remote:
        return blocked(f"branch {ctx.branch} is not on {ctx.remote}", code="branch.unpublished", **data)
    if remote != head:
        if wt.is_ancestor(ctx.worktree, remote, head):
            detail = f"has {wt.commits_after(ctx.worktree, remote)} unpushed commit(s)"
        elif wt.is_ancestor(ctx.worktree, head, remote):
            detail = f"is behind {ctx.remote}/{ctx.branch} ({remote[:12]}), which has commits this run did not verify"
        else:
            detail = f"and {ctx.remote}/{ctx.branch} ({remote[:12]}) have diverged"
        return blocked(f"branch {ctx.branch} at {head[:12]} {detail}", code="branch.unsynchronized", **data)
    if pr.get("headRefOid") != head:
        raise Restart(f"PR #{pr['number']} head {str(pr.get('headRefOid'))[:12]} is not the verified {head[:12]}")
    return None


def published_evidence(ctx: GateContext, pr: dict, data: dict) -> GateResult | None:
    body = ctx.plan_dir / "pr.md"
    if not body.is_file():
        return blocked(f".dev/{ctx.plan}/pr.md does not exist", code="evidence.invalid", **data)
    local, published = evidence_text(body.read_text(encoding="utf-8")), evidence_text(pr.get("body") or "")
    if not published:
        return blocked(f"PR #{pr['number']}'s published body has no Evidence section; update it from .dev/{ctx.plan}/"
                       "pr.md with `gh pr edit --body-file`", code="evidence.unpublished", **data)
    if published != local:
        return blocked(f"PR #{pr['number']}'s published Evidence differs from .dev/{ctx.plan}/pr.md; update the PR "
                       "body", code="evidence.unpublished", **data)
    snapshot = next_exec_dir(ctx, "published-body")
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "body.md").write_text(pr.get("body") or "", encoding="utf-8")
    code, output, receipt = run_script(ctx, [sys.executable, str(PR_EVIDENCE), "check", str(snapshot / "body.md")],
                                       "pr-evidence-check")
    halted = interrupted(ctx, receipt, "pr-evidence.py check", **data)
    if halted:
        return halted
    if code != 0:
        return blocked(f"the published PR body fails pr-evidence.py check: {first_lines(output)}",
                       code="evidence.invalid", **data)
    return None


def required_checks(ctx: GateContext, pr: dict, head: str, data: dict) -> GateResult | None:
    """Wait for the contract's required checks on this head to appear and finish, under the attempt deadline."""
    try:
        ci = records.load_contract(contract_path(ctx))["ci"]
    except records.RecordError as error:
        return blocked(f"{records.CONTRACT_PATH}: {error} [{error.code}]", retryable=False, code="contract.invalid",
                       **data)
    required = ci.get("required_checks", [])
    allowed_skips = set(ci.get("allow_skipped", []))
    while True:
        checks, error = pr_checks(ctx, pr["number"])
        if error:
            return blocked(error, code="github.unavailable", **data)
        data["pr"] = pr_record(pr, checks)
        by_name = {check.get("name"): (check.get("bucket") or check.get("state") or "unknown").lower() for check in checks}
        failing = sorted(name for name, bucket in by_name.items() if bucket in ("fail", "cancel"))
        if failing:
            return blocked(f"PR #{pr['number']} has failing checks on {head[:12]}: {', '.join(map(str, failing))}",
                           code="ci.failed", **data)
        skipped = sorted(name for name in required if by_name.get(name) == "skipping" and name not in allowed_skips)
        if skipped:
            return blocked(f"required check(s) skipped on {head[:12]}: {', '.join(skipped)}", code="ci.skipped", **data)
        unknown = sorted(name for name in required if by_name.get(name) not in (None, "pass", "pending", "skipping"))
        if unknown:
            return blocked(f"required check(s) in an unknown state: {', '.join(unknown)}", code="ci.unknown", **data)
        missing = [name for name in required if name not in by_name]
        pending = sorted(name for name, bucket in by_name.items() if bucket == "pending")
        if not missing and not pending:
            if not required and "none" not in ci and not checks:
                return blocked("the contract requires CI checks but none were reported", code="ci.missing", **data)
            return None
        waiting = ", ".join([*(f"{name} (not reported yet)" for name in missing), *pending])
        if ctx.stop_reason() == "cancelled by operator":
            return blocked(f"cancelled while PR #{pr['number']} checks were pending", retryable=False,
                           code="attempt.cancelled", **data)
        if ctx.deadline is not None and time.time() + ctx.poll_seconds > ctx.deadline:
            return blocked(f"PR #{pr['number']} checks still pending at the stage deadline: {waiting}",
                           code="ci.missing" if missing and not pending else "ci.pending", **data)
        ctx.sleep(ctx.poll_seconds)
        current, error = pr_view(ctx, pr["number"])
        if error:
            return blocked(error, code="github.unavailable", **data)
        if current.get("headRefOid") != head or wt.head(ctx.worktree) != head:
            raise Restart(f"the PR head moved to {str(current.get('headRefOid'))[:12]} while its checks were pending")


@reports_warnings
def ship_gate(ctx: GateContext) -> GateResult:
    stop = ctx.stop_reason()
    if stop:
        return stopped(stop, "the ship gate")
    violation = boundary(ctx) or intent_gate(ctx)
    if violation is not None:
        return violation
    publication_data: dict = {}
    publication.publish(ctx, publication_data)
    prs, error = open_prs(ctx)
    if error:
        return blocked(error, code="github.unavailable", **publication_data)
    if not prs:
        return blocked(f"no open pull request for branch {ctx.branch}", code="pr.missing", **publication_data)
    if len(prs) > 1:
        numbers = ", ".join(f"#{pr.get('number')}" for pr in prs)
        return blocked(f"{len(prs)} open pull requests for {ctx.branch} ({numbers}); expected exactly one",
                       retryable=False, code="pr.multiple")
    number = prs[0]["number"]
    restarts: list[str] = []
    for _ in range(SHIP_ROUNDS):
        try:
            result = verify_candidate(ctx, number, restarts)
            result.data.update(publication_data)
            return result
        except Restart as change:
            restarts.append(str(change))
            stop = ctx.stop_reason()
            if stop:
                return stopped(stop, "re-verifying the changed pull request")
    return blocked(f"the pull request kept changing during verification: {'; '.join(restarts)}", code="pr.unstable")


def verify_candidate(ctx: GateContext, number: int, restarts: list[str]) -> GateResult:
    """One full verification of the PR's current candidate revision; raises Restart when it moves."""
    head = wt.head(ctx.worktree)
    pr, error = pr_view(ctx, number)
    if error:
        return blocked(error, code="github.unavailable")
    data: dict = {"pr": pr_record(pr), "pr_body": f".dev/{ctx.plan}/pr.md", "revision": head}
    if restarts:
        data["restarts"] = list(restarts)
    result = synchronized(ctx, pr, head, data) or published_evidence(ctx, pr, data)
    if result is not None:
        return result
    dirty = wt.dirty_tracked(ctx.worktree)
    if dirty:
        return blocked(f"tracked worktree is dirty: {', '.join(dirty[:10])}", code="worktree.dirty", **data)
    index, review = highest_index(ctx.plan_dir, "review")
    if review is None:
        return blocked(f"no review_N.md in .dev/{ctx.plan}", code="review.missing", **data)
    data["review"] = relative(review, ctx.worktree)
    text = review.read_text(encoding="utf-8")
    value = verdict(text)
    if value not in SHIP_VERDICTS:
        return blocked(f"{review.name} has no parseable Verdict: line (got {value!r})", code="review.verdict", **data)
    record_path = review.with_suffix(".json")
    try:
        record = records.load_review(record_path, required_lenses=SHIP_MANDATORY_LENSES)
    except records.RecordError as error:
        return blocked(f"{error} [{error.code}]; rerun only the panel tasks `aggregate-findings.py pending` lists",
                       code="review.incomplete", **data)
    data["review_record"] = relative(record_path, ctx.worktree)
    if record.get("revision") != head:
        return blocked(f"{record_path.name} reviewed {str(record.get('revision'))[:12]}, not the current {head[:12]}; "
                       "review the final revision", code="review.stale", **data)
    if record["verdict"] != value:
        return blocked(f"{review.name} says {value} but {record_path.name} says {record['verdict']}; render the "
                       "Markdown from the record", code="review.verdict", **data)
    if value == "BLOCK":
        titles = blocker_titles(text)
        reason = f"{review.name} verdict BLOCK: {'; '.join(titles) or 'blockers not listed'}"
        if not pr.get("isDraft"):
            reason += f"; PR #{number} must stay draft while blocked"
        return blocked(reason, code="review.block", **data)
    if pr.get("isDraft"):
        return blocked(f"PR #{number} is still a draft after verdict {value}", code="pr.draft", **data)
    for step in (lambda: gauntlet_evidence(ctx, head, data), lambda: scenario_evidence(ctx, data),
                 lambda: run_validation(ctx, data)):
        result = step()
        if result is not None:
            return result
        stop = ctx.stop_reason()
        if stop:
            return stopped(stop, "the rest of the ship gate", **data)
    after = wt.head(ctx.worktree)
    if after != head:
        raise Restart(f"HEAD moved from {head[:12]} to {after[:12]} during verification")
    dirty = wt.dirty_tracked(ctx.worktree)
    if dirty:
        return blocked(f"verification changed tracked files: {', '.join(dirty[:10])}", code="worktree.dirty", **data)
    ci = required_checks(ctx, pr, head, data)
    if ci is not None:
        return ci
    final, error = pr_view(ctx, number)
    if error:
        return blocked(error, code="github.unavailable", **data)
    if final.get("headRefOid") != head or wt.head(ctx.worktree) != head:
        raise Restart(f"the PR head moved to {str(final.get('headRefOid'))[:12]} before completion was recorded")
    if final.get("isDraft") or final.get("state") != "OPEN" or evidence_text(final.get("body") or "") != \
            evidence_text(pr.get("body") or ""):
        raise Restart(f"PR #{number} changed (draft, state, or Evidence) before completion was recorded")
    result, error = read_result(ctx)
    if error:
        # The gate verified the evidence itself; a result file it cannot read is untidy, not a failure.
        ctx.warnings.append(f"the stage result is invalid: {error}")
    if result is not None:
        if result.get("pr_url") and result["pr_url"] != pr.get("url"):
            return blocked(f"ship-result.json names {result['pr_url']} but the open PR is {pr.get('url')}",
                           code="result.mismatch", **data)
        if "draft" in result and isinstance(result["draft"], bool) and result["draft"] != bool(pr.get("isDraft")):
            return blocked(f"ship-result.json says draft={result['draft']} but PR #{number} disagrees",
                           code="result.mismatch", **data)
    data["completion"] = {"observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "revision": head,
                          "pr": {"number": number, "url": final.get("url"), "base": final.get("baseRefName"),
                                 "head_ref": final.get("headRefName"), "head_sha": final.get("headRefOid"),
                                 "evidence_sha256": records.sha256_bytes(evidence_text(final.get("body") or "")
                                                                         .encode("utf-8"))},
                          "checks": data["pr"].get("checks"), "restarts": list(restarts)}
    return passed(**data)


GATES: dict[str, Callable[[GateContext], GateResult]] = {
    "scope": scope_gate,
    "scope-review": scope_review_gate,
    "build": build_gate,
    "ship": ship_gate,
}


# --- merge rule -------------------------------------------------------------------------

@dataclass
class Outcome:
    outcome: str
    reason: str | None
    retryable: bool
    source: str
    warning: str | None = None
    code: str | None = None
    conditions: list[str] = field(default_factory=list)


def merge(gate: GateResult, result: dict | None, result_error: str | None, blocking: list[dict] = ()) -> Outcome:
    """Combine the authoritative gate, the stage's result file, and the run's unresolved park conditions.

    A claimed success never overrides a failed gate. A passing gate also cannot override a
    missing, invalid, blocked, or failed stage result: the result is the skill's completion
    contract, while the gate independently verifies its evidence. A recognized, unresolved
    park condition blocks completion even when the gate passed, and its code decides retryability.
    """
    notes = list(gate.warnings)
    invalid = f"the stage result is invalid: {result_error}" if result_error else None
    if blocking:
        described = "; ".join(f"{c['code']} ({c['id']}): {c['summary']}" for c in blocking[:3])
        retryable = all(c["retryable"] for c in blocking) and (gate.passed or gate.retryable)
        reason = described if gate.passed else f"{described}; gate: {gate.reason}"
        return Outcome("blocked", reason, retryable, "condition", warning=_join(notes), code=blocking[0]["code"],
                       conditions=[c["id"] for c in blocking])
    if gate.passed:
        if invalid:
            return Outcome("blocked", invalid, True, "skill", warning=_join(notes), code="result.invalid")
        if result is None:
            return Outcome("blocked", "skill wrote no result file", True, "skill", warning=_join(notes),
                           code="result.missing")
        if result["status"] != "done":
            reported = [c for c in result.get("conditions", []) if c.get("resolution", "unresolved") == "unresolved"]
            if result["status"] == "blocked" and reported and not blocking:
                # The skill stopped on conditions the runner's own gate has since disproved: the block is moot.
                codes = ", ".join(sorted({c["code"] for c in reported}))
                notes.append(f"skill reported blocked on {codes}, which the runner resolved at the gate")
                return Outcome("done", None, True, "gate", _join(notes))
            return Outcome(result["status"], f"skill reported {result['status']}: {result['reason']}", True, "skill",
                           warning=_join(notes), code=f"result.{result['status']}")
        return Outcome("done", None, True, "gate", _join(notes))
    reason = gate.reason
    if invalid:
        reason = f"{reason}; {invalid}"
    if result is not None and result["status"] != "done" and result.get("reason") and result["reason"] not in (reason or ""):
        reason = f"{reason} (skill: {result['reason']})"
    return Outcome(gate.outcome, reason, gate.retryable, "gate", warning=_join(notes), code=gate.code)


def _join(notes: list[str]) -> str | None:
    return "; ".join(notes) or None
