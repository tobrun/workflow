#!/usr/bin/env python3
"""Keep only findings on lines the local branch added: `path:line[:col]: ...` lines from stdin.

    uvx ruff check --output-format concise $(tools/harden/scope.sh --python) | python3 tools/harden/added_lines.py

Findings on untouched lines are pre-existing: they are counted on stderr, not printed. The comparison is
base..HEAD, so uncommitted edits are not included: commit first. Exits 2 when there is no merge base.
"""

import re
import subprocess
import sys

FINDING = re.compile(r"^(?P<path>.+?):(?P<line>\d+)\b")


def normalize(path: str) -> str:
    while path.startswith("./"):
        path = path[2:]
    return path


def merge_base() -> str:
    """Merge base of HEAD and the default branch; exits 2 with a clear message when there is none."""
    default = subprocess.run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], capture_output=True,
                             text=True, check=False).stdout.strip() or "origin/main"
    found = subprocess.run(["git", "merge-base", "HEAD", default], capture_output=True, text=True, check=False)
    if found.returncode != 0 or not found.stdout.strip():
        print(f"cannot find a merge base between HEAD and {default}: fetch it or create the ref "
              f"({found.stderr.strip()})", file=sys.stderr)
        sys.exit(2)
    return found.stdout.strip()


def added_lines() -> dict[str, set[int]]:
    base = merge_base()
    diff = subprocess.run(["git", "-c", "core.quotePath=false", "diff", "--unified=0", base, "HEAD"],
                          capture_output=True, text=True, check=True).stdout
    added: dict[str, set[int]] = {}
    path = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            # git appends a tab after a path containing spaces
            path = line[6:].rstrip("\t") if line.startswith("+++ b/") else None
        elif line.startswith("@@") and path:
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            start, count = int(match.group(1)), int(match.group(2) or 1)
            added.setdefault(path, set()).update(range(start, start + count))
    return added


def main() -> int:
    if "-h" in sys.argv[1:] or "--help" in sys.argv[1:]:
        print(__doc__)
        return 0
    added = added_lines()
    kept = skipped = 0
    for line in sys.stdin:
        match = FINDING.match(line)
        if not match:
            continue
        if int(match.group("line")) in added.get(normalize(match.group("path")), set()):
            sys.stdout.write(line)
            kept += 1
        else:
            skipped += 1
    print(f"{kept} finding(s) on added lines; {skipped} pre-existing", file=sys.stderr)
    return 1 if kept else 0


if __name__ == "__main__":
    sys.exit(main())
