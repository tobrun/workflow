#!/usr/bin/env python3
"""Check that docs/architecture.md is present, well-formed, and current.

Usage:
  python3 architecture-check.py docs/architecture.md [--root DIR] [--touched FILE ...]

Exit 2 when the file is missing (run an initial capture), 1 with one violation
per line when the overview is stale or malformed, 0 when it is current for what was
checked. Violations: a required section missing, a backticked path that does
not exist under --root, and a --touched file that no component's "Lives at"
path covers ("uncharted").
"""

from __future__ import annotations

import argparse
import glob
import re
import sys
from pathlib import Path

SECTIONS = ["Components", "Flows", "Boundaries", "Cross-cutting", "Entry points"]
HEADER = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
BACKTICK = re.compile(r"`([^`\n]+)`")
PATH_LIKE = re.compile(r"^[^\s]+(/[^\s]*|\.[A-Za-z0-9]{1,6})$")
TABLE_ROW = re.compile(r"^\|(.+)\|\s*$")


def sections(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    matches = list(HEADER.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        found[match.group(1)] = text[match.end():end]
    return found


def path_exists(root: Path, raw: str) -> bool:
    candidate = raw.strip().rstrip("/")
    if any(ch in candidate for ch in "*?["):
        return bool(glob.glob(str(root / candidate), recursive=True))
    return (root / candidate).exists()


def table_rows(section: str) -> list[list[str]]:
    rows = []
    for line in section.splitlines():
        match = TABLE_ROW.match(line)
        if not match:
            continue
        cells = [cell.strip() for cell in match.group(1).split("|")]
        if all(set(cell) <= set("-: ") for cell in cells):
            continue
        rows.append(cells)
    return rows[1:] if rows else []


def component_paths(section: str) -> dict[str, list[str]]:
    paths: dict[str, list[str]] = {}
    for cells in table_rows(section):
        if not cells:
            continue
        paths[cells[0]] = [p.strip().rstrip("/") for p in BACKTICK.findall(" ".join(cells[1:])) if PATH_LIKE.match(p.strip())]
    return paths


def covered(touched: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if any(ch in pattern for ch in "*?["):
            base = pattern.split("*", 1)[0].rstrip("/")
            if base and touched.startswith(base + "/"):
                return True
        elif touched == pattern or touched.startswith(pattern + "/"):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("map")
    parser.add_argument("--root", default=".")
    parser.add_argument("--touched", nargs="*", default=[])
    args = parser.parse_args()

    root = Path(args.root).resolve()
    map_path = Path(args.map)
    if not map_path.exists():
        print(f"missing: {map_path} - run an initial capture")
        return 2
    text = map_path.read_text(encoding="utf-8", errors="replace")
    violations: list[str] = []

    if not re.search(r"^Purpose:", text, re.MULTILINE):
        violations.append("section: no 'Purpose:' line")
    if not re.search(r"^Captured:\s*\d{4}-\d{2}-\d{2}", text, re.MULTILINE):
        violations.append("section: no dated 'Captured:' line")
    found = sections(text)
    order = [name for name in found if name in SECTIONS]
    for name in SECTIONS:
        if name not in found:
            violations.append(f"section: missing '## {name}'")
    if order != [name for name in SECTIONS if name in found]:
        violations.append(f"section: out of order, expected {', '.join(SECTIONS)}")

    for raw in BACKTICK.findall(text):
        candidate = raw.strip()
        if PATH_LIKE.match(candidate) and not path_exists(root, candidate):
            violations.append(f"stale: `{candidate}` does not exist under {root}")

    components = component_paths(found.get("Components", ""))
    if "Components" in found and not components:
        violations.append("section: Components table has no rows")
    for name, paths in components.items():
        if not paths:
            violations.append(f"component: '{name}' names no backticked path in its row")

    all_paths = [p for paths in components.values() for p in paths]
    for touched in args.touched:
        relative = touched.strip()
        try:
            relative = str(Path(touched).resolve().relative_to(root))
        except ValueError:
            pass
        if not covered(relative, all_paths):
            violations.append(f"uncharted: {relative} is covered by no component's path")

    if violations:
        print("\n".join(violations))
        return 1
    print(f"architecture-check: ok ({len(components)} components, {len(args.touched)} touched files covered)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
