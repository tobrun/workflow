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
IMPACT = re.compile(r"\[impact:\s*(high|medium|low)\]")
RISK = re.compile(r"^Risk:\s*(low|medium|high)\s+-\s+\S", re.IGNORECASE)
ECHO = re.compile(r"(D-[a-z0-9-]+)\s*\(([^)]*)\)")
CHANGE_SET = re.compile(r"^(\d+)\.\s+\S")  # unindented; nested numbered lists are not change sets
TESTS = re.compile(r"^\s*tests:\s*(.*)$", re.IGNORECASE)
# The scenario tags, one per test layer, and the verification kinds of references/verification.md.
TAGS = ("unit", "component", "integration", "contract", "property", "fuzz", "e2e", "smoke", "benchmark")
LAYER = re.compile(r"^\[(" + "|".join(TAGS) + r")\]\s+\S")
KINDS = (
    "unit", "functional", "component", "contract", "property", "fuzz", "formal", "static-analysis", "lint",
    "coverage", "mutation", "regression", "acceptance", "smoke", "benchmark", "performance", "ui-ux",
    "usability", "threat-model", "penetration",
)
# A tag is proof for its kind: a kind marked yes needs a scenario with its tag, and a tag needs its kind marked yes.
TAG_KIND = {"unit": "unit", "integration": "functional", "component": "component", "contract": "contract",
            "property": "property", "fuzz": "fuzz", "smoke": "smoke", "benchmark": "benchmark"}
VERIFICATION_LINE = re.compile(r"^\s*[-*]\s+([a-z-]+):\s*(yes|no)\b\s*(.*)$")
# A formal mechanism the spec may name; Lean also needs a ### Theorems block, per references/proofs.md.
MECHANISM = re.compile(r"TLA\+|Alloy|\[property\]|stateful|\bLean\b|\bVerus\b|\bKani\b")
THEOREM_ENTRY = re.compile(r"^\s*[-*]\s+([A-Za-z_][\w']*):\s*(.*)$")
WITNESS = re.compile(r"^\s*witness:\s*(.*)$")
THREAT_LINE = re.compile(r"^\s*[-*]\s+.+:\s*[STRIDE]+\s+.+->.+tests:\s*\S")
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
# The narrative a person reads first, and the diagrams that may sit beside it.
SUMMARY_WORDS = 150
FLOW_DIAGRAMS = 3


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
            if not IMPACT.search(question):
                problem(number, f"{slug} states no [impact: high|medium|low] tag")
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
            hint = f"({CHOSEN} <text copied from: {decision.get('choice', '')}>)"
            if decision.get("not_doing"):
                expected = resolution.startswith(NOT_DOING)
                hint = f"({NOT_DOING} not doing)"
            elif decision["open"]:
                expected = resolution == "open"
                hint = "(open)"
            elif resolution.startswith(CHOSEN):
                shorthand = resolution[1:].strip()
                expected = not shorthand or shorthand in decision.get("choice", "")
            else:
                expected = False
            if not expected:
                problem(number, f"{slug} echo '({echo})' does not match its resolution; expected {hint}")
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
                problem(number, f"scenario '{scenario[:40]}' carries no layer tag; use one of "
                                + " ".join(f"[{tag}]" for tag in TAGS))
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


def check_risk(lines: list[str], problem) -> None:
    for line in lines:
        if HEADING.match(line):
            break
        if RISK.match(line):
            return
    problem(None, "no 'Risk: low|medium|high - why' line above the first section")


def scope_block(body: list[tuple[int, str]], name: str) -> list[tuple[int, str]] | None:
    """The lines of a ### block inside the scope section, or None when it is absent."""
    found, inside = None, False
    for number, line in body:
        if line.startswith("### "):
            inside = line[4:].strip().lower() == name
            if inside:
                found = []
            continue
        if inside and line.strip():
            found.append((number, line))
    return found


def used_tags(body: list[tuple[int, str]]) -> set[str]:
    tags = set()
    for _, line in body:
        tests = TESTS.match(line)
        if tests:
            tags.update(re.findall(r"\[(" + "|".join(TAGS) + r")\]", tests.group(1)))
    return tags


def check_verification(scope: list[tuple[int, str]], plan: list[tuple[int, str]], problem) -> None:
    block = scope_block(scope, "verification")
    if block is None:
        problem(None, "no '### Verification' block in the scope section; mark each of the "
                      f"{len(KINDS)} kinds in references/verification.md yes or no with a reason")
        return
    planned: dict[str, bool] = {}
    for number, line in block:
        match = VERIFICATION_LINE.match(line)
        if not match:
            problem(number, "a Verification line reads '- {kind}: yes|no - {how or why not}'")
            continue
        kind, answer, rest = match.groups()
        if kind not in KINDS:
            problem(number, f"'{kind}' is not a verification kind; use one of {', '.join(KINDS)}")
            continue
        if kind in planned:
            problem(number, f"{kind} is listed twice in the Verification block")
        planned[kind] = answer == "yes"
        if not rest.startswith("- ") or not rest[2:].strip():
            problem(number, f"{kind}: {answer} states no ' - {'how' if answer == 'yes' else 'reason'}'")
        if kind == "formal" and answer == "yes" and not MECHANISM.search(rest):
            problem(number, "formal: yes names no mechanism; a TLA+ or Alloy model, a Lean model with proofs, "
                            "Verus or Kani, or a stateful [property] test")
        if kind == "formal":
            planned["lean"] = answer == "yes" and bool(re.search(r"\bLean\b", rest))
    check_theorems(scope, plan, planned.pop("lean", False), problem)
    for kind in KINDS:
        if kind not in planned:
            problem(None, f"the Verification block does not mark {kind}; every kind is yes or no with a reason")
    tags = used_tags(plan)
    for tag, kind in TAG_KIND.items():
        if tag in tags and planned.get(kind) is False:
            problem(None, f"the change plan has [{tag}] scenarios but the Verification block marks {kind}: no")
        if planned.get(kind) and tag not in tags:
            problem(None, f"the Verification block marks {kind}: yes but no change set has a [{tag}] scenario")
    if planned.get("threat-model"):
        threats = scope_block(scope, "threat model")
        if not threats:
            problem(None, "threat-model: yes needs a '### Threat model' block in the scope section")
        else:
            for number, line in threats:
                if not THREAT_LINE.match(line):
                    problem(number, "a Threat model line reads "
                                    "'- {boundary}: {STRIDE letters} {threat} -> {mitigation} - tests: {scenario}'")
    if planned.get("penetration") is False and planned.get("threat-model"):
        problem(None, "threat-model: yes needs penetration: yes; the penetration test tries each threat")


def read_theorems(block: list[tuple[int, str]]) -> list[dict]:
    """The ### Theorems entries: name, reading, the fenced lean statement, and the witness."""
    entries: list[dict] = []
    fence = None
    for number, line in block:
        stripped = line.strip()
        if fence is not None:
            if stripped.startswith("```"):
                fence = None
            else:
                fence.append(stripped)
            continue
        if stripped.startswith("```lean") and entries:
            fence = []
            entries[-1]["fences"].append(fence)
            continue
        witness = WITNESS.match(line)
        if witness and entries:
            entries[-1]["witness"] = witness.group(1).strip()
            continue
        entry = THEOREM_ENTRY.match(line)
        if entry:
            entries.append({"line": number, "name": entry.group(1), "reading": entry.group(2).strip(),
                            "fences": [], "witness": ""})
    for entry in entries:
        entry["statement"] = "\n".join("\n".join(f) for f in entry.pop("fences"))
    return entries


def check_theorems(scope: list[tuple[int, str]], plan: list[tuple[int, str]], lean: bool, problem) -> None:
    """A Lean mechanism needs statements a person can approve, each read in English and shown non-vacuous."""
    block = scope_block(scope, "theorems")
    if block is None:
        if lean:
            problem(None, "formal: yes names Lean but the scope section has no '### Theorems' block")
        return
    if not lean:
        problem(None, "a '### Theorems' block needs formal: yes naming its Lean model")
    entries = read_theorems(block)
    if not entries:
        problem(block[0][0] if block else None, "the Theorems block states no theorem; "
                "each reads '- {name}: {reading}', a ```lean def {name} : Prop := ...``` fence, and 'witness:'")
    seen: set[str] = set()
    for entry in entries:
        name, number = entry["name"], entry["line"]
        if name in seen:
            problem(number, f"theorem {name} is stated twice")
        seen.add(name)
        if not entry["reading"]:
            problem(number, f"theorem {name} has no plain-English reading after its name")
        if not re.search(rf"^def\s+{re.escape(name)}\s*:\s*Prop\s*:=", entry["statement"], re.MULTILINE):
            problem(number, f"theorem {name} needs a ```lean fence stating 'def {name} : Prop := ...'")
        if not entry["witness"]:
            problem(number, f"theorem {name} has no 'witness:' line, a concrete example showing it is not vacuous")
    if lean:
        differential = any(re.search(r"\[property\][^;]*\b(?:model|oracle)\b", match.group(1), re.IGNORECASE)
                           for _, line in plan if (match := TESTS.match(line)))
        if not differential:
            problem(None, "formal: yes names Lean but no [property] scenario checks the code against the model; "
                          "add a differential test with the model as oracle")


def summary_paragraph(scope: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """The first paragraph under '### Summary', up to the blank line that ends it; what follows is ordinary scope."""
    found: list[tuple[int, str]] = []
    inside = False
    for number, line in scope:
        if line.startswith("### "):
            if found:
                break
            inside = line[4:].strip().lower() == "summary"
            continue
        if inside:
            if line.strip():
                found.append((number, line))
            elif found:
                break
    return found


def check_summary(scope: list[tuple[int, str]], problem) -> None:
    """The scope opens with a short narrative: plain prose a person reads before any list."""
    block = summary_paragraph(scope)
    if not block:
        problem(None, "no '### Summary' paragraph in the scope section; say in plain technical English what changes, "
                      f"for whom, and how it behaves, in at most {SUMMARY_WORDS} words")
        return
    if any(line.lstrip().startswith(("- ", "* ", "|", "```")) or re.match(r"^\s*\d+\.\s", line) for _, line in block):
        problem(block[0][0], "the Summary is a paragraph, not a list, table, or code block")
    words = sum(len(line.split()) for _, line in block)
    if words > SUMMARY_WORDS:
        problem(block[0][0], f"the Summary runs {words} words; keep it under {SUMMARY_WORDS}")


def check_flow(scope: list[tuple[int, str]], problem) -> None:
    block = scope_block(scope, "flow")
    if block is None:
        return
    fences = [number for number, line in block if line.strip().startswith("```mermaid")]
    if not fences:
        problem(None, "the Flow block holds no ```mermaid diagram")
    elif len(fences) > FLOW_DIAGRAMS:
        problem(fences[FLOW_DIAGRAMS], f"the Flow block holds {len(fences)} diagrams; keep the {FLOW_DIAGRAMS} that most help a reader")


def check_validation(lines: list[str], problem) -> None:
    for index, line in enumerate(lines):
        if line.strip().lower() == "### validation":
            if any(rest.strip() and not rest.startswith("#") for rest in lines[index + 1 : index + 12]):
                return
            problem(index + 1, "the Validation block lists no commands")
            return
    problem(None, "no '### Validation' block in the scope section")


def lint(path: Path, lines: list[str]) -> tuple[list[str], dict[str, dict]]:
    """Every problem in the spec, in line order, and the decisions it argues; render-spec.py reuses this."""
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
    check_change_plan(found.get("change plan", []), built_change_sets(path), problem)
    check_risk(lines, problem)
    check_validation(lines, problem)
    check_verification(found.get("scope", []), found.get("change plan", []), problem)
    check_summary(found.get("scope", []), problem)
    check_flow(found.get("scope", []), problem)
    return [message for _, message in sorted(problems)], decisions


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: lint-spec.py <path to spec.md>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"no spec at {path}", file=sys.stderr)
        return 2
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        print(f"{path} is not valid UTF-8: {error}", file=sys.stderr)
        return 2
    problems, decisions = lint(path, lines)
    for message in problems:
        print(message)
    if problems:
        print(f"\n{len(problems)} problem(s); the spec is not final.")
        return 1
    print(f"{path}: clean - {len(decisions)} decisions argued.")
    for line in wave_report(sections(lines).get("change plan", []), built_change_sets(path)):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
