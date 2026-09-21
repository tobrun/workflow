#!/usr/bin/env python3
"""Check that a spec's tests: scenarios really landed as tests.

Usage: python3 check-tests.py .dev/{plan-name} [--repo-root .]
Exit 0 when every change set is accounted for, 1 with one problem per line
otherwise. Every test named in implementation-notes.md must exist on disk with
that name in it, so a claimed test that was never written cannot pass as done.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HEADING = re.compile(r"^##\s+(.*?)\s*$")
CHANGE_SET = re.compile(r"^\s*(\d+)\.\s+\S")
TESTS = re.compile(r"^\s*tests:\s*(.*)$", re.IGNORECASE)
NOTE_ENTRY = re.compile(r"^##\s+Change set\s+(\d+)\b", re.IGNORECASE)
NOTE_TESTS = re.compile(r"^\s*-\s*Tests added:\s*(.*)$", re.IGNORECASE)


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


def note_entries(notes: Path) -> dict[int, list[str]]:
    """Tests named per change set, from implementation-notes.md."""
    entries: dict[int, list[str]] = {}
    current = None
    for line in notes.read_text(encoding="utf-8").splitlines():
        entry = NOTE_ENTRY.match(line)
        if entry:
            current = int(entry.group(1))
            entries.setdefault(current, [])
            continue
        named = NOTE_TESTS.match(line)
        if not named or current is None:
            continue
        value = named.group(1).strip()
        if value.lower().startswith("none"):
            continue
        entries[current].extend(t.strip() for t in value.split(",") if t.strip())
    return entries


def check_named_test(reference: str, repo_root: Path, change_set: int, problem) -> None:
    """A named test must be path::name, and that name must be in that file."""
    if "::" not in reference:
        problem(f"change set {change_set}: '{reference}' is not in path::test name form")
        return
    path, _, name = reference.partition("::")
    target = repo_root / path.strip()
    if not target.is_file():
        problem(f"change set {change_set}: {path.strip()} does not exist")
        return
    if name.strip() not in target.read_text(encoding="utf-8", errors="replace"):
        problem(
            f"change set {change_set}: {path.strip()} contains no test "
            f"named '{name.strip()}'"
        )


def main(argv: list[str]) -> int:
    args: list[str] = []
    repo_root = Path(".")
    rest = argv[1:]
    while rest:
        value = rest.pop(0)
        if value == "--repo-root" and rest:
            repo_root = Path(rest.pop(0))
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
    entries = note_entries(notes)

    for change_set, scenarios in sorted(counts.items()):
        if change_set not in entries:
            problem(f"change set {change_set} has no entry in {notes}")
            continue
        named = entries[change_set]
        if scenarios and len(named) < scenarios:
            problem(
                f"change set {change_set}: {scenarios} scenario(s) specced, "
                f"{len(named)} test(s) named"
            )
        for reference in named:
            check_named_test(reference, repo_root, change_set, problem)

    for change_set in sorted(set(entries) - set(counts)):
        problem(f"change set {change_set} is in {notes} but not in the spec's change plan")

    for message in problems:
        print(message)
    if problems:
        print(f"\n{len(problems)} problem(s); the spec is not fully implemented.")
        return 1
    print(f"{plan}: clean - {len(counts)} change sets, every named test found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
