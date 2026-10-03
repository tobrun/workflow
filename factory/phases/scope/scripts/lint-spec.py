#!/usr/bin/env python3
"""Check a spec.md against the mechanical rules of the scope skill.

Usage: python3 lint-spec.py .dev/{plan-name}/spec.md
Exit 0 when the spec is clean, 1 with one problem per line otherwise.
A clean spec also gets the build waves its file lists allow, so the author sees
which shared files make change sets wait for each other.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SLUG = re.compile(r"^(D-[a-z0-9]+(?:-[a-z0-9]+)*):\s*(.+)$")
BAD_SLUG = re.compile(r"^(D-\S+):")
ALTERNATIVE = re.compile(r"^\s+([✓✗?⊘])\s+(.*)$")
FLAG = re.compile(r"^\s+⚑")
ECHO = re.compile(r"(D-[a-z0-9-]+)\s*\(([^)]*)\)")
CHANGE_SET = re.compile(r"^\s*(\d+)\.\s+\S")
TESTS = re.compile(r"^\s*tests:\s*(.*)$", re.IGNORECASE)
LAYER = re.compile(r"^\[(unit|integration|e2e)\]\s+\S")
HEADING = re.compile(r"^##\s+(.*?)\s*$")
NOTE_ENTRY = re.compile(r"^##\s+Change set\s+(\d+)\b", re.IGNORECASE)
BACKTICKED = re.compile(r"`([^`\s]+)`")
# A backticked token is a file when it ends in a known file extension;
# `embedder.model` and `job.stages` are code, not paths.
FILE_NAME = re.compile(
    r"(?:^|/)(?:Makefile|Dockerfile|[\w.@+-]+\."
    r"(?:c|cc|cpp|cs|css|go|gradle|graphql|h|html|java|js|json|jsx|kt|lock|md|mjs|cjs|php|proto"
    r"|py|rb|rs|scss|sh|sql|svelte|swift|tf|toml|ts|tsx|txt|vue|xml|yaml|yml))$"
)

CHOSEN, OPEN, NOT_DOING = "✓", "?", "⊘"
# One agent builds one change set; past this many scenarios it stops fitting in one sitting.
SCENARIO_LIMIT = 25


def sections(lines: list[str]) -> dict[str, list[tuple[int, str]]]:
    """Split numbered lines into research / scope / change plan buckets."""
    found: dict[str, list[tuple[int, str]]] = {}
    current = None
    for number, line in enumerate(lines, start=1):
        heading = HEADING.match(line)
        if heading:
            name = heading.group(1).lower()
            current = name if name in ("research", "scope", "change plan") else None
            if current:
                found.setdefault(current, [])
            continue
        if current:
            found[current].append((number, line))
    return found


def read_decisions(research: list[tuple[int, str]], problem) -> dict[str, dict]:
    decisions: dict[str, dict] = {}
    current = None
    for number, line in research:
        header = SLUG.match(line)
        if not header and BAD_SLUG.match(line):
            problem(number, f"{BAD_SLUG.match(line).group(1)} is not a kebab-case D- slug")
            current = None
            continue
        if header:
            slug, question = header.group(1), header.group(2)
            if slug in decisions:
                problem(number, f"{slug} is defined twice; slugs are unique and stable")
            current = decisions.setdefault(
                slug, {"line": number, "open": "[open]" in question, "marks": [], "flagged": False}
            )
            continue
        if current is None:
            continue
        if FLAG.match(line):
            current["flagged"] = True
            continue
        alternative = ALTERNATIVE.match(line)
        if not alternative:
            continue
        mark, text = alternative.group(1), alternative.group(2)
        current["marks"].append(mark)
        if " - " not in text:
            problem(number, "alternative has no ' - because' clause")
        if mark == NOT_DOING:
            current["not_doing"] = True
            if "reopen" not in text.lower():
                problem(number, "not-doing line states no condition that would reopen it")
        if mark == CHOSEN:
            current["choice"] = text.split(" - ")[0].strip().lower()
    return decisions


def check_decisions(decisions: dict[str, dict], problem) -> None:
    for slug, decision in decisions.items():
        chosen = decision["marks"].count(CHOSEN)
        # An [open] decision is still being argued and may not have its alternatives yet.
        if not decision["open"] and len(decision["marks"]) < 2:
            problem(decision["line"], f"{slug} argues fewer than two alternatives")
        if decision.get("not_doing"):
            if chosen:
                problem(decision["line"], f"{slug} is not-doing yet marks an alternative chosen")
        elif decision["open"]:
            if chosen:
                problem(decision["line"], f"{slug} is [open] yet marks an alternative chosen")
            if OPEN not in decision["marks"]:
                problem(decision["line"], f"{slug} is [open] with no ? alternative")
        elif chosen != 1:
            problem(decision["line"], f"{slug} has {chosen} chosen alternatives; expected exactly one")


def check_echoes(
    body: list[tuple[int, str]], decisions: dict[str, dict], in_change_plan: bool, problem
) -> None:
    for number, line in body:
        for slug, echo in ECHO.findall(line):
            decision = decisions.get(slug)
            if decision is None:
                problem(number, f"{slug} is echoed but never argued in the research section")
                continue
            resolution = echo.strip().lower()
            if decision.get("not_doing"):
                expected = resolution.startswith(NOT_DOING)
            elif decision["open"]:
                expected = resolution == "open"
            elif resolution.startswith(CHOSEN):
                shorthand = resolution[1:].strip()
                expected = not shorthand or shorthand in decision.get("choice", "")
            else:
                expected = False
            if not expected:
                problem(number, f"{slug} echo '({echo})' does not match its resolution")
            if in_change_plan and (decision["open"] or decision["flagged"]):
                problem(number, f"the change plan links {slug}, which is still open or flagged")


def built_change_sets(spec: Path) -> set[int]:
    """Change sets build already logged; their size is history, and they never renumber."""
    notes = spec.parent / "implementation-notes.md"
    if not notes.is_file():
        return set()
    entries = (NOTE_ENTRY.match(line) for line in notes.read_text(encoding="utf-8").splitlines())
    return {int(entry.group(1)) for entry in entries if entry}


def check_change_plan(body: list[tuple[int, str]], built: set[int], problem) -> None:
    counts: dict[int, int] = {}
    current = None
    for number, line in body:
        if "⚑" in line:
            problem(number, "the change plan carries a ⚑; resolve it before the plan is final")
        change_set = CHANGE_SET.match(line)
        if change_set:
            current = int(change_set.group(1))
            counts.setdefault(current, 0)
            continue
        tests = TESTS.match(line)
        if not tests:
            continue
        if current is None:
            problem(number, "tests: line outside any change set")
            continue
        counts[current] += 1
        value = tests.group(1).strip()
        if value.lower().startswith("none"):
            if " - " not in value:
                problem(number, "'tests: none' states no reason")
            continue
        if not value:
            problem(number, "tests: line is empty")
            continue
        scenarios = [scenario.strip() for scenario in value.split(";") if scenario.strip()]
        for scenario in scenarios:
            if not LAYER.match(scenario):
                problem(number, f"scenario '{scenario[:40]}' carries no [unit]/[integration]/[e2e] tag")
        if len(scenarios) > SCENARIO_LIMIT and current not in built:
            problem(
                number,
                f"change set {current} carries {len(scenarios)} scenarios; split it along a seam "
                f"so no change set carries more than {SCENARIO_LIMIT}",
            )
    for change_set, seen in sorted(counts.items()):
        if seen != 1:
            problem(None, f"change set {change_set} has {seen} tests: lines; expected exactly one")


def change_set_files(body: list[tuple[int, str]]) -> dict[int, set[str]]:
    """Files each change set names, read from the backticked paths outside its tests: line."""
    files: dict[int, set[str]] = {}
    current = None
    for _, line in body:
        change_set = CHANGE_SET.match(line)
        if change_set:
            current = int(change_set.group(1))
            files.setdefault(current, set())
        if current is None or TESTS.match(line):
            continue
        files[current].update(t for t in BACKTICKED.findall(line) if FILE_NAME.search(t))
    return files


def shared_files(one: set[str], other: set[str]) -> list[str]:
    """Paths two change sets both name; a bare file name matches any path that ends in it."""
    shared = one & other
    for mine, theirs in ((one, other), (other, one)):
        bare = {path for path in theirs if "/" not in path}
        shared |= {path for path in mine if "/" in path and path.rsplit("/", 1)[-1] in bare}
    return sorted(shared)


def build_waves(files: dict[int, set[str]]) -> tuple[list[list[int]], dict[int, tuple[int, list[str]]]]:
    """The waves build's consecutive-disjoint batching gives when only file lists decide.

    A change set joins the current wave when it shares no file with the wave or with
    any earlier change set still held back. Also returns, per change set that had to
    wait, the change set it waited on last and the files they share.
    """
    waves: list[list[int]] = []
    waited: dict[int, tuple[int, list[str]]] = {}
    remaining = sorted(files)
    while remaining:
        wave: list[int] = []
        held: list[int] = []
        for change_set in remaining:
            for earlier in wave + held:
                shared = shared_files(files[change_set], files[earlier])
                if shared:
                    waited[change_set] = (earlier, shared)
                    held.append(change_set)
                    break
            else:
                wave.append(change_set)
        waves.append(wave)
        remaining = held
    return waves, waited


def wave_report(body: list[tuple[int, str]], built: set[int]) -> list[str]:
    files = {number: paths for number, paths in change_set_files(body).items() if number not in built}
    if len(files) < 2:
        return []
    waves, waited = build_waves(files)
    layout = " ".join("[" + " ".join(str(n) for n in wave) + "]" for wave in waves)
    lines = [
        f"build waves by file lists alone: {len(files)} change sets in {len(waves)} waves - {layout}"
    ]
    for change_set, (other, shared) in sorted(waited.items()):
        more = f" (+{len(shared) - 3} more)" if len(shared) > 3 else ""
        lines.append(f"  {change_set} waits on {other}: {', '.join(shared[:3])}{more}")
    return lines


def check_validation(lines: list[str], problem) -> None:
    for index, line in enumerate(lines):
        if line.strip().lower() == "### validation":
            if any(rest.strip() and not rest.startswith("#") for rest in lines[index + 1 : index + 12]):
                return
            problem(index + 1, "the Validation block lists no commands")
            return
    problem(None, "no '### Validation' block in the scope section")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: lint-spec.py <path to spec.md>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"no spec at {path}", file=sys.stderr)
        return 2
    lines = path.read_text(encoding="utf-8").splitlines()

    problems: set[tuple[int, str]] = set()

    def problem(number: int | None, message: str) -> None:
        where = f"{path}:{number}" if number else str(path)
        problems.add((number or 0, f"{where}: {message}"))

    found = sections(lines)
    for name in ("research", "scope", "change plan"):
        if name not in found:
            problem(None, f"no '## {name.title()}' section")

    decisions = read_decisions(found.get("research", []), problem)
    check_decisions(decisions, problem)
    check_echoes(found.get("scope", []), decisions, False, problem)
    check_echoes(found.get("change plan", []), decisions, True, problem)
    built = built_change_sets(path)
    check_change_plan(found.get("change plan", []), built, problem)
    check_validation(lines, problem)

    for _, message in sorted(problems):
        print(message)
    if problems:
        print(f"\n{len(problems)} problem(s); the spec is not final.")
        return 1
    print(f"{path}: clean - {len(decisions)} decisions argued.")
    for line in wave_report(found.get("change plan", []), built):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
