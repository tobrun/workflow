#!/usr/bin/env python3
"""Decide how much of a repository a local quality gate has to cover.

Usage:
  python3 impact-scope.py [--base REF] [--json]

Lists the files changed since REF (default: the merge base with the default
branch; the working tree and untracked files included), the packages that own
them, and a verdict a gate loops against instead of judging scope by feel:

  docs     every change is documentation; no test, build, or lint run is owed
  impacted the change is bounded to the listed packages; run each gate command
           at that scope and defer the full merge gate to the pull request's CI
  full     a change reaches every package (a lockfile, a root config, a CI
           workflow, a shared toolchain pin); run the gate commands in full

A package is the nearest directory holding a build manifest; "." is the root.
Exit status is 0 for every verdict and 2 only when git cannot answer.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import PurePosixPath

MANIFESTS = (
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "go.mod", "Cargo.toml",
    "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile", "composer.json", "mix.exs",
    "deno.json", "pubspec.yaml", "Package.swift", "CMakeLists.txt", "BUILD.bazel",
)
DOC_SUFFIXES = {".md", ".mdx", ".rst", ".txt", ".adoc"}
DOC_NAMES = {"LICENSE", "LICENCE", "NOTICE", "AUTHORS", "CODEOWNERS", "CHANGELOG"}
DOC_DIRS = {"docs", "doc", ".dev"}
# A change under one of these reaches every package, wherever it sits.
GLOBAL_DIRS = (".github/workflows/", ".github/actions/", ".circleci/", ".buildkite/", ".gitlab/")
GLOBAL_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "pnpm-workspace.yaml", "bun.lockb",
    "poetry.lock", "uv.lock", "Pipfile.lock", "go.work", "go.work.sum", "Cargo.lock",
    "Gemfile.lock", "composer.lock", ".gitlab-ci.yml", ".tool-versions", ".nvmrc",
    ".node-version", ".python-version", "rust-toolchain", "rust-toolchain.toml",
    "WORKSPACE", "WORKSPACE.bazel", "MODULE.bazel", "nx.json", "turbo.json", "lerna.json",
}
CONFIG_NAMES = {"Makefile", "GNUmakefile", "justfile", "Justfile", "Dockerfile", "Taskfile.yml", "noxfile.py", "conftest.py"}
CONFIG_SUFFIXES = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".lock", ".mk"}


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def default_base() -> str:
    candidates = []
    try:
        candidates.append(git("symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip())
    except RuntimeError:
        pass
    candidates += ["origin/main", "origin/master", "main", "master"]
    for ref in candidates:
        try:
            return git("merge-base", "HEAD", ref).strip()
        except RuntimeError:
            continue
    raise RuntimeError("no default branch found; pass --base")


def changed_files(base: str) -> list[str]:
    tracked = git("diff", "--name-only", base).splitlines()
    untracked = git("ls-files", "--others", "--exclude-standard").splitlines()
    return sorted({path for path in tracked + untracked if path})


def is_doc(path: str) -> bool:
    pure = PurePosixPath(path)
    if pure.parts and pure.parts[0] in DOC_DIRS:
        return True
    return pure.suffix.lower() in DOC_SUFFIXES or pure.stem.upper() in DOC_NAMES


def global_reason(path: str) -> str | None:
    pure = PurePosixPath(path)
    if path.startswith(GLOBAL_DIRS):
        return f"{path}: CI configuration"
    if pure.name in GLOBAL_NAMES:
        return f"{path}: lockfile or workspace/toolchain pin"
    if len(pure.parts) == 1:
        # A root manifest or root tool config feeds every package under it.
        if pure.name in MANIFESTS:
            return f"{path}: root manifest"
        if is_config(pure):
            return f"{path}: root-level configuration"
    return None


def is_config(pure: PurePosixPath) -> bool:
    name = pure.name
    return (
        name.startswith(".")
        or name in CONFIG_NAMES
        or pure.suffix.lower() in CONFIG_SUFFIXES
        or "config" in name.lower()
        or name.endswith("rc")
    )


def owning_package(path: str, manifest_dirs: set[str]) -> str:
    for parent in PurePosixPath(path).parents:
        key = "." if str(parent) == "." else str(parent)
        if key in manifest_dirs:
            return key
    return "."


def tracked_manifest_dirs() -> set[str]:
    dirs = set()
    for path in git("ls-files").splitlines():
        pure = PurePosixPath(path)
        if pure.name in MANIFESTS:
            dirs.add(str(pure.parent))
    return dirs or {"."}


def assess(files: list[str], manifest_dirs: set[str]) -> dict:
    code = [path for path in files if not is_doc(path)]
    reasons = [r for r in (global_reason(path) for path in code) if r]
    if not files or not code:
        verdict = "docs"
    elif reasons:
        verdict = "full"
    else:
        verdict = "impacted"
    packages = sorted({owning_package(path, manifest_dirs) for path in code})
    return {"verdict": verdict, "reasons": reasons, "packages": packages, "files": files}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", help="ref to diff against (default: merge base with the default branch)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    try:
        base = args.base or default_base()
        report = assess(changed_files(base), tracked_manifest_dirs())
    except RuntimeError as error:
        print(f"impact-scope: {error}", file=sys.stderr)
        return 2
    report["base"] = base
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"verdict: {report['verdict']}")
    print(f"base: {base}")
    for reason in report["reasons"]:
        print(f"full because: {reason}")
    print("packages: " + (", ".join(report["packages"]) or "none"))
    print(f"files: {len(report['files'])}")
    for path in report["files"]:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
