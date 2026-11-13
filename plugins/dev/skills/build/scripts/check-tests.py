#!/usr/bin/env python3
"""Account for spec scenarios using runner-produced JUnit results.

Usage: python3 check-tests.py .dev/{plan-name} [--repo-root .]
       python3 check-tests.py capture --layer unit --report results.xml
         --receipt run.json --source tests.py --source app.py -- python -m pytest ...

The default requires test-evidence.json, mapping each positional scenario ID
(CS1-S1, CS1-S2, ...) to a passing runner case and a captured receipt.
--inventory-only checks declarations in notes for migration, not execution.
Receipts bind report and source hashes to a real command's result; they do not
prove that assertions or a claimed testing layer are semantically appropriate.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HEADING = re.compile(r"^##\s+(.*?)\s*$")
CHANGE_SET = re.compile(r"^\s*(\d+)\.\s+\S")
TESTS = re.compile(r"^\s*tests:\s*(.*)$", re.IGNORECASE)
NOTE_ENTRY = re.compile(r"^##\s+Change set\s+(\d+)\b", re.IGNORECASE)
NOTE_TESTS = re.compile(r"^\s*-\s*Tests added:\s*(.*)$", re.IGNORECASE)
# A comma separates two references only when a path::name follows it; a test name may hold commas.
NOTE_SEPARATOR = re.compile(r",\s*(?=[^,]*::)")


def spec_scenarios(spec: Path, problem) -> dict[int, int]:
    """Scenario count per change set, from the spec's change plan."""
    counts: dict[int, int] = {}
    in_plan = False
    current = None
    for number, line in enumerate(spec.read_text(encoding="utf-8").splitlines(), start=1):
        heading = HEADING.match(line)
        if heading:
            in_plan = heading.group(1).lower() == "change plan"
            continue
        if not in_plan:
            continue
        change_set = CHANGE_SET.match(line)
        if change_set:
            current = int(change_set.group(1))
            counts.setdefault(current, 0)
            continue
        tests = TESTS.match(line)
        if not tests or current is None:
            continue
        value = tests.group(1).strip()
        if value.lower().startswith("none"):
            continue
        counts[current] = len([s for s in value.split(";") if s.strip()])
    if not counts:
        problem(f"{spec}: no change sets found under '## Change plan'")
    return counts


def note_entries(notes: Path) -> tuple[dict[int, list[str]], list[str]]:
    """Tests named per change set, and those a fixup entry names, from implementation-notes.md.

    Any other ## heading ends a change set's entry, so a fixup's tests never
    count toward the scenarios of the change set above it.
    """
    entries: dict[int, list[str]] = {}
    fixups: list[str] = []
    current: list[str] | None = None
    for line in notes.read_text(encoding="utf-8").splitlines():
        entry = NOTE_ENTRY.match(line)
        if entry:
            current = entries.setdefault(int(entry.group(1)), [])
            continue
        if HEADING.match(line):
            current = fixups
            continue
        named = NOTE_TESTS.match(line)
        if not named or current is None:
            continue
        value = named.group(1).strip()
        if value.lower().startswith("none"):
            continue
        current.extend(t.strip() for t in NOTE_SEPARATOR.split(value) if t.strip())
    return entries, fixups


def check_named_test(reference: str, repo_root: Path, entry: str, problem) -> None:
    """Inventory only: recognize declarations, never arbitrary name substrings."""
    if "::" not in reference:
        problem(f"{entry}: '{reference}' is not in path::test name form")
        return
    path, _, name = reference.partition("::")
    target = repo_root / path.strip()
    if not target.is_file():
        problem(f"{entry}: {path.strip()} does not exist")
        return
    text = target.read_text(encoding="utf-8", errors="replace")
    if target.suffix == ".py":
        try:
            names = {node.name for node in ast.walk(ast.parse(text))
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and node.name.startswith("test")}
        except SyntaxError:
            names = set()
    elif target.suffix in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
        # This is a migration inventory, not runner discovery. Dynamic titles
        # must use the default evidence mode and the runner's exact case ID.
        text = re.sub(r"/\*[\s\S]*?\*/|^\s*//[^\n]*$", "", text, flags=re.MULTILINE)
        names = {match.group(2) for match in re.finditer(
            r"\b(?:test|it)(?:\.(?:only|skip))?\s*\(\s*(['\"])(.*?)\1", text
        )}
    else:
        problem(f"{entry}: unsupported inventory language; use runner evidence for {path.strip()}")
        return
    if name.strip() not in names:
        problem(f"{entry}: {path.strip()} contains no test named '{name.strip()}'")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scenarios(spec: Path) -> dict[str, tuple[str, str]]:
    """IDs retain change-set numbering; append scenarios instead of reordering."""
    result = {}
    in_plan, current = False, None
    for line in spec.read_text(encoding="utf-8").splitlines():
        heading = HEADING.match(line)
        if heading:
            in_plan = heading.group(1).lower() == "change plan"
        if not in_plan:
            continue
        change_set = CHANGE_SET.match(line)
        if change_set:
            current = int(change_set.group(1))
        tests = TESTS.match(line)
        if tests and current is not None and not tests.group(1).strip().lower().startswith("none"):
            for index, scenario in enumerate(tests.group(1).split(";"), 1):
                scenario = scenario.strip()
                if not scenario:
                    continue
                tag = re.match(r"^\[(unit|integration|e2e)\]\s+", scenario)
                result[f"CS{current}-S{index}"] = (tag.group(1) if tag else "", scenario)
    return result


def runner_cases(report: Path) -> dict[str, bool]:
    root = ET.parse(report).getroot()
    cases = {}
    for case in root.iter("testcase"):
        key = f"{case.get('classname', '')}::{case.get('name', '')}"
        if key in cases:
            raise ValueError(f"duplicate runner case {key}")
        cases[key] = not any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
    return cases


def capture(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run a test command and bind its JUnit report to source hashes.")
    parser.add_argument("--layer", choices=("unit", "integration", "e2e"), required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a test command is required after --")
    try:
        sources = {str(p.resolve()): sha256(p) for p in args.source}
        previous = args.report.stat().st_mtime_ns if args.report.exists() else None
        args.receipt.unlink(missing_ok=True)
        result = subprocess.run(command, check=False)
        if not args.report.is_file() or args.report.stat().st_mtime_ns == previous:
            raise ValueError("test command did not write a fresh JUnit report")
        runner_cases(args.report)
        if any(sha256(Path(path)) != value for path, value in sources.items()):
            raise ValueError("source changed while the test command ran")
        receipt = {
            "schema": "dev.test-run/1", "layer": args.layer, "command": command,
            "exitCode": result.returncode, "report": str(args.report.resolve()),
            "reportSha256": sha256(args.report), "sources": sources,
        }
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        return result.returncode
    except (OSError, ValueError, ET.ParseError) as error:
        print(f"capture: {error}", file=sys.stderr)
        return 1


def check_evidence(plan: Path, repo_root: Path, problem) -> None:
    """Verify mapping, freshness, passing execution, and declared layer."""
    expected = scenarios(plan / "spec.md")
    if not expected:
        return
    try:
        manifest = json.loads((plan / "test-evidence.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema") != "dev.test-evidence/1":
            raise ValueError("expected schema dev.test-evidence/1")
        mappings = manifest.get("scenarios")
        if not isinstance(mappings, list):
            raise ValueError("scenarios must be an array")
    except (OSError, ValueError) as error:
        problem(f"runner evidence: {error}; see references/test-evidence.md")
        return
    seen, used_cases = set(), set()
    for mapping in mappings:
        try:
            if not isinstance(mapping, dict):
                raise ValueError("scenario mapping must be an object")
            scenario_id = mapping.get("id")
            if not isinstance(scenario_id, str) or scenario_id not in expected or scenario_id in seen:
                raise ValueError(f"unknown or duplicate scenario id {scenario_id!r}")
            seen.add(scenario_id)
            layer, text = expected[scenario_id]
            if not layer or mapping.get("scenario") != text:
                raise ValueError(f"{scenario_id}: scenario text or layer changed")
            receipt_path = plan / mapping["receipt"]
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if not isinstance(receipt, dict) or receipt.get("schema") != "dev.test-run/1" or receipt.get("layer") != layer:
                raise ValueError(f"{scenario_id}: receipt layer disagrees with spec")
            if type(receipt.get("exitCode")) is not int or receipt["exitCode"] != 0:
                raise ValueError(f"{scenario_id}: test command did not pass")
            command = receipt.get("command")
            if not isinstance(command, list) or not command or any(
                not isinstance(part, str) or not part for part in command
            ):
                raise ValueError(f"{scenario_id}: missing test command")
            sources = receipt.get("sources")
            if not isinstance(sources, dict) or not sources:
                raise ValueError(f"{scenario_id}: receipt has no source hashes")
            source = (repo_root / mapping["source"]).resolve()
            source.relative_to(repo_root.resolve())
            if str(source) not in sources:
                raise ValueError(f"{scenario_id}: test source not bound to receipt")
            for path, value in sources.items():
                if sha256(Path(path)) != value:
                    raise ValueError(f"{scenario_id}: source changed since execution: {path}")
            report = Path(receipt["report"])
            if sha256(report) != receipt.get("reportSha256"):
                raise ValueError(f"{scenario_id}: runner report changed since capture")
            case = mapping["case"]
            key = (str(report.resolve()), case)
            if key in used_cases:
                raise ValueError(f"{scenario_id}: runner case already used by another scenario")
            used_cases.add(key)
            if runner_cases(report).get(case) is not True:
                raise ValueError(f"{scenario_id}: runner case absent, failed, or skipped: {case}")
        except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as error:
            problem(f"runner evidence: {error}")
    for missing in sorted(set(expected) - seen):
        problem(f"runner evidence: {missing} has no scenario mapping")


def main(argv: list[str]) -> int:
    if argv[1:2] == ["capture"]:
        return capture(argv[2:])
    args: list[str] = []
    repo_root = Path(".")
    inventory_only = False
    rest = argv[1:]
    while rest:
        value = rest.pop(0)
        if value == "--repo-root" and rest:
            repo_root = Path(rest.pop(0))
        elif value == "--inventory-only":
            inventory_only = True
        else:
            args.append(value)
    if len(args) != 1:
        print("usage: check-tests.py <plan directory> [--repo-root .]", file=sys.stderr)
        return 2

    plan = Path(args[0])
    spec, notes = plan / "spec.md", plan / "implementation-notes.md"
    for path in (spec, notes):
        if not path.is_file():
            print(f"no {path.name} at {path}", file=sys.stderr)
            return 2

    problems: list[str] = []

    def problem(message: str) -> None:
        problems.append(message)

    counts = spec_scenarios(spec, problem)
    entries, fixups = note_entries(notes)

    for change_set, scenarios in sorted(counts.items()):
        if change_set not in entries:
            problem(f"change set {change_set} has no entry in {notes}")
            continue
        named = entries[change_set]
        if inventory_only and scenarios and len(set(named)) < scenarios:
            problem(
                f"change set {change_set}: {scenarios} scenario(s) specced, "
                f"{len(named)} test(s) named"
            )
        if inventory_only:
            for reference in named:
                check_named_test(reference, repo_root, f"change set {change_set}", problem)

    for change_set in sorted(set(entries) - set(counts)):
        problem(f"change set {change_set} is in {notes} but not in the spec's change plan")

    if inventory_only:
        for reference in fixups:
            check_named_test(reference, repo_root, "a fixup entry", problem)
    else:
        check_evidence(plan, repo_root, problem)

    for message in problems:
        print(message)
    if problems:
        print(f"\n{len(problems)} problem(s); the spec is not fully implemented.")
        return 1
    proof = "inventory only; execution and layers NOT verified" if inventory_only else "runner evidence verified; assertion semantics require review"
    print(f"{plan}: clean - {len(counts)} change sets, {proof}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
