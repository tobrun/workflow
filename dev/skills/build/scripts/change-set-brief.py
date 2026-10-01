#!/usr/bin/env python3
"""Cut a spec down to what one change set's implementer needs.

Usage: python3 change-set-brief.py .dev/{plan-name} N [N ...] [--out-dir DIR]

A brief is spec.md minus the decisions the change set does not link and minus
the other change sets, followed by what earlier change sets did and deviated on
from implementation-notes.md. Every kept line is verbatim, so an agent reading
the brief reads the spec's own words, a third of them.

With --out-dir, writes DIR/change-set-N.md per change set and prints each path
with its size against the spec. Without it, prints the one brief to stdout.
Exit 2 on a missing spec or an unknown change set.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HEADING = re.compile(r"^##\s+(.*?)\s*$")
DECISION = re.compile(r"^(D-[a-z0-9]+(?:-[a-z0-9]+)*):")
LINKED = re.compile(r"\bD-[a-z0-9]+(?:-[a-z0-9]+)*")
CHANGE_SET = re.compile(r"^\s*(\d+)\.\s+\S")
NOTE_ENTRY = re.compile(r"^##\s+Change set\s+(\d+)\b", re.IGNORECASE)
# The two note lines only the scenario checker and the reviewers read.
NOTE_NOISE = re.compile(r"^\s*-\s*(Tests added|Seams tested):", re.IGNORECASE)


def split_sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    """The spec as (lower-cased ## heading, lines) pairs; the text before the first is ''."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in lines:
        heading = HEADING.match(line)
        if heading:
            sections.append((heading.group(1).lower(), [line]))
        else:
            sections[-1][1].append(line)
    return sections


def decision_entries(research: list[str]) -> dict[str, list[str]]:
    """Each decision's header and its indented alternative lines, by slug."""
    entries: dict[str, list[str]] = {}
    current = None
    for line in research[1:]:
        header = DECISION.match(line)
        if header:
            current = entries.setdefault(header.group(1), [])
            current.append(line)
        elif current is not None and line[:1] in (" ", "\t") and line.strip():
            current.append(line)
        elif line.strip():
            current = None
    return entries


def change_set_blocks(plan: list[str]) -> dict[int, list[str]]:
    """Each change set's lines, from its numbered line to the next one."""
    blocks: dict[int, list[str]] = {}
    current = None
    for line in plan[1:]:
        change_set = CHANGE_SET.match(line)
        if change_set:
            current = blocks.setdefault(int(change_set.group(1)), [])
        if current is not None:
            current.append(line)
    for block in blocks.values():
        while block and not block[-1].strip():
            block.pop()
    return blocks


def notes_digest(notes: Path) -> list[str]:
    """implementation-notes.md without its test and seam inventories."""
    if not notes.is_file():
        return []
    kept = [
        line for line in notes.read_text(encoding="utf-8").splitlines()
        if not NOTE_NOISE.match(line) and not line.startswith("# ")
    ]
    return kept if any(NOTE_ENTRY.match(line) for line in kept) else []


def brief(plan_dir: Path, number: int) -> str:
    spec = plan_dir / "spec.md"
    sections = split_sections(spec.read_text(encoding="utf-8").splitlines())
    by_name = dict(sections)
    blocks = change_set_blocks(by_name.get("change plan", [""]))
    if number not in blocks:
        known = ", ".join(str(n) for n in sorted(blocks)) or "none"
        raise LookupError(f"{spec} has no change set {number} (change sets: {known})")
    block = blocks[number]
    decisions = decision_entries(by_name.get("research", [""]))
    linked = list(dict.fromkeys(slug for line in block for slug in LINKED.findall(line)))

    out = [
        f"# Brief: change set {number} of {plan_dir.name}",
        "",
        f"This is {spec.resolve()} cut down to change set {number}: every line below is the spec's own.",
        "Left out are the decisions this change set does not link and the other change sets' plans.",
        "Open the spec only to follow something this brief points at, never to read it whole.",
        "",
    ]
    for name, lines in sections:
        if name == "research":
            out += ["## Research (the decisions this change set links)", ""]
            for slug in linked:
                out += decisions.get(slug, [f"{slug}: not argued in the spec's research section"]) + [""]
            if not linked:
                out += ["This change set links no decision.", ""]
        elif name == "change plan":
            out += [f"## Change plan (change set {number} only)", ""] + block + [""]
            others = [blocks[n][0].strip() for n in sorted(blocks) if n != number]
            if others:
                out += ["The other change sets, whose files are not yours:"] + [f"- {o[:140]}" for o in others] + [""]
        else:
            out += lines + ([""] if lines and lines[-1].strip() else [])
    digest = notes_digest(plan_dir / "implementation-notes.md")
    if digest:
        out += ["## What earlier change sets did (from implementation-notes.md)", ""]
        out += [("#" + line) if line.startswith("## ") else line for line in digest]
    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str]) -> int:
    args: list[str] = []
    out_dir = None
    rest = argv[1:]
    while rest:
        value = rest.pop(0)
        if value == "--out-dir" and rest:
            out_dir = Path(rest.pop(0))
        else:
            args.append(value)
    numbers = args[1:]
    if not numbers or not all(n.isdigit() for n in numbers) or (out_dir is None and len(numbers) != 1):
        print(
            "usage: change-set-brief.py <plan directory> <change set> [<change set> ...] [--out-dir DIR]\n"
            "       several change sets need --out-dir",
            file=sys.stderr,
        )
        return 2
    plan_dir = Path(args[0])
    spec = plan_dir / "spec.md"
    if not spec.is_file():
        print(f"no spec.md at {spec}", file=sys.stderr)
        return 2
    try:
        briefs = {int(n): brief(plan_dir, int(n)) for n in numbers}
    except LookupError as error:
        print(error, file=sys.stderr)
        return 2
    if out_dir is None:
        sys.stdout.write(briefs[int(numbers[0])])
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    spec_bytes = len(spec.read_bytes())
    for number, text in briefs.items():
        target = out_dir / f"change-set-{number}.md"
        target.write_text(text, encoding="utf-8")
        size = len(text.encode("utf-8"))
        print(f"{target.resolve()}: {size} bytes, {round(100 * size / spec_bytes)}% of spec.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
