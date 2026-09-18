#!/usr/bin/env python3
"""Coverage-weighted complexity for the functions the local branch touched.

    tools/harden/complexity_coverage.py RADON_JSON COVERAGE_JSON [--threshold 6]

RADON_JSON is `radon cc -j <files>`; COVERAGE_JSON is `coverage json` from a run of the suite. A function's score
is its cyclomatic complexity squared scaled down by its line coverage p: c*c*(1-p) + c*p, so a fully covered
function scores its complexity and an uncovered one its complexity squared. Prints each function over the threshold
as `file:line function score (complexity N, coverage P%)` and exits 1 when any is.

Per D-complexity-threshold in docs/decisions.md the line is 10 and applies to functions the branch added (their
name did not exist in the file at the merge base); `--touched` widens it to every function with an added line.
"""

import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from added_lines import added_lines  # noqa: E402


def base_names(path: str) -> set[str]:
    """Function and method names the file already defined at the merge base; empty for a new file."""
    default = subprocess.run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], capture_output=True,
                             text=True, check=False).stdout.strip() or "origin/main"
    base = subprocess.run(["git", "merge-base", "HEAD", default], capture_output=True, text=True,
                          check=False).stdout.strip()
    shown = subprocess.run(["git", "show", f"{base}:{path}"], capture_output=True, text=True, check=False)
    return set(re.findall(r"^\s*def (\w+)", shown.stdout, re.MULTILINE)) if shown.returncode == 0 else set()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("radon")
    parser.add_argument("coverage")
    parser.add_argument("--threshold", type=float, default=10)
    parser.add_argument("--touched", action="store_true", help="also judge pre-existing functions the branch edited")
    parser.add_argument("--all", action="store_true", help="print every touched function, not only offenders")
    parser.add_argument("--prefix", default="", help="repository-relative prefix for paths both reports carry, "
                        "such as factory/ when radon and coverage ran inside factory/")
    args = parser.parse_args()
    with open(args.radon, encoding="utf-8") as handle:
        radon = {args.prefix + path: blocks for path, blocks in json.load(handle).items()}
    with open(args.coverage, encoding="utf-8") as handle:
        files = json.load(handle)["files"]
    covered = {args.prefix + path: data for path, data in files.items()}
    added = added_lines()
    offenders = 0
    for path, blocks in sorted(radon.items()):
        cov = covered.get(path) or {}
        executed, missing = set(cov.get("executed_lines", [])), set(cov.get("missing_lines", []))
        existing = base_names(path)
        stack = list(blocks)
        while stack:
            block = stack.pop()
            stack += block.get("methods", []) + block.get("closures", [])
            if block["type"] == "class":
                continue
            lines = set(range(block["lineno"], block["endline"] + 1))
            mine = added.get(path, set())
            if not (lines & mine if args.touched else lines & mine and block["name"] not in existing):
                continue
            statements = lines & (executed | missing)
            p = len(statements & executed) / len(statements) if statements else 0.0
            c = block["complexity"]
            score = c * c * (1 - p) + c * p
            name = f"{block['classname']}.{block['name']}" if block.get("classname") else block["name"]
            if score > args.threshold or args.all:
                offenders += score > args.threshold
                print(f"{path}:{block['lineno']} {name} {score:.1f} (complexity {c}, coverage {p:.0%})")
    print(f"{offenders} function(s) over {args.threshold:g}", file=sys.stderr)
    return 1 if offenders else 0


if __name__ == "__main__":
    sys.exit(main())
