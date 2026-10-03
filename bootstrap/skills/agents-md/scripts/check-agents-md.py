#!/usr/bin/env python3
"""Check that a repository's root AGENTS.md is well-formed and matches the repo.

Usage:
  python3 check-agents-md.py AGENTS.md [--root DIR] [--max-lines N]

Exit 2 when the file is missing or the arguments are bad, 1 with one violation
per line, 0 when clean. Lines starting with "note:" never change the exit code;
they name facts the script cannot settle statically (a command it does not
resolve, a default branch it cannot read), for the caller to confirm by hand.

Violations: a required section missing, out of order, or duplicated; the file
over --max-lines; an em dash or a leftover <<fill placeholder; a Commands slot
missing or malformed, or "none" without an open item; a malformed or unknown
open item; a backticked path or relative link that does not exist under --root;
a command whose script, target, recipe, or file does not exist; a default
branch that disagrees with origin/HEAD; and a line repeated verbatim.
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

SECTIONS = ["Commands", "Tests", "Quality", "Architecture", "Contributions", "Maintaining this file"]
SLOTS = {
    "Setup": "setup",
    "Build": "build",
    "Check": "check",
    "Unit": "unit",
    "Integration": "integration",
    "E2E": "e2e",
    "Merge gate": "ci",
}
SLUGS = {
    "setup", "build", "check", "unit", "integration", "e2e", "e2e-env", "browser", "ci",
    "security", "architecture", "decisions", "contracts", "dependencies", "commits",
    "plans", "pr", "branch",
}
EM_DASH = "\u2014"
FILE_EXTENSIONS = {
    "astro", "c", "cfg", "cjs", "cpp", "cs", "css", "dart", "env", "ex", "exs", "gif", "go", "gradle",
    "graphql", "h", "hcl", "hpp", "html", "ico", "ini", "java", "jpeg", "jpg", "js", "json", "jsonc",
    "jsx", "kt", "kts", "less", "lock", "lua", "md", "mdx", "mjs", "mk", "php", "png", "proto", "py",
    "rb", "robot", "rs", "sass", "scala", "scss", "sh", "sql", "svelte", "svg", "swift", "tf", "toml",
    "ts", "tsx", "txt", "vue", "webp", "xml", "yaml", "yml", "zig",
}
# A token such as `kebab-case.mdx` names a convention, not a file.
NAMING_CONVENTIONS = {
    "kebab-case", "camelcase", "pascalcase", "snake_case", "screaming_snake_case", "lowercase", "uppercase",
}
FILE_RUNNERS = {"python", "python3", "node", "bash", "sh", "zsh", "ruby", "perl", "tsx", "ts-node"}
NOTE_RUNNERS = {
    "npx", "bunx", "pnpx", "uv", "uvx", "poetry", "pipenv", "hatch", "pdm", "rye", "cargo", "go",
    "pytest", "tox", "nox", "docker", "docker-compose", "gradle", "mvn", "dotnet", "swift",
    "xcodebuild", "flutter", "dart", "mix", "bundle", "rake", "composer", "php", "deno",
}
PM_BUILTINS = {
    "npm": {"install", "i", "ci", "exec", "init", "publish", "audit", "outdated", "update", "ls", "link", "pack", "version"},
    "pnpm": {"install", "i", "add", "remove", "rm", "update", "up", "exec", "dlx", "init", "list", "ls", "why",
             "outdated", "publish", "link", "unlink", "store", "config", "audit", "create", "fetch", "patch",
             "rebuild", "prune", "deploy", "import", "env", "setup", "root", "bin", "help"},
    "yarn": {"install", "add", "remove", "up", "upgrade", "dlx", "exec", "init", "info", "why", "set", "config",
             "cache", "npm", "plugin", "constraints", "audit", "link", "unlink", "pack", "version"},
    "bun": {"install", "i", "add", "remove", "rm", "update", "upgrade", "test", "x", "create", "init", "build",
            "pm", "link", "unlink", "publish", "outdated", "audit", "patch", "exec", "repl", "info", "why"},
}
RUNNER_SCRIPTS = {"test": "test", "t": "test", "start": "start"}

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
BACKTICK = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\]\(([^)\s]+)\)")
SLOT_LINE = re.compile(r"^- ([A-Za-z0-9 ]+?):\s*(.*)$")
OPEN_ITEM = re.compile(r"^- \[ \] ([a-z0-9-]+): (\S.*)$")
DEFAULT_BRANCH = re.compile(r"[Dd]efault branch is `([^`]+)`")


class Doc:
    """AGENTS.md split into numbered lines outside fenced code blocks, and H2 sections."""

    def __init__(self, text: str):
        self.lines: list[tuple[int, str]] = []
        fenced = False
        for number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("```"):
                fenced = not fenced
                continue
            if not fenced:
                self.lines.append((number, line))
        self.h1: list[int] = []
        self.h2: list[tuple[int, str]] = []
        for number, line in self.lines:
            match = HEADING.match(line)
            if match and len(match.group(1)) == 1:
                self.h1.append(number)
            elif match and len(match.group(1)) == 2:
                self.h2.append((number, match.group(2)))

    def section(self, name: str) -> list[tuple[int, str]]:
        for index, (start, title) in enumerate(self.h2):
            if title == name:
                end = self.h2[index + 1][0] if index + 1 < len(self.h2) else float("inf")
                return [(n, line) for n, line in self.lines if start < n < end]
        return []

    def intro(self) -> list[tuple[int, str]]:
        first = self.h2[0][0] if self.h2 else float("inf")
        return [(n, line) for n, line in self.lines if n < first]


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def ignored(root: Path, relative: str) -> bool:
    # A trailing slash lets a directory-only pattern such as `dist/` match a path that does not exist yet.
    bare = relative.rstrip("/")
    return any(git(root, "check-ignore", "-q", form).returncode == 0 for form in (bare, bare + "/"))


def exists(base: Path, raw: str) -> bool:
    candidate = raw.strip().rstrip("/")
    if (base / candidate).exists():
        return True
    return any(ch in candidate for ch in "*?[") and bool(glob.glob(str(base / candidate), recursive=True))


def tracked_basenames(root: Path) -> set[str]:
    listed = git(root, "ls-files", "--cached", "--others", "--exclude-standard")
    if listed.returncode == 0:
        return {Path(line).name for line in listed.stdout.splitlines()}
    return {path.name for path in root.rglob("*") if ".git" not in path.parts}


def path_candidate(root: Path, token: str) -> bool:
    """True when a backticked token reads as a repository path rather than prose or a ref."""
    if any(ch.isspace() for ch in token) or "://" in token:
        return False
    if token.startswith(("/", "~", "@", "$", "-", ".dev/")) or token == ".dev":
        return False
    if any(ch in token for ch in "{}<>()=|;,'\""):
        return False
    last = token.rstrip("/").rsplit("/", 1)[-1]
    if last.split(".", 1)[0].lower() in NAMING_CONVENTIONS:
        return False
    has_extension = "." in last and last.rsplit(".", 1)[-1].lower() in FILE_EXTENSIONS
    if "/" not in token:
        return has_extension
    first = token.split("/", 1)[0]
    return has_extension or token.endswith("/") or (root / first).exists()


def check_structure(doc: Doc, violations: list[str]) -> None:
    if len(doc.h1) != 1:
        violations.append(f"structure: expected exactly one '# ' title, found {len(doc.h1)}")
    titles = [title for _, title in doc.h2]
    for title in sorted({t for t in titles if titles.count(t) > 1}):
        violations.append(f"structure: '## {title}' appears more than once")
    for name in SECTIONS:
        if name not in titles:
            violations.append(f"structure: missing '## {name}'")
    present = [t for t in dict.fromkeys(titles) if t in SECTIONS]
    if present != [name for name in SECTIONS if name in present]:
        violations.append(f"structure: sections out of order, expected {', '.join(SECTIONS)}")
    if titles and "Maintaining this file" in titles and titles[-1] != "Maintaining this file":
        violations.append("structure: '## Maintaining this file' must be the last section")
    if not any(DEFAULT_BRANCH.search(line) for _, line in doc.intro()):
        violations.append("structure: the intro has no 'Default branch is `x`' sentence")


def check_text(text: str, max_lines: int, violations: list[str]) -> None:
    lines = text.splitlines()
    if len(lines) > max_lines:
        violations.append(f"length: {len(lines)} lines, over the {max_lines}-line cap")
    for number, line in enumerate(lines, 1):
        if EM_DASH in line:
            violations.append(f"em-dash: line {number}")
        if "<<fill" in line:
            violations.append(f"placeholder: line {number} still has a <<fill slot")


def open_items(doc: Doc, violations: list[str]) -> set[str]:
    slugs: set[str] = set()
    seen: set[tuple[str, str]] = set()
    section = doc.section("Open items")
    if any(title == "Open items" for _, title in doc.h2) and not any(line.strip() for _, line in section):
        violations.append("open-items: '## Open items' is empty; remove it when there are none")
    for number, line in section:
        if not line.strip() or line.startswith((" ", "\t")):
            continue
        match = OPEN_ITEM.match(line)
        if not match:
            violations.append(f"open-items: line {number} is not '- [ ] slug: text'")
            continue
        slug, detail = match.group(1), match.group(2).strip()
        if slug not in SLUGS:
            violations.append(f"open-items: line {number} uses unknown slug '{slug}'")
        if (slug, detail) in seen:
            violations.append(f"open-items: line {number} repeats an earlier item")
        seen.add((slug, detail))
        slugs.add(slug)
    return slugs


def check_slots(doc: Doc, item_slugs: set[str], violations: list[str]) -> None:
    found: dict[str, int] = {}
    for number, line in doc.section("Commands"):
        match = SLOT_LINE.match(line)
        if not match or match.group(1) not in SLOTS:
            continue
        label, value = match.group(1), match.group(2).strip()
        found[label] = found.get(label, 0) + 1
        if value.startswith("`"):
            continue
        if re.match(r"^n/a \(.+\)", value):
            continue
        if re.match(r"^none\b", value):
            if SLOTS[label] not in item_slugs:
                violations.append(f"commands: '{label}: none' (line {number}) has no '- [ ] {SLOTS[label]}:' open item")
            continue
        violations.append(f"commands: '{label}' (line {number}) must be a backticked command, 'none', or 'n/a (reason)'")
    for label in SLOTS:
        if found.get(label, 0) == 0:
            violations.append(f"commands: missing the '- {label}:' slot")
        elif found[label] > 1:
            violations.append(f"commands: the '{label}' slot appears {found[label]} times")


def check_paths(root: Path, doc: Doc, violations: list[str]) -> None:
    skipped = {n for n, _ in doc.section("Open items")}
    basenames: set[str] | None = None
    for number, line in doc.lines:
        if number in skipped:
            continue
        for raw in BACKTICK.findall(line):
            token = raw.strip()
            if not path_candidate(root, token) or ignored(root, token):
                continue
            if "/" in token:
                if not exists(root, token):
                    violations.append(f"stale: `{token}` (line {number}) does not exist under {root}")
            else:
                if basenames is None:
                    basenames = tracked_basenames(root)
                if not exists(root, token) and not fnmatch.filter(basenames, token):
                    violations.append(f"stale: `{token}` (line {number}) matches no file in the repository")
        for target in LINK.findall(line):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative = target.split("#", 1)[0]
            if relative and not exists(root, relative) and not ignored(root, relative):
                violations.append(f"stale: link '{target}' (line {number}) does not exist under {root}")


def package_scripts(directory: Path) -> dict[str, str] | None:
    manifest = directory / "package.json"
    if not manifest.exists():
        return None
    try:
        return json.loads(manifest.read_text(encoding="utf-8")).get("scripts", {}) or {}
    except (json.JSONDecodeError, AttributeError):
        return None


def make_targets(directory: Path) -> tuple[set[str], bool] | None:
    files = [directory / name for name in ("GNUmakefile", "makefile", "Makefile") if (directory / name).exists()]
    if not files:
        return None
    files += sorted(directory.glob("*.mk")) + sorted(directory.glob("*/*.mk"))
    targets: set[str] = set()
    catch_all = False
    for path in files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"^([^\s:#=][^:#=]*?)\s*::?(?!=)", line)
            if not match:
                continue
            for name in match.group(1).split():
                if "%" in name:
                    catch_all = True
                targets.add(name)
    return targets, catch_all


def just_recipes(directory: Path) -> set[str] | None:
    for name in ("justfile", "Justfile", ".justfile"):
        path = directory / name
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            return {m.group(1) for m in re.finditer(r"^@?([A-Za-z0-9_-]+)[^:\n=]*:(?!=)", text, re.MULTILINE)}
    return None


def task_names(directory: Path) -> set[str] | None:
    for name in ("Taskfile.yml", "Taskfile.yaml", "taskfile.yml", "taskfile.yaml"):
        path = directory / name
        if not path.exists():
            continue
        names: set[str] = set()
        inside = False
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if re.match(r"^tasks:\s*$", line):
                inside = True
            elif re.match(r"^\S", line):
                inside = False
            elif inside:
                match = re.match(r"^  ([A-Za-z0-9_.:-]+):", line)
                if match:
                    names.add(match.group(1))
        return names
    return None


def segments(command: str) -> list[list[str]]:
    parts = re.split(r"\s*(?:&&|\|\||;)\s*", command)
    result = []
    for part in parts:
        part = part.split("|", 1)[0].strip()
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        while words and re.match(r"^[A-Z_][A-Z0-9_]*=", words[0]):
            words = words[1:]
        if words:
            result.append(words)
    return result


def declared_dependencies(*directories: Path) -> set[str]:
    names: set[str] = set()
    for directory in directories:
        manifest = directory / "package.json"
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for key in ("dependencies", "devDependencies", "optionalDependencies"):
            names.update((data.get(key) or {}).keys())
    return names


def path_arguments_exist(args: list[str], cwd: Path, rel: str) -> str:
    """Test runners take path prefixes as filters; a slashed argument must prefix something real."""
    for arg in args:
        if "/" in arg and not arg.startswith(("-", "/")) and "*" not in arg:
            if not glob.glob(glob.escape(str(cwd / arg)) + "*"):
                return f"`{arg}` matches nothing in {rel}"
    return ""


def resolve_package_script(runner: str, words: list[str], cwd: Path, rel: str, root: Path) -> str | None:
    """Return a violation, "" when resolved, or None when not statically decidable."""
    args = words[1:]
    if not args:
        return ""
    first = args[0]
    if first.startswith("-") or first in {"workspace", "workspaces"}:
        return None
    if first in {"run", "run-script"}:
        if len(args) < 2:
            return ""
        name = args[1]
        if name.startswith("-"):
            return None
    elif runner != "bun" and first in RUNNER_SCRIPTS:
        name = RUNNER_SCRIPTS[first]
    elif first == "test":
        return path_arguments_exist(args[1:], cwd, rel)
    elif first in PM_BUILTINS[runner]:
        return ""
    elif runner == "npm":
        return None
    else:
        name = first
    if runner == "bun" and (cwd / name).is_file():
        return ""
    manifest = "package.json" if rel == "." else f"{rel}/package.json"
    scripts = package_scripts(cwd)
    if scripts is not None and name in scripts:
        return ""
    # bun and yarn (and an implicit pnpm call) also run a dependency's binary, e.g. `bun turbo`.
    runs_binaries = runner in {"bun", "yarn"} or (runner == "pnpm" and first not in {"run", "run-script"})
    if runs_binaries and ((cwd / "node_modules" / ".bin" / name).exists() or name in declared_dependencies(cwd, root)):
        return None
    if scripts is None:
        return f"no readable {manifest} for `{name}`"
    return f"`{name}` is not a script in {manifest}"


def resolve(words: list[str], cwd: Path, root: Path) -> str | None:
    runner = words[0]
    rel = str(cwd.relative_to(root)) if cwd != root else "."
    if runner in PM_BUILTINS:
        return resolve_package_script(runner, words, cwd, rel, root)
    if runner == "make":
        args = words[1:]
        if "-C" in args:
            index = args.index("-C")
            if index + 1 < len(args):
                cwd = cwd / args[index + 1]
                rel = str(cwd.relative_to(root))
                args = args[:index] + args[index + 2:]
        wanted = [a for a in args if not a.startswith("-") and "=" not in a]
        found = make_targets(cwd)
        if found is None:
            return f"no Makefile in {rel}"
        targets, catch_all = found
        missing = [t for t in wanted if t not in targets]
        if missing and catch_all:
            return None
        return f"make target(s) {', '.join(missing)} not defined in {rel}" if missing else ""
    if runner == "just":
        recipes = just_recipes(cwd)
        if recipes is None:
            return f"no justfile in {rel}"
        wanted = [a for a in words[1:2] if not a.startswith("-")]
        return f"just recipe `{wanted[0]}` not defined in {rel}" if wanted and wanted[0] not in recipes else ""
    if runner == "task":
        names = task_names(cwd)
        if names is None:
            return f"no Taskfile in {rel}"
        wanted = [a for a in words[1:2] if not a.startswith("-")]
        return f"task `{wanted[0]}` not defined in {rel}" if wanted and wanted[0] not in names else ""
    if runner in FILE_RUNNERS:
        args = words[1:]
        if not args or args[0] in {"-m", "-c", "-e", "--eval"}:
            return ""
        target = next((a for a in args if not a.startswith("-")), None)
        if target is None:
            return ""
        return "" if (cwd / target).exists() else f"`{target}` does not exist in {rel}"
    if runner.startswith("./"):
        return "" if (cwd / runner).exists() else f"`{runner}` does not exist in {rel}"
    if runner in NOTE_RUNNERS:
        return None
    return ""


def check_commands(root: Path, doc: Doc, violations: list[str], notes: list[str]) -> int:
    skipped = {n for n, _ in doc.section("Open items")}
    in_commands = {n for n, _ in doc.section("Commands")}
    resolved = 0
    known = set(PM_BUILTINS) | FILE_RUNNERS | NOTE_RUNNERS | {"make", "just", "task", "cd"}
    for number, line in doc.lines:
        if number in skipped:
            continue
        for raw in BACKTICK.findall(line):
            command = raw.strip()
            parts = segments(command)
            if not parts:
                continue
            if not (parts[0][0] in known or parts[0][0].startswith("./")):
                if number in in_commands and " " in command:
                    notes.append(f"note: `{command}` (line {number}) uses a runner this script does not know; confirm it runs")
                continue
            cwd = root
            for words in parts:
                if words[0] == "cd":
                    target = words[1] if len(words) > 1 else "."
                    if not (cwd / target).is_dir():
                        violations.append(f"command: `{command}` (line {number}) cds into missing `{target}`")
                        break
                    cwd = (cwd / target).resolve()
                    continue
                outcome = resolve(words, cwd, root)
                if outcome is None:
                    notes.append(f"note: `{command}` (line {number}) is not resolved statically; confirm it runs")
                elif outcome:
                    violations.append(f"command: `{command}` (line {number}): {outcome}")
                else:
                    resolved += 1
    return resolved


def check_branch(root: Path, doc: Doc, violations: list[str], notes: list[str]) -> None:
    stated = next((m.group(1) for _, line in doc.intro() if (m := DEFAULT_BRANCH.search(line))), None)
    if stated is None:
        return
    head = git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head.returncode != 0:
        notes.append(f"note: origin/HEAD does not resolve locally; confirm the default branch `{stated}` another way")
        return
    actual = head.stdout.strip().split("/", 1)[-1]
    if actual != stated:
        violations.append(f"branch: the file says `{stated}` but origin/HEAD is `{actual}`")


def check_duplicates(doc: Doc, violations: list[str]) -> None:
    seen: dict[str, int] = {}
    for number, line in doc.lines:
        if HEADING.match(line):
            continue
        normal = " ".join(re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", line).lower().split())
        if len(normal) < 12 or set(normal) <= set("|-: "):
            continue
        if normal in seen:
            violations.append(f"duplicate: line {number} repeats line {seen[normal]}")
        else:
            seen[normal] = number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("agents_md")
    parser.add_argument("--root", default=".")
    parser.add_argument("--max-lines", type=int, default=150)
    try:
        args = parser.parse_args()
    except SystemExit:
        return 2
    root = Path(args.root).resolve()
    path = Path(args.agents_md)
    if not path.is_file():
        print(f"missing: {path}")
        return 2
    if not root.is_dir():
        print(f"missing: --root {root} is not a directory")
        return 2

    text = path.read_text(encoding="utf-8", errors="replace")
    doc = Doc(text)
    violations: list[str] = []
    notes: list[str] = []
    check_structure(doc, violations)
    check_text(text, args.max_lines, violations)
    item_slugs = open_items(doc, violations)
    check_slots(doc, item_slugs, violations)
    check_paths(root, doc, violations)
    resolved = check_commands(root, doc, violations, notes)
    check_branch(root, doc, violations, notes)
    check_duplicates(doc, violations)

    for note in dict.fromkeys(notes):
        print(note)
    if violations:
        print("\n".join(violations))
        return 1
    print(f"check-agents-md: ok ({len(text.splitlines())} lines, {resolved} commands resolved, {len(set(notes))} notes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
