#!/usr/bin/env python3
"""Check the portable skills tree against the Agent Skills spec (agentskills.io) and for self-containment.

Usage: python3 scripts/check_skills.py [skills-dir]   (default: skills)

Per skill folder: SKILL.md frontmatter uses only the spec's six fields; name is
lowercase kebab-case, at most 64 characters, and matches the folder; description
is non-empty and at most 1024 characters; compatibility at most 500; metadata maps
strings to strings; the body stays under 500 lines. Every relative link and
{skill}-skill-root path resolves to a file inside the tree - inside the skill, or a
sibling skill of the same set - and every bundled file is reachable from SKILL.md,
so an agent can find it. Exit 1 with one problem per line.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Fenced blocks and inline code are matched first so a link-shaped example is not checked.
LINK = re.compile(r"(?P<code>```.*?```|~~~.*?~~~|`[^`\n]+`)|\]\((?P<target>[^)\s#]+)(?:#[^)\s]*)?(?:\s+\"[^\"]*\")?\)", re.DOTALL)
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:|^/|^\{", re.IGNORECASE)
SKILL_ROOT = re.compile(r"\{([a-z0-9]+(?:-[a-z0-9]+)*)-skill-root\}(/[^\s`'\")\]]*)?")


def frontmatter(text: str) -> tuple[dict[str, object], int] | None:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return None
    fields: dict[str, object] = {}
    current = None
    for line in match.group(1).splitlines():
        if line.startswith("  ") and current == "metadata":
            key, _, value = line.strip().partition(":")
            fields["metadata"][key.strip()] = value.strip()  # type: ignore[index]
            continue
        key, _, value = line.partition(":")
        current = key.strip()
        fields[current] = {} if current == "metadata" and not value.strip() else value.strip()
    return fields, text[: match.end()].count("\n")


def check_skill(tree: Path, folder: Path, problems: list[str]) -> None:
    where = f"{tree.name}/{folder.name}"
    skill_md = folder / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8")
    parsed = frontmatter(text)
    if parsed is None:
        problems.append(f"{where}/SKILL.md: no YAML frontmatter")
        return
    fields, header_lines = parsed
    for key in sorted(set(fields) - FIELDS):
        problems.append(f"{where}/SKILL.md: '{key}' is not an Agent Skills field; keep harness-specific settings out of the portable copy")
    name = str(fields.get("name", ""))
    if not NAME.match(name) or len(name) > 64:
        problems.append(f"{where}/SKILL.md: name '{name}' is not lowercase kebab-case of at most 64 characters")
    if name != folder.name:
        problems.append(f"{where}/SKILL.md: name '{name}' does not match its folder")
    description = str(fields.get("description", ""))
    if not description or len(description) > 1024:
        problems.append(f"{where}/SKILL.md: description must be 1 to 1024 characters (has {len(description)})")
    if len(str(fields.get("compatibility", ""))) > 500:
        problems.append(f"{where}/SKILL.md: compatibility is over 500 characters")
    metadata = fields.get("metadata", {})
    if not isinstance(metadata, dict) or not all(isinstance(v, str) and v for v in metadata.values()):
        problems.append(f"{where}/SKILL.md: metadata must map strings to strings")
    body_lines = text.count("\n") - header_lines
    if body_lines >= 500:
        problems.append(f"{where}/SKILL.md: the body has {body_lines} lines; keep it under 500 and move detail to references/")

    reached = {skill_md.resolve()}
    queue = [skill_md]
    while queue:
        path = queue.pop()
        content = path.read_text(encoding="utf-8", errors="replace")
        targets = [path.parent / m.group("target") for m in LINK.finditer(content)
                   if m.group("target") and not SCHEME.match(m.group("target"))]
        for match in SKILL_ROOT.finditer(content):
            owner, rest = match.group(1), (match.group(2) or "").lstrip("/")
            if not (tree / owner / "SKILL.md").is_file():
                problems.append(f"{os.path.relpath(path, tree.parent)}: {{{owner}-skill-root}} names no skill in the set")
                continue
            if rest:
                targets.append(tree / owner / rest)
        for target in targets:
            resolved = target.resolve()
            label = f"{os.path.relpath(path, tree.parent)}: {os.path.relpath(target, path.parent)}"
            if not resolved.is_relative_to(tree.resolve()):
                problems.append(f"{label} leaves the skills tree; vendor the file into the skill")
            elif not resolved.exists():
                problems.append(f"{label} does not exist")
            elif resolved.is_relative_to(folder.resolve()) and resolved not in reached:
                reached.add(resolved)
                inside = sorted(p for p in resolved.rglob("*") if p.is_file()) if resolved.is_dir() else [resolved]
                for found in inside:
                    if found.resolve() not in reached:
                        reached.add(found.resolve())
                    if found.suffix == ".md":
                        queue.append(found)
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.resolve() not in reached and "agents" not in path.relative_to(folder).parts:
            problems.append(f"{where}/{path.relative_to(folder)} is bundled but nothing links to it from SKILL.md")


def main(argv: list[str]) -> int:
    tree = Path(argv[1] if len(argv) > 1 else "skills")
    if not tree.is_dir():
        print(f"no skills tree at {tree}", file=sys.stderr)
        return 2
    problems: list[str] = []
    folders = sorted(p for p in tree.iterdir() if p.is_dir())
    for folder in folders:
        if not (folder / "SKILL.md").is_file():
            problems.append(f"{tree.name}/{folder.name}: a folder in the skills tree without SKILL.md")
            continue
        check_skill(tree, folder, problems)
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} problem(s)")
        return 1
    print(f"{tree}: {len(folders)} portable skills, spec-compliant and self-contained")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
