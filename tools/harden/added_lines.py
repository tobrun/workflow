#!/usr/bin/env python3
"""Keep only findings on lines the local branch added: `path:line[:col]: ...` lines from stdin.

    uvx ruff check --output-format concise $(tools/harden/scope.sh --python) | python3 tools/harden/added_lines.py

Findings on untouched lines are pre-existing: they are counted on stderr, not printed.
"""

import re
import subprocess
import sys

FINDING = re.compile(r"^(?P<path>[^:\s]+):(?P<line>\d+)\b")


def added_lines() -> dict[str, set[int]]:
    default = subprocess.run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], capture_output=True,
                             text=True, check=False).stdout.strip() or "origin/main"
    base = subprocess.run(["git", "merge-base", "HEAD", default], capture_output=True, text=True,
                          check=True).stdout.strip()
    diff = subprocess.run(["git", "diff", "--unified=0", base, "HEAD"], capture_output=True, text=True,
                          check=True).stdout
    added: dict[str, set[int]] = {}
    path = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@") and path:
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            start, count = int(match.group(1)), int(match.group(2) or 1)
            added.setdefault(path, set()).update(range(start, start + count))
    return added


def main() -> int:
    added = added_lines()
    kept = skipped = 0
    for line in sys.stdin:
        match = FINDING.match(line)
        if not match:
            continue
        if int(match.group("line")) in added.get(match.group("path"), set()):
            sys.stdout.write(line)
            kept += 1
        else:
            skipped += 1
    print(f"{kept} finding(s) on added lines; {skipped} pre-existing", file=sys.stderr)
    return 1 if kept else 0


if __name__ == "__main__":
    sys.exit(main())
