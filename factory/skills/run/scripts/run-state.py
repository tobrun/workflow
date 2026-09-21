#!/usr/bin/env python3
"""Own factory-run.json: init, handoff, diff-spec, diff-config, attempt, record, check-result, show.

Usage:
  run-state.py init <plan> --request <path-or-text> --base <branch>
                [--phases a,b,c] [--attempts <phase>=<n> ...] [--ceiling <n>]
  run-state.py handoff <plan> --branch <name> [--seal <path> ...] [--dirty <file> ...]
                [--config <path>] [--pipeline <path>]
  run-state.py diff-spec <plan>
  run-state.py diff-config <plan> [--config <path>] [--pipeline <path>]
  run-state.py attempt <plan> <phase> [--status launched|done|failed|stopped] [--result <path>]
                [--type <type> --skill <path>]   # type and skill only on a launch
  run-state.py record <plan>   # reads one JSON decision object on stdin
  run-state.py check-result <path>
  run-state.py show <plan>

Exit codes, which the orchestrator branches on:
  init          0 written; 1 a state file already exists; 3 unusable --phases,
                --attempts or --ceiling
  handoff       0 recorded (sealing nothing when no --seal is given); 1 a sealed
                file is missing; 3 unusable state file or --pipeline file
  diff-spec     0 no drift, or nothing was sealed; 1 drift found; 3 unusable state file
  diff-config   0 no drift, or no config file to watch; 1 a phase moved; 3 unusable
                state file or --pipeline file
  attempt       0 recorded; 3 unusable state file, a phase outside this run's list,
                --type or --skill on a close, no launched attempt to close, or an
                attempt already closed
  record        0 recorded; 1 action outside the four; 3 unreadable stdin JSON
                or unusable state file
  check-result  0 done; 1 failed, a missing or unparseable result file included;
                2 stopped
  show          0 printed; 3 unusable state file

3 always means the call itself could not be carried out - an unusable state
file, a usage error, or an attempt that cannot be closed - never a phase
outcome, so a bad call never reaches a judgment. A missing or unparseable
*result* file is a phase outcome, not a bad call: a subagent that dies before
writing its result is the likeliest real failure, so it is a failed attempt the
orchestrator can relaunch. Every function here stays at or under cyclomatic
complexity 10 (D-complexity-threshold).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

HEADING = re.compile(r"^##\s+(.*?)\s*$")
CHANGE_SET = re.compile(r"^\s*(\d+)\.\s+\S")
TESTS = re.compile(r"^\s*tests:\s*(.*)$", re.IGNORECASE)
NOT_DOING = re.compile(r"^\s*(?:[-*]\s+)?⊘\s")

ACTIONS = {"advance", "repair", "relaunch", "end"}
DEFAULT_PHASES = ("scope", "scope-review", "build", "ship")
DEFAULT_ATTEMPTS = 3
DEFAULT_CEILING = 12
SCHEMA = 2
ATTEMPT_STATUSES = ("launched", "done", "failed", "stopped")
STATE_NAME = "factory-run.json"
DEFAULT_CONFIG = Path(".factory") / "config.yaml"
BAD_CALL = 3
STATE_SHAPE = {
    "phases": dict,
    "scenario_texts": dict,
    "not_doing_lines": list,
    "decisions": list,
    "repairs": list,
    "seals": list,
    "pipeline": list,
    "budgets": dict,
}


def state_path(plan: str) -> Path:
    return Path(".dev") / plan / STATE_NAME


def spec_path(plan: str) -> Path:
    return Path(".dev") / plan / "spec.md"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def shape_problem(state: dict) -> str:
    """The first field whose shape the commands rely on is wrong, or an empty string.

    Only the fields this script reads back are checked, and only when present:
    a state file written by `init` carries them all, and a null or wrongly typed
    one would otherwise crash a command rather than report a bad call.
    """
    for field, kind in STATE_SHAPE.items():
        if field in state and not isinstance(state[field], kind):
            found = type(state[field]).__name__
            return f"{field} is {found}, expected {kind.__name__}"
    for phase, attempts in state.get("phases", {}).items():
        if not isinstance(attempts, list):
            return f"phases.{phase} is not a list of attempts"
        if any(not isinstance(attempt, dict) for attempt in attempts):
            return f"phases.{phase} holds an attempt that is not an object"
    return budget_problem(state)


def is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def budget_problem(state: dict) -> str:
    for phase, budget in state.get("budgets", {}).items():
        if not is_count(budget):
            return f"budgets.{phase} is not a number"
    if "ceiling" in state and not is_count(state["ceiling"]):
        return "ceiling is not a number"
    return ""


def load_state(plan: str) -> dict | None:
    """The parsed state file, or None (with the reason printed) when it is unusable."""
    path = state_path(plan)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"unusable state file {path}: {exc}")
        return None
    if not isinstance(state, dict):
        print(f"unusable state file {path}: not a JSON object")
        return None
    problem = shape_problem(state)
    if problem:
        print(f"unusable state file {path}: {problem}")
        return None
    return state


def save_state(plan: str, state: dict) -> None:
    state_path(plan).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def parse_scenario_list(value: str) -> list[str] | None:
    """The scenarios named on a `tests:` line, or None when it names none."""
    value = value.strip()
    if value.lower().startswith("none"):
        return None
    return [s.strip() for s in value.split(";") if s.strip()]


def not_doing_lines(lines: list[str]) -> list[str]:
    """Every ⊘ entry in the spec, stripped, in order.

    An entry opens with the mark, as a decision alternative or a list bullet
    (the notation lint-spec.py parses); a line that merely cites the character
    in prose is not one.
    """
    return [line.strip() for line in lines if NOT_DOING.match(line)]


def scenario_texts(lines: list[str]) -> dict[str, list[str]]:
    """Scenario texts per change set, read from the Change plan section."""
    texts: dict[str, list[str]] = {}
    in_plan = False
    current: str | None = None
    for line in lines:
        heading = HEADING.match(line)
        if heading:
            in_plan = heading.group(1).lower() == "change plan"
        if not in_plan:
            continue
        change_set = CHANGE_SET.match(line)
        if change_set:
            current = change_set.group(1)
            texts.setdefault(current, [])
            continue
        tests = TESTS.match(line)
        if not tests or current is None:
            continue
        scenarios = parse_scenario_list(tests.group(1))
        if scenarios is not None:
            texts[current] = scenarios
    return texts


def scenario_texts_and_not_doing(spec: Path) -> tuple[dict[str, list[str]], list[str]]:
    """Scenario texts per change set, and every ⊘ line, from the current spec."""
    lines = spec.read_text(encoding="utf-8").splitlines()
    return scenario_texts(lines), not_doing_lines(lines)


def git_dirty_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


def phase_ids(raw: str | None) -> list[str]:
    """The ordered phase ids of a run: --phases, or the built-in four."""
    if raw is None:
        return list(DEFAULT_PHASES)
    ids = [part.strip() for part in raw.split(",")]
    if not ids or any(not part for part in ids) or len(set(ids)) != len(ids):
        raise ValueError(f"--phases {raw!r}: expected distinct, non-empty ids separated by commas")
    return ids


def phase_budgets(phases: list[str], pairs: list[str] | None) -> dict[str, int]:
    """Per-phase attempt budgets: the default, overridden by --attempts <phase>=<n>."""
    budgets = {phase: DEFAULT_ATTEMPTS for phase in phases}
    for pair in pairs or []:
        phase, _, count = pair.partition("=")
        if phase not in budgets or not count.isdigit() or int(count) < 1:
            raise ValueError(f"--attempts {pair!r}: expected <phase>=<n> for a phase in this run")
        budgets[phase] = int(count)
    return budgets


def cmd_init(args: argparse.Namespace) -> int:
    path = state_path(args.plan)
    if path.exists():
        print(f"factory-run.json already exists at {path}")
        return 1
    try:
        phases = phase_ids(args.phases)
        budgets = phase_budgets(phases, args.attempts)
    except ValueError as exc:
        print(f"bad call: {exc}")
        return BAD_CALL
    if args.ceiling is not None and args.ceiling < 1:
        print(f"bad call: --ceiling {args.ceiling}: expected a positive number")
        return BAD_CALL
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "schema": SCHEMA,
        "plan": args.plan,
        "request": args.request,
        "base": args.base,
        "branch": None,
        "seals": [],
        "scenario_texts": {},
        "not_doing_lines": [],
        "config_path": None,
        "config_sha256": None,
        "pipeline": [],
        "dirty_files": [],
        "phases": {phase: [] for phase in phases},
        "budgets": budgets,
        "ceiling": args.ceiling if args.ceiling is not None else DEFAULT_CEILING,
        "decisions": [],
        "repairs": [],
    }
    save_state(args.plan, state)
    print(f"initialized {path}")
    return 0


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def has_dev_notation(lines: list[str]) -> bool:
    """Whether a sealed file is a spec in the dev notation: a Change plan or a ⊘ line."""
    if not_doing_lines(lines):
        return True
    for line in lines:
        heading = HEADING.match(line)
        if heading and heading.group(1).lower() == "change plan":
            return True
    return False


def read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def merge_notation(paths: list[Path]) -> tuple[dict[str, list[str]], list[str]]:
    """Scenario texts and ⊘ lines merged across every sealed spec in the dev notation."""
    texts: dict[str, list[str]] = {}
    not_doing: list[str] = []
    for path in paths:
        lines = read_lines(path)
        for change_set, scenarios in scenario_texts(lines).items():
            texts.setdefault(change_set, []).extend(scenarios)
        not_doing.extend(not_doing_lines(lines))
    return texts, not_doing


def seal_records(paths: list[Path]) -> list[dict]:
    return [
        {"path": str(path), "sha256": sha256_of(path), "notation": has_dev_notation(read_lines(path))}
        for path in paths
    ]


def load_pipeline(path: str) -> list[dict] | None:
    """The ids, types and skill paths of a `show --resolved --json` file, or None."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return [
            {"id": phase["id"], "type": phase["type"], "skill": phase["skill"]}
            for phase in data["phases"]
        ]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"unusable pipeline file {path}: {exc!r}")
        return None


def record_config_seal(state: dict, config: str | None, pipeline: str | None) -> bool:
    """Seal the config hash and the resolved pipeline; False when --pipeline is unusable."""
    if pipeline is not None:
        sealed = load_pipeline(pipeline)
        if sealed is None:
            return False
        state["pipeline"] = sealed
    path = Path(config) if config else DEFAULT_CONFIG
    state["config_path"] = str(path)
    state["config_sha256"] = sha256_of(path) if path.is_file() else None
    return True


def cmd_handoff(args: argparse.Namespace) -> int:
    paths = [Path(seal) for seal in args.seal or []]
    for path in paths:
        if not path.is_file():
            print(f"no sealed file at {path}")
            return 1
    state = load_state(args.plan)
    if state is None:
        return BAD_CALL
    if not record_config_seal(state, args.config, args.pipeline):
        return BAD_CALL
    records = seal_records(paths)
    texts, not_doing = merge_notation([Path(r["path"]) for r in records if r["notation"]])
    state["branch"] = args.branch
    state["seals"] = records
    state["sealed_nothing"] = not records
    state["scenario_texts"] = texts
    state["not_doing_lines"] = not_doing
    state["dirty_files"] = args.dirty if args.dirty else git_dirty_files()
    save_state(args.plan, state)
    if not records:
        print("handoff recorded: sealed nothing")
    else:
        print(
            f"handoff recorded: {len(records)} file(s) sealed, "
            f"{sum(len(v) for v in texts.values())} scenario(s), {len(not_doing)} not-doing line(s)"
        )
    return 0


def scenario_drift(stored: dict[str, list[str]], texts: dict[str, list[str]]) -> list[str]:
    """One message per change set whose approved scenarios were dropped or reworded."""
    problems: list[str] = []
    for change_set, before in stored.items():
        after = texts.get(change_set, [])
        dropped = [t for t in before if t not in after]
        added = [t for t in after if t not in before]
        if dropped:
            problems.append(
                f"change set {change_set}: scenario dropped or reworded: before {dropped!r}, now {added!r}"
            )
    return problems


def not_doing_drift(stored: list[str], not_doing: list[str]) -> list[str]:
    """One message per stored ⊘ line that is no longer in the spec."""
    return [f"⊘ line dropped: {line!r}" for line in stored if line not in not_doing]


def sealed_records(state: dict, plan: str) -> list[dict]:
    """The sealed files of a run; an older state file sealed `.dev/{plan}/spec.md` alone."""
    if "seals" in state:
        return [seal for seal in state["seals"] if isinstance(seal, dict)]
    if state.get("spec_sha256"):
        return [{"path": str(spec_path(plan)), "sha256": state["spec_sha256"], "notation": True}]
    return []


def hash_drift(records: list[dict]) -> list[str]:
    """One message per sealed file without dev notation that is missing or changed."""
    problems = []
    for record in records:
        path = Path(record.get("path", ""))
        if record.get("notation"):
            continue
        if not path.is_file():
            problems.append(f"changed: sealed file is missing: {path}")
        elif sha256_of(path) != record.get("sha256"):
            problems.append(f"changed: {path} no longer matches its sealed sha256")
    return problems


def notation_drift(state: dict, records: list[dict]) -> list[str]:
    paths = [Path(r.get("path", "")) for r in records if r.get("notation")]
    missing = [f"changed: sealed file is missing: {p}" for p in paths if not p.is_file()]
    if missing or not paths:
        return missing
    texts, not_doing = merge_notation(paths)
    problems = scenario_drift(state.get("scenario_texts", {}), texts)
    return problems + not_doing_drift(state.get("not_doing_lines", []), not_doing)


def cmd_diff_spec(args: argparse.Namespace) -> int:
    state = load_state(args.plan)
    if state is None:
        return BAD_CALL
    records = sealed_records(state, args.plan)
    if not records:
        print("not watched: nothing was sealed at handoff")
        return 0
    problems = hash_drift(records) + notation_drift(state, records)
    for message in problems:
        print(message)
    return 1 if problems else 0


def pipeline_positions(pipeline: list[dict]) -> dict[str, int]:
    return {phase.get("id"): index for index, phase in enumerate(pipeline, start=1)}


def moved_phases(sealed: list[dict], current: list[dict]) -> list[str]:
    """Phases whose place among the phases both pipelines share changed."""
    now_at, then_at = pipeline_positions(current), pipeline_positions(sealed)
    common_then = [p["id"] for p in sealed if p["id"] in now_at]
    common_now = [p["id"] for p in current if p["id"] in then_at]
    return [
        f"phase {pid} reordered: position {then_at[pid]} -> {now_at[pid]}"
        for index, pid in enumerate(common_then)
        if common_now.index(pid) != index
    ]


def edited_phases(sealed: list[dict], current: list[dict]) -> list[str]:
    """Phases present in both pipelines whose type or skill path changed."""
    before = {p["id"]: p for p in sealed}
    problems = []
    for phase in current:
        old = before.get(phase["id"])
        if old is None:
            continue
        if old["type"] != phase["type"]:
            problems.append(f"phase {phase['id']} retyped: {old['type']} -> {phase['type']}")
        if old["skill"] != phase["skill"]:
            problems.append(f"phase {phase['id']} repointed: {old['skill']} -> {phase['skill']}")
    return problems


def pipeline_drift(sealed: list[dict], current: list[dict]) -> list[str]:
    then_ids = {p["id"] for p in sealed}
    now_ids = {p["id"] for p in current}
    problems = [f"phase {p['id']} removed" for p in sealed if p["id"] not in now_ids]
    problems += [f"phase {p['id']} inserted" for p in current if p["id"] not in then_ids]
    return problems + moved_phases(sealed, current) + edited_phases(sealed, current)


def config_drift(state: dict, config: Path, pipeline: str | None) -> tuple[list[str], str] | None:
    """The drift messages and a one-line note, or None when --pipeline is unusable."""
    changed = sha256_of(config) != state.get("config_sha256")
    if pipeline is None:
        return ([f"config changed: {config} no longer matches its sealed sha256"] if changed else []), ""
    current = load_pipeline(pipeline)
    if current is None:
        return None
    sealed = state.get("pipeline", [])
    if not sealed:
        return [], "not watched: no pipeline was sealed at handoff"
    problems = pipeline_drift(sealed, current)
    note = "config text changed, resolved pipeline unchanged" if changed and not problems else ""
    return problems, note


def cmd_diff_config(args: argparse.Namespace) -> int:
    state = load_state(args.plan)
    if state is None:
        return BAD_CALL
    config = Path(args.config) if args.config else DEFAULT_CONFIG
    if not config.is_file():
        print("not watched: no config file, so the run uses the built-in default pipeline")
        return 0
    result = config_drift(state, config, args.pipeline)
    if result is None:
        return BAD_CALL
    problems, note = result
    for message in problems:
        print(message)
    if note:
        print(note)
    return 1 if problems else 0


def cmd_record(args: argparse.Namespace) -> int:
    raw = sys.stdin.read()
    try:
        decision = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"invalid JSON on stdin: {exc}")
        return BAD_CALL
    if not isinstance(decision, dict):
        print("invalid JSON on stdin: not a decision object")
        return BAD_CALL
    action = decision.get("action")
    if action not in ACTIONS:
        print(f"action {action!r} is not one of {sorted(ACTIONS)}")
        return 1
    state = load_state(args.plan)
    if state is None:
        return BAD_CALL
    entry = {
        "phase": decision.get("phase"),
        "attempt": decision.get("attempt"),
        "action": action,
        "rationale": decision.get("rationale", ""),
        "evidence": decision.get("evidence", []),
    }
    state.setdefault("decisions", []).append(entry)
    if action == "repair":
        repair = decision.get("repair", {})
        state.setdefault("repairs", []).append(
            {
                "phase": entry["phase"],
                "attempt": entry["attempt"],
                "description": repair.get("description", ""),
                "files": repair.get("files", []),
                "evidence": entry["evidence"],
            }
        )
    save_state(args.plan, state)
    print(f"recorded {action} for {entry['phase']} attempt {entry['attempt']}")
    return 0


def read_result(path: Path) -> tuple[dict | None, str]:
    """The parsed result object, or None and why the attempt counts as failed.

    A result file that is missing, unreadable, or not a JSON object is a phase
    outcome, not a bad call: the phase died before writing a usable result.
    """
    if not path.is_file():
        return None, f"no result file: {path}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"unparseable result file {path}: {exc}"
    if not isinstance(data, dict):
        return None, f"unparseable result file {path}: not a JSON object"
    return data, ""


def stop_kind(data: dict) -> str:
    """The stop kind of a stopped result, however loosely the phase wrote `stop`.

    A hard stop is never overridable, so a `stop` that is not the documented
    object still ends as a stop, never as a retryable failure.
    """
    stop = data.get("stop")
    if isinstance(stop, dict):
        return str(stop.get("kind", ""))
    if isinstance(stop, str):
        return stop
    return ""


def report_result(data: dict) -> int:
    """Print the result's one-line verdict and return its exit code.

    A present stop kind wins over `status`: a stop is unappealable and ending a
    run a phase did not mean to stop is recoverable, while continuing past a
    found secret is not.
    """
    kind = stop_kind(data)
    status = data.get("status")
    if kind or status == "stopped":
        print(f"stopped: {kind}")
        return 2
    if status == "done":
        print("done")
        return 0
    if status == "failed":
        print(f"failed: {data.get('reason', '')}")
        return 1
    print(f"failed: unrecognized status {status!r}")
    return 1


def cmd_check_result(args: argparse.Namespace) -> int:
    data, reason = read_result(Path(args.path))
    if data is None:
        print(f"failed: {reason}")
        return 1
    return report_result(data)


def close_attempt(attempt: dict, status: str, result: str | None) -> None:
    """Close a launched attempt, recording an unusable result file as a failure."""
    attempt["status"] = status
    attempt["closed"] = now()
    if not result:
        if status == "failed":
            attempt["result"] = {"status": "failed", "reason": "no result file"}
        return
    data, reason = read_result(Path(result))
    if data is None:
        attempt["status"] = "failed"
        attempt["result"] = {"status": "failed", "reason": reason}
        print(f"recorded as a failed attempt: {reason}")
        return
    attempt["result"] = data


def open_attempt(attempts: list[dict], args: argparse.Namespace) -> None:
    attempt = {"status": "launched", "result": None, "opened": now()}
    if args.type:
        attempt["type"] = args.type
    if args.skill:
        attempt["skill"] = args.skill
    attempts.append(attempt)


def cmd_attempt(args: argparse.Namespace) -> int:
    state = load_state(args.plan)
    if state is None:
        return BAD_CALL
    launching = args.status in (None, "launched")
    if not launching and (args.type or args.skill):
        print("--type and --skill are only valid when launching an attempt")
        return BAD_CALL
    phases = state.setdefault("phases", {})
    if args.phase not in phases:
        print(f"{args.phase!r} is not a phase of this run: {list(phases)}")
        return BAD_CALL
    attempts = phases[args.phase]
    if launching:
        open_attempt(attempts, args)
    elif not attempts:
        print(f"no launched attempt of {args.phase} to close")
        return BAD_CALL
    elif attempts[-1].get("status") != "launched":
        closed = attempts[-1].get("status")
        print(
            f"{args.phase} attempt {len(attempts)} is already closed as {closed!r}; "
            f"record a new launch instead of reclosing it"
        )
        return BAD_CALL
    else:
        close_attempt(attempts[-1], args.status, args.result)
    save_state(args.plan, state)
    print(f"{args.phase} attempt {len(attempts)}: {attempts[-1]['status']}")
    return 0


def run_finished(state: dict) -> bool:
    """Whether an advance was recorded on the last declared phase."""
    phases = list(state.get("phases", {}))
    if not phases:
        return False
    return any(
        isinstance(d, dict) and d.get("action") == "advance" and d.get("phase") == phases[-1]
        for d in state.get("decisions", [])
    )


def budget_lines(state: dict) -> list[str]:
    """Attempts against each phase's budget and the total against the ceiling."""
    budgets = state.get("budgets", {})
    lines = []
    total = 0
    for phase, attempts in state.get("phases", {}).items():
        budget = budgets.get(phase, DEFAULT_ATTEMPTS)
        total += len(attempts)
        mark = " (exhausted)" if len(attempts) >= budget else ""
        lines.append(f"{phase}: {len(attempts)}/{budget} attempts{mark}")
    ceiling = state.get("ceiling", DEFAULT_CEILING)
    mark = " (exhausted)" if total >= ceiling else ""
    lines.append(f"total: {total}/{ceiling} attempts{mark}")
    return lines


def cmd_show(args: argparse.Namespace) -> int:
    state = load_state(args.plan)
    if state is None:
        return BAD_CALL
    for phase, attempts in state.get("phases", {}).items():
        if not attempts:
            print(f"{phase}: no attempts")
            continue
        for index, attempt in enumerate(attempts, start=1):
            print(f"{phase} attempt {index}: {attempt.get('status', 'unknown')}")
    for line in budget_lines(state):
        print(line)
    for repair in state.get("repairs", []):
        print(f"repair on {repair['phase']} attempt {repair['attempt']}: {repair['description']}")
    print(f"finished: {'yes' if run_finished(state) else 'no'}")
    return 0


class Parser(argparse.ArgumentParser):
    """An ArgumentParser whose usage errors report BAD_CALL, not argparse's 2.

    2 is the orchestrator's unappealable hard stop, so a mistyped phase or
    status must not end an unattended run as if a secret had been found.
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: bad call: {message}", file=sys.stderr)
        raise SystemExit(BAD_CALL)


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init")
    init_p.add_argument("plan")
    init_p.add_argument("--request", required=True)
    init_p.add_argument("--base", required=True)
    init_p.add_argument("--phases")
    init_p.add_argument("--attempts", action="append")
    init_p.add_argument("--ceiling", type=int)

    handoff_p = sub.add_parser("handoff")
    handoff_p.add_argument("plan")
    handoff_p.add_argument("--branch", required=True)
    handoff_p.add_argument("--seal", action="append")
    handoff_p.add_argument("--dirty", action="append")
    handoff_p.add_argument("--config")
    handoff_p.add_argument("--pipeline")

    diff_p = sub.add_parser("diff-spec")
    diff_p.add_argument("plan")

    diff_config_p = sub.add_parser("diff-config")
    diff_config_p.add_argument("plan")
    diff_config_p.add_argument("--config")
    diff_config_p.add_argument("--pipeline")

    attempt_p = sub.add_parser("attempt")
    attempt_p.add_argument("plan")
    attempt_p.add_argument("phase")
    attempt_p.add_argument("--status", choices=ATTEMPT_STATUSES)
    attempt_p.add_argument("--result")
    attempt_p.add_argument("--type")
    attempt_p.add_argument("--skill")

    record_p = sub.add_parser("record")
    record_p.add_argument("plan")

    check_p = sub.add_parser("check-result")
    check_p.add_argument("path")

    show_p = sub.add_parser("show")
    show_p.add_argument("plan")

    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv[1:])
    handlers = {
        "init": cmd_init,
        "handoff": cmd_handoff,
        "diff-spec": cmd_diff_spec,
        "diff-config": cmd_diff_config,
        "attempt": cmd_attempt,
        "record": cmd_record,
        "check-result": cmd_check_result,
        "show": cmd_show,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
