#!/usr/bin/env python3
"""Own factory-run.json: init, handoff, diff-spec, record, check-result, show.

Usage:
  run-state.py init <plan> --request <path-or-text> --base <branch>
  run-state.py handoff <plan> --branch <name> [--spec <path>] [--dirty <file> ...]
  run-state.py diff-spec <plan> [--spec <path>]
  run-state.py record <plan>   # reads one JSON decision object on stdin
  run-state.py check-result <path>
  run-state.py show <plan>

The orchestrator loops on the exit code of every subcommand, so a malformed
state file or a bad call never reaches a judgment. Every function here stays
at or under cyclomatic complexity 10 (D-complexity-threshold).
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
NOT_DOING = re.compile(r"⊘")

ACTIONS = {"advance", "repair", "relaunch", "end"}
STATE_NAME = "factory-run.json"


def state_path(plan: str) -> Path:
    return Path(".dev") / plan / STATE_NAME


def spec_path(plan: str, override: str | None) -> Path:
    return Path(override) if override else Path(".dev") / plan / "spec.md"


def load_state(plan: str) -> dict:
    path = state_path(plan)
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(plan: str, state: dict) -> None:
    state_path(plan).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def scenario_texts_and_not_doing(spec: Path) -> tuple[dict[str, list[str]], list[str]]:
    """Scenario texts per change set, and every ⊘ line, from the current spec."""
    texts: dict[str, list[str]] = {}
    not_doing: list[str] = []
    in_plan = False
    current: str | None = None
    for line in spec.read_text(encoding="utf-8").splitlines():
        heading = HEADING.match(line)
        if heading:
            in_plan = heading.group(1).lower() == "change plan"
        if NOT_DOING.search(line):
            not_doing.append(line.strip())
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
        value = tests.group(1).strip()
        if value.lower().startswith("none"):
            continue
        texts[current] = [s.strip() for s in value.split(";") if s.strip()]
    return texts, not_doing


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


def cmd_diff_spec(args: argparse.Namespace) -> int:
    state = load_state(args.plan)
    spec = spec_path(args.plan, args.spec)
    texts, not_doing = scenario_texts_and_not_doing(spec)
    problems: list[str] = []
    stored_texts: dict[str, list[str]] = state.get("scenario_texts", {})
    for change_set, before in stored_texts.items():
        after = texts.get(change_set, [])
        dropped = [t for t in before if t not in after]
        added = [t for t in after if t not in before]
        if dropped:
            problems.append(
                f"change set {change_set}: scenario dropped or reworded: before {dropped!r}, now {added!r}"
            )
    stored_not_doing = state.get("not_doing_lines", [])
    for line in stored_not_doing:
        if line not in not_doing:
            problems.append(f"⊘ line dropped: {line!r}")
    for message in problems:
        print(message)
    return 1 if problems else 0


def cmd_record(args: argparse.Namespace) -> int:
    raw = sys.stdin.read()
    try:
        decision = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"invalid JSON on stdin: {exc}")
        return 1
    action = decision.get("action")
    if action not in ACTIONS:
        print(f"action {action!r} is not one of {sorted(ACTIONS)}")
        return 1
    state = load_state(args.plan)
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


def cmd_check_result(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"missing result file: {path}")
        return 3
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"unparseable result file {path}: {exc}")
        return 3
    status = data.get("status")
    if status == "done":
        print("done")
        return 0
    if status == "failed":
        print(f"failed: {data.get('reason', '')}")
        return 1
    if status == "stopped":
        kind = data.get("stop", {}).get("kind", "")
        print(f"stopped: {kind}")
        return 2
    print(f"failed: unrecognized status {status!r}")
    return 1


def cmd_show(args: argparse.Namespace) -> int:
    state = load_state(args.plan)
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
        "record": cmd_record,
        "check-result": cmd_check_result,
        "show": cmd_show,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
