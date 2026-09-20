#!/usr/bin/env python3
"""Own factory-run.json: init, handoff, diff-spec, attempt, record, check-result, show.

Usage:
  run-state.py init <plan> --request <path-or-text> --base <branch>
  run-state.py handoff <plan> --branch <name> [--spec <path>] [--dirty <file> ...]
  run-state.py diff-spec <plan> [--spec <path>]
  run-state.py attempt <plan> <phase> [--status launched|done|failed|stopped] [--result <path>]
  run-state.py record <plan>   # reads one JSON decision object on stdin
  run-state.py check-result <path>
  run-state.py show <plan>

Exit codes, which the orchestrator branches on:
  init          0 written; 1 a state file already exists
  handoff       0 recorded; 1 no spec.md at the path; 3 unusable state file
  diff-spec     0 no drift; 1 drift found; 3 unusable state file or no spec.md
  attempt       0 recorded; 3 unusable state file, or no launched attempt to close
  record        0 recorded; 1 action outside the four; 3 unreadable stdin JSON
                or unusable state file
  check-result  0 done; 1 failed; 2 stopped; 3 missing or unparseable result file
  show          0 printed; 3 unusable state file

3 always means the call itself could not be carried out, never a phase outcome,
so a malformed state file or a bad call never reaches a judgment. Every function
here stays at or under cyclomatic complexity 10 (D-complexity-threshold).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HEADING = re.compile(r"^##\s+(.*?)\s*$")
CHANGE_SET = re.compile(r"^\s*(\d+)\.\s+\S")
TESTS = re.compile(r"^\s*tests:\s*(.*)$", re.IGNORECASE)
NOT_DOING = re.compile(r"^\s*(?:[-*]\s+)?⊘\s")

ACTIONS = {"advance", "repair", "relaunch", "end"}
PHASES = ("scope", "scope-review", "build", "ship")
ATTEMPT_STATUSES = ("launched", "done", "failed", "stopped")
STATE_NAME = "factory-run.json"
BAD_CALL = 3


def state_path(plan: str) -> Path:
    return Path(".dev") / plan / STATE_NAME


def spec_path(plan: str, override: str | None) -> Path:
    return Path(override) if override else Path(".dev") / plan / "spec.md"


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


def cmd_init(args: argparse.Namespace) -> int:
    path = state_path(args.plan)
    if path.exists():
        print(f"factory-run.json already exists at {path}")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "plan": args.plan,
        "request": args.request,
        "base": args.base,
        "branch": None,
        "spec_sha256": None,
        "scenario_texts": {},
        "not_doing_lines": [],
        "dirty_files": [],
        "phases": {"scope": [], "scope-review": [], "build": [], "ship": []},
        "decisions": [],
        "repairs": [],
    }
    save_state(args.plan, state)
    print(f"initialized {path}")
    return 0


def cmd_handoff(args: argparse.Namespace) -> int:
    spec = spec_path(args.plan, args.spec)
    if not spec.is_file():
        print(f"no spec.md at {spec}")
        return 1
    state = load_state(args.plan)
    if state is None:
        return BAD_CALL
    digest = hashlib.sha256(spec.read_bytes()).hexdigest()
    texts, not_doing = scenario_texts_and_not_doing(spec)
    state["branch"] = args.branch
    state["spec_sha256"] = digest
    state["scenario_texts"] = texts
    state["not_doing_lines"] = not_doing
    state["dirty_files"] = args.dirty if args.dirty else git_dirty_files()
    save_state(args.plan, state)
    print(f"handoff recorded: {sum(len(v) for v in texts.values())} scenario(s), {len(not_doing)} not-doing line(s)")
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


def cmd_diff_spec(args: argparse.Namespace) -> int:
    state = load_state(args.plan)
    if state is None:
        return BAD_CALL
    spec = spec_path(args.plan, args.spec)
    if not spec.is_file():
        print(f"no spec.md at {spec}")
        return BAD_CALL
    texts, not_doing = scenario_texts_and_not_doing(spec)
    problems = scenario_drift(state.get("scenario_texts", {}), texts)
    problems += not_doing_drift(state.get("not_doing_lines", []), not_doing)
    for message in problems:
        print(message)
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


def read_result(path: Path) -> dict | None:
    """The parsed result file, or None when it is missing, unreadable, or not an object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


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


def cmd_check_result(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"missing result file: {path}")
        return BAD_CALL
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"unparseable result file {path}: {exc}")
        return BAD_CALL
    if not isinstance(data, dict):
        print(f"unparseable result file {path}: not a JSON object")
        return BAD_CALL
    status = data.get("status")
    if status == "done":
        print("done")
        return 0
    if status == "failed":
        print(f"failed: {data.get('reason', '')}")
        return 1
    if status == "stopped":
        print(f"stopped: {stop_kind(data)}")
        return 2
    print(f"failed: unrecognized status {status!r}")
    return 1


def cmd_attempt(args: argparse.Namespace) -> int:
    state = load_state(args.plan)
    if state is None:
        return BAD_CALL
    attempts = state.setdefault("phases", {}).setdefault(args.phase, [])
    if args.status == "launched":
        attempts.append({"status": "launched", "result": None})
    elif not attempts:
        print(f"no launched attempt of {args.phase} to close")
        return BAD_CALL
    else:
        attempts[-1]["status"] = args.status
        if args.result:
            attempts[-1]["result"] = read_result(Path(args.result))
    save_state(args.plan, state)
    print(f"{args.phase} attempt {len(attempts)}: {args.status}")
    return 0


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
    for repair in state.get("repairs", []):
        print(f"repair on {repair['phase']} attempt {repair['attempt']}: {repair['description']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init")
    init_p.add_argument("plan")
    init_p.add_argument("--request", required=True)
    init_p.add_argument("--base", required=True)

    handoff_p = sub.add_parser("handoff")
    handoff_p.add_argument("plan")
    handoff_p.add_argument("--branch", required=True)
    handoff_p.add_argument("--spec")
    handoff_p.add_argument("--dirty", action="append")

    diff_p = sub.add_parser("diff-spec")
    diff_p.add_argument("plan")
    diff_p.add_argument("--spec")

    attempt_p = sub.add_parser("attempt")
    attempt_p.add_argument("plan")
    attempt_p.add_argument("phase", choices=PHASES)
    attempt_p.add_argument("--status", choices=ATTEMPT_STATUSES, default="launched")
    attempt_p.add_argument("--result")

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
        "attempt": cmd_attempt,
        "record": cmd_record,
        "check-result": cmd_check_result,
        "show": cmd_show,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
