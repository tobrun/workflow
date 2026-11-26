#!/usr/bin/env python3
"""Write a pull request's verification evidence from CI, where the agent cannot write it.

Usage:
  ci-evidence.py collect --base REF --out DIR [--head REF] [--junit FILE ...] [--coverage FILE ...]
                         [--body PR_BODY.md] [--revert-cmd CMD] [--threshold 80] [--run-url URL]
                         [--workflow PATH] [--proofs FILE] [--strict]
  ci-evidence.py publish --block DIR/block.md (--pr N | --body-file FILE)
  ci-evidence.py pending --sha SHA (--pr N | --body-file FILE)
  ci-evidence.py splice NEW_BODY --from CURRENT_BODY

`collect` reads what CI itself produced - JUnit reports, a coverage report, and the
git diff between the base and the head commit - and writes DIR/evidence.json, the
markdown block DIR/block.md, and DIR/predicate.json, an in-toto test-result
predicate for actions/attest. It runs a test-diff guard (removed assertions, new
skips or focused tests, regenerated snapshots, lowered thresholds), a fails-on-revert
check (the PR's new test cases run against the base implementation, where they
should fail), changed-line coverage, whether each spec scenario's test passed (from
the body's hidden dev-acceptance marker), and the report proof-gate.py wrote when
--proofs names one (whether every Lean statement is proved, and which statements
changed since the base, for a person to review). It measures; it never compares
the agent's claims, and it fails only with --strict. Each finding is listed under
the table, prefixed with the check that found it.

`publish` replaces whatever sits between the DEV-EVIDENCE markers of the PR body
with the block, through `gh` or on a file; `pending` marks the block as waiting
for a new head commit; `splice` carries the current block into a body the agent
rewrote, so an agent's PR update never erases or forges it.
Standard library only; it is vendored into a repository as .verify/ci-evidence.py.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

BEGIN = "<!-- BEGIN DEV-EVIDENCE -->"
END = "<!-- END DEV-EVIDENCE -->"
MARKERS = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
PREDICATE_TYPE = "https://in-toto.io/attestation/test-result/v0.1"

TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|testdata)/|(^|/)test_[^/]+\.py$|_test\.(py|go)$|\.(test|spec)\.[cm]?[jt]sx?$"
    r"|(^|/)src/test/|Tests?\.(java|kt)$|(^|/)tests/.+\.rs$|_test\.(cc|cpp)$"
)
SNAPSHOT_PATH = re.compile(r"(^|/)__snapshots__/|\.snap$|\.golden$|(^|/)golden/|(^|/)snapshots/|\.approved\.")
ASSERTION = re.compile(
    r"\bassert\w*\b|\bexpect\s*\(|\bshould\b|\brequire\.\w+\(|\bt\.(Error|Errorf|Fatal|Fatalf|Fail)\b"
    r"|\b(EXPECT|ASSERT)_[A-Z]+\s*\(|\bXCTAssert|\bassert_eq!|\bassert!|\bverify\s*\("
)
SKIP = re.compile(
    r"@pytest\.mark\.(skip|xfail)|@unittest\.(skip|expectedFailure)|\bpytest\.skip\(|\.(skip|only|todo)\s*\("
    r"|\b(xit|xdescribe|xtest|fit|fdescribe)\s*\(|\bt\.Skip(f|Now)?\(|@Disabled\b|@Ignore\b|#\[ignore\]"
    r"|\bGTEST_SKIP\b|\bDISABLED_\w+"
)
THRESHOLD_FILE = re.compile(r"(^|/)(pyproject\.toml|setup\.cfg|\.coveragerc|tox\.ini|package\.json|\.nycrc[^/]*"
                            r"|(jest|vitest|vite)\.config\.[cm]?[jt]s|codecov\.ya?ml|\.github/workflows/[^/]+)$")
THRESHOLD = re.compile(r"(fail[_-]under|threshold|minimum|branches|lines|functions|statements|coverage)\W[^0-9]*([0-9]+(?:\.[0-9]+)?)",
                       re.IGNORECASE)


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    result = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=cwd, capture_output=True, check=False)
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# What CI ran
# ---------------------------------------------------------------------------


LOAD_FAILURE = re.compile(r"collection|ImportError|ModuleNotFoundError|Cannot find module|cannot find symbol"
                          r"|undefined: |unresolved import|SyntaxError|could not compile", re.IGNORECASE)


@dataclass
class Case:
    id: str
    file: str
    status: str  # passed, failed, skipped
    message: str = ""


def read_junit(paths: list[Path]) -> dict[str, Case]:
    cases: dict[str, Case] = {}
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError) as error:
            raise SystemExit(f"{path}: not a JUnit report ({error})")
        for case in root.iter("testcase"):
            key = f"{case.get('classname', '')}::{case.get('name', '')}"
            problem = case.find("failure") if case.find("failure") is not None else case.find("error")
            status = "failed" if problem is not None else "skipped" if case.find("skipped") is not None else "passed"
            message = f"{problem.get('message', '')} {problem.text or ''}" if problem is not None else ""
            cases[key] = Case(key, case.get("file", ""), status, message[:500])
    return cases


def empty_suite_errors(paths: list[Path]) -> int:
    """Errors a report states for a suite that holds no testcase (a run that crashed before any test)."""
    total = 0
    for path in paths:
        root = ET.parse(path).getroot()
        for suite in root.iter("testsuite"):
            if suite.find(".//testcase") is None:
                for key in ("errors", "failures"):
                    try:
                        total += int(suite.get(key, "0"))
                    except ValueError:
                        pass
    return total


def changed_lines(base: str, head: str, root: Path) -> dict[str, set[int]]:
    """Lines the head adds or modifies, per file that still exists."""
    out = git("diff", "-U0", "--no-color", "--no-renames", f"{base}...{head}", cwd=root)
    lines: dict[str, set[int]] = {}
    current = None
    for line in out.splitlines():
        if line.startswith("+++ "):
            current = None if line[4:] == "/dev/null" else line[6:].rstrip("\t")
            if current:
                lines.setdefault(current, set())
        elif line.startswith("@@") and current:
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            start, count = int(match.group(1)), int(match.group(2) or 1)
            lines[current].update(range(start, start + count))
    return lines


def diff_hunks(base: str, head: str, root: Path) -> dict[str, tuple[list[tuple[int, str]], list[str]]]:
    """Per changed file: added lines with their head line numbers, and removed lines."""
    out = git("diff", "-U0", "--no-color", "--no-renames", f"{base}...{head}", cwd=root)
    files: dict[str, tuple[list[tuple[int, str]], list[str]]] = {}
    current, number = None, 0
    for line in out.splitlines():
        if line.startswith("diff --git"):
            rest = line[len("diff --git "):]  # "a/P b/P": without renames both halves are the same path
            size = (len(rest) - 5) // 2
            current = rest[2 + size + 3:] if size > 0 and rest[:2] == "a/" else None
            if current:
                files.setdefault(current, ([], []))
        elif line.startswith(("+++", "---")):
            continue
        elif line.startswith("@@") and current:
            number = int(re.search(r"\+(\d+)", line).group(1))
        elif current and line.startswith("+"):
            files[current][0].append((number, line[1:]))
            number += 1
        elif current and line.startswith("-"):
            files[current][1].append(line[1:])
    return files


def status_of(base: str, head: str, root: Path) -> dict[str, str]:
    out = git("diff", "--name-status", "--no-renames", f"{base}...{head}", cwd=root)
    return {parts[-1]: parts[0][0] for parts in (line.split("\t") for line in out.splitlines() if line)}


# ---------------------------------------------------------------------------
# The test-diff guard
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    kind: str
    where: str
    text: str


def guard(base: str, head: str, root: Path) -> list[Finding]:
    findings: list[Finding] = []
    statuses = status_of(base, head, root)
    for path, status in sorted(statuses.items()):
        if status == "D" and TEST_PATH.search(path):
            findings.append(Finding("test file deleted", path, ""))
        if SNAPSHOT_PATH.search(path) and status in "MD":
            findings.append(Finding("snapshot regenerated" if status == "M" else "snapshot deleted", path, ""))
    for path, (added, removed) in sorted(diff_hunks(base, head, root).items()):
        if TEST_PATH.search(path) and not SNAPSHOT_PATH.search(path):
            kept = {text.strip() for _, text in added}
            for text in removed:
                if ASSERTION.search(text) and text.strip() not in kept:
                    findings.append(Finding("assertion removed or changed", path, text.strip()))
            for number, text in added:
                if SKIP.search(text):
                    findings.append(Finding("test skipped or focused", f"{path}:{number}", text.strip()))
        if THRESHOLD_FILE.search(path):
            before = {m.group(1).lower(): float(m.group(2)) for t in removed for m in [THRESHOLD.search(t)] if m}
            for number, text in added:
                match = THRESHOLD.search(text)
                if match and match.group(1).lower() in before and float(match.group(2)) < before[match.group(1).lower()]:
                    findings.append(Finding("threshold lowered", f"{path}:{number}",
                                            f"{match.group(1)} {before[match.group(1).lower()]:g} -> {float(match.group(2)):g}"))
    return findings


# ---------------------------------------------------------------------------
# Changed-line coverage
# ---------------------------------------------------------------------------


def read_coverage(paths: list[Path], root: Path) -> dict[str, dict[int, bool]]:
    """Per covered file (as the report names it), line -> covered. Cobertura, JaCoCo, LCOV, or a Go profile."""
    files: dict[str, dict[int, bool]] = {}

    def mark(name: str, line: int, hit: bool) -> None:
        entry = files.setdefault(name, {})
        entry[line] = entry.get(line, False) or hit

    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        if text.lstrip().startswith("<"):
            report = ET.fromstring(text)
            if report.tag == "coverage":  # Cobertura
                sources = [s.text.strip() for s in report.iter("source") if s.text]
                for cls in report.iter("class"):
                    name = cls.get("filename", "")
                    for source in sources:
                        candidate = os.path.join(source, name)
                        if os.path.isabs(candidate) and candidate.startswith(str(root)):
                            name = os.path.relpath(candidate, root)
                            break
                    for line in cls.iter("line"):
                        mark(name, int(line.get("number")), int(line.get("hits", "0")) > 0)
            else:  # JaCoCo
                for package in report.iter("package"):
                    for source in package.iter("sourcefile"):
                        name = f"{package.get('name', '')}/{source.get('name', '')}"
                        for line in source.iter("line"):
                            mark(name, int(line.get("nr")), int(line.get("ci", "0")) > 0)
        elif text.startswith("mode:"):  # Go profile
            for line in text.splitlines()[1:]:
                match = re.match(r"(.+):(\d+)\.\d+,(\d+)\.\d+ \d+ (\d+)$", line)
                if match:
                    for number in range(int(match.group(2)), int(match.group(3)) + 1):
                        mark(match.group(1), number, int(match.group(4)) > 0)
        else:  # LCOV
            current = None
            for line in text.splitlines():
                if line.startswith("SF:"):
                    current = os.path.relpath(line[3:], root) if os.path.isabs(line[3:]) else line[3:]
                elif line.startswith("DA:") and current:
                    number, hits = line[3:].split(",")[:2]
                    mark(current, int(number), int(hits) > 0)
    return files


def match_report(path: str, report: dict[str, dict[int, bool]]) -> dict[int, bool] | None:
    if path in report:
        return report[path]
    # A report may name a file by a shorter path, or (a Go profile) behind a module path like example.com/x/.
    candidates = [name for name in report
                  if path.endswith("/" + name) or (name.endswith("/" + path) and "." in name.split("/")[0])]
    return report[candidates[0]] if len(candidates) == 1 else None


@dataclass
class Coverage:
    measured: bool
    covered: int = 0
    total: int = 0
    uncovered: dict[str, list[int]] = field(default_factory=dict)
    unmeasured: list[str] = field(default_factory=list)

    @property
    def percent(self) -> float | None:
        return 100.0 * self.covered / self.total if self.total else None


def coverage(changed: dict[str, set[int]], paths: list[Path], root: Path) -> Coverage:
    if not paths:
        return Coverage(measured=False)
    report = read_coverage(paths, root)
    result = Coverage(measured=True)
    for path, lines in sorted(changed.items()):
        if TEST_PATH.search(path) or not re.search(r"\.(py|[cm]?[jt]sx?|go|java|kt|rs|c|cc|cpp|h|hpp|rb|php|swift|cs)$", path):
            continue
        hits = match_report(path, report)
        if hits is None:
            result.unmeasured.append(path)
            continue
        for line in sorted(lines):
            if line in hits:
                result.total += 1
                if hits[line]:
                    result.covered += 1
                else:
                    result.uncovered.setdefault(path, []).append(line)
    return result


def ranges(numbers: list[int]) -> str:
    spans, start = [], None
    for previous, number in zip([None] + numbers, numbers):
        if start is None or number != previous + 1:
            if start is not None:
                spans.append((start, previous))
            start = number
    if start is not None:
        spans.append((start, numbers[-1]))
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in spans)


# ---------------------------------------------------------------------------
# Fails on revert
# ---------------------------------------------------------------------------


@dataclass
class Revert:
    ran: bool
    detail: str = ""
    new_failing: list[str] = field(default_factory=list)
    new_passing: list[str] = field(default_factory=list)
    unloadable: list[str] = field(default_factory=list)  # failed on base because they could not even load


def run_tests(command: str, cwd: Path, tests: list[str], junit: Path) -> tuple[int, str]:
    rendered = command.replace("{tests}", " ".join(shlex.quote(t) for t in tests)).replace("{junit}", shlex.quote(str(junit)))
    result = subprocess.run(rendered, shell=True, cwd=cwd, capture_output=True, text=True, check=False)
    return result.returncode, (result.stdout + result.stderr)[-2000:]


def fails_on_revert(base: str, head: str, root: Path, command: str | None) -> Revert:
    if not command:
        return Revert(False, "no --revert-cmd configured")
    statuses = status_of(base, head, root)
    tests = sorted(p for p, s in statuses.items() if s in "AM" and TEST_PATH.search(p) and not SNAPSHOT_PATH.search(p))
    if not tests:
        return Revert(False, "the PR changes no test file")
    scratch = Path(tempfile.mkdtemp(prefix="ci-evidence-"))
    tree = scratch / "base"
    git("worktree", "add", "--detach", str(tree), base, cwd=root)
    try:
        for shared in ("node_modules", ".venv", "venv", "vendor"):
            if (root / shared).is_dir() and not (tree / shared).exists():
                (tree / shared).symlink_to(root / shared)
        existing = [t for t in tests if statuses[t] == "M"]
        before: dict[str, Case] = {}
        if existing:
            run_tests(command, tree, existing, scratch / "before.xml")
            before = read_junit([scratch / "before.xml"]) if (scratch / "before.xml").is_file() else {}
        for path in tests:
            target = tree / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((root / path).read_bytes())
        code, output = run_tests(command, tree, tests, scratch / "after.xml")
        if not (scratch / "after.xml").is_file():
            return Revert(False, f"the revert run wrote no JUnit report (exit {code}): {output.strip()[-300:]}")
        after = read_junit([scratch / "after.xml"])
        result = Revert(True)
        for key, case in sorted(after.items()):
            if key in before:
                continue  # existed on the base; whether it fails there says nothing about the new tests
            if case.status == "failed" and LOAD_FAILURE.search(case.message):
                result.unloadable.append(key)
            elif case.status == "failed":
                result.new_failing.append(key)
            else:
                result.new_passing.append(key)
        if not (result.new_failing or result.new_passing or result.unloadable):
            result.detail = "the changed test files add no test case"
        return result
    finally:
        git("worktree", "remove", "--force", str(tree), cwd=root, check=False)
        shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------
# The scenario mapping the PR body carries
# ---------------------------------------------------------------------------


def section(body: str, name: str) -> str:
    match = re.search(rf"^##\s+{re.escape(name)}\s*$(.*?)(?=^##\s|\Z|{re.escape(BEGIN)})", body, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else ""


def table_rows(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("|") and not re.match(r"^\|[\s:|-]+\|$", line):
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows[1:] if rows else []


ACCEPTANCE_MARKER = re.compile(r"<!--\s*dev-acceptance:\s*(\[.*?\])\s*-->", re.DOTALL)


def acceptance(body: str) -> list[dict]:
    """Each spec scenario's id and the runner case that proves it, from the hidden dev-acceptance marker.

    pr-evidence.py acceptance writes the marker from test-evidence.json; an older body's
    `## Acceptance criteria` table is still read when the marker is absent.
    """
    marker = ACCEPTANCE_MARKER.search(body)
    if marker:
        try:
            mapped = json.loads(marker.group(1))
        except ValueError:
            mapped = []
        return [{"id": str(m["id"]), "case": str(m["case"])} for m in mapped
                if isinstance(m, dict) and m.get("id") and m.get("case")]
    criteria = []
    for row in table_rows(section(body, "Acceptance criteria")):
        if len(row) >= 3 and re.match(r"^`?CS\d+-S\d+`?$", row[0]):
            criteria.append({"id": row[0].strip("`"), "case": row[2].strip("`")})
    return criteria


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------


def read_proofs(path: str | None) -> dict | None:
    """proof-gate.py's JSON report; a named file that is missing or unreadable is a gate that did not run."""
    if not path:
        return None
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return {"result": "not run", "theorems": [], "integrity": [], "unfinished": [],
                "errors": [f"no proof report at {path}: {error}"], "statements": {}}
    report.setdefault("statements", {})
    return report


def expand(patterns: list[str], missing: list[str]) -> list[Path]:
    """Files the patterns match; a pattern that matches nothing is noted, so a crashed test step still gets a block."""
    found = []
    for pattern in patterns:
        matches = sorted(glob.glob(pattern, recursive=True))
        if not matches:
            missing.append(pattern)
        found += [Path(m) for m in matches]
    return found


def collect(args: argparse.Namespace) -> int:
    root = Path(git("rev-parse", "--show-toplevel").strip())
    head = git("rev-parse", args.head, cwd=root).strip()
    base = git("merge-base", args.base, head, cwd=root).strip()
    missing: list[str] = []
    junit_files = expand(args.junit, missing)
    coverage_files = expand(args.coverage, missing)
    cases = read_junit(junit_files)
    suite_errors = empty_suite_errors(junit_files)
    body = Path(args.body).read_text(encoding="utf-8") if args.body else ""
    changed = changed_lines(base, head, root)
    guarded = guard(base, head, root)
    covered = coverage(changed, coverage_files, root)
    reverted = fails_on_revert(base, head, root, args.revert_cmd)
    criteria = acceptance(body)
    for criterion in criteria:
        case = cases.get(criterion["case"])
        criterion["status"] = case.status if case else "not found"
    counts = {s: sum(1 for c in cases.values() if c.status == s) for s in ("passed", "failed", "skipped")}
    counts["failed"] += suite_errors
    none_ran = counts["passed"] + counts["failed"] == 0
    proofs = read_proofs(args.proofs)
    weak = bool(guarded) or bool(reverted.new_passing) or (covered.percent is not None and covered.percent < args.threshold)
    weak = weak or none_ran or any(c["status"] != "passed" for c in criteria)
    weak = weak or bool(proofs and proofs["result"] in ("unfinished", "not run"))
    failed = bool(counts["failed"]) or any(c["status"] == "failed" for c in criteria) or bool(args.junit and not junit_files)
    failed = failed or bool(proofs and proofs["result"] == "integrity")
    verdict = "failed" if failed else "attention" if weak else "verified"
    evidence = {
        "head": head, "base": base, "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "run_url": args.run_url, "workflow": args.workflow, "verdict": verdict, "threshold": args.threshold,
        "tests": counts, "failed_tests": sorted(k for k, c in cases.items() if c.status == "failed"),
        "skipped_tests": sorted(k for k, c in cases.items() if c.status == "skipped"),
        "guard": [f.__dict__ for f in guarded],
        "revert": reverted.__dict__,
        "coverage": {"measured": covered.measured, "percent": covered.percent, "covered": covered.covered,
                     "total": covered.total, "uncovered": covered.uncovered, "unmeasured": covered.unmeasured},
        "acceptance": criteria, "none_ran": none_ran, "junit_reports": len(junit_files), "missing": missing,
        "proofs": proofs,
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    (out / "block.md").write_text(render(evidence), encoding="utf-8")
    (out / "predicate.json").write_text(json.dumps(predicate(evidence, cases), indent=2) + "\n", encoding="utf-8")
    print(f"ci-evidence: {verdict} at {head[:7]} - wrote {out}/block.md, evidence.json, predicate.json")
    return 1 if args.strict and verdict != "verified" else 0


def predicate(evidence: dict, cases: dict[str, Case]) -> dict:
    result = {"failed": "FAILED", "attention": "WARNED", "verified": "PASSED"}[evidence["verdict"]]
    configuration = [{"name": evidence["workflow"] or "ci", "digest": {"gitCommit": evidence["head"]}}]
    if evidence["run_url"]:
        configuration[0]["uri"] = evidence["run_url"]
    return {
        "result": result, "configuration": configuration, "url": evidence["run_url"] or "",
        "passedTests": sorted(k for k, c in cases.items() if c.status == "passed"),
        "warnedTests": sorted(evidence["revert"]["new_passing"]),
        "failedTests": evidence["failed_tests"],
    }


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------

ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌", "none": "➖"}


FINDINGS_SHOWN = 5


def findings_of(evidence: dict, base: str) -> list[tuple[str, list[str]]]:
    """Every finding, grouped by the check that found it, in the order of the table."""
    cov, revert, proofs = evidence["coverage"], evidence["revert"], evidence.get("proofs") or {}
    groups = [
        ("Failing test", [f"`{t}`" for t in evidence["failed_tests"]]),
        ("Test-diff guard", [f"`{g['where']}` {g['kind']}" + (f": `{g['text'][:100]}`" if g["text"] else "") for g in evidence["guard"]]),
        ("Fails on revert", [f"`{t}` passes on base `{base}`, so it does not prove this change" for t in revert["new_passing"]]),
        ("Coverage", [f"`{path}` lines {ranges(sorted(lines))} not covered" for path, lines in cov["uncovered"].items()]
         + [f"`{path}` changed but not in the coverage report" for path in cov["unmeasured"]]),
        ("Spec scenarios", [f"{c['id']} `{c['case']}` " + ("not in any CI report" if c["status"] == "not found" else c["status"])
                            for c in evidence["acceptance"] if c["status"] != "passed"]),
        ("Proof gate", [f"`{f['file']}`" + (f":{f['line']}" if f["line"] else "") + f" {f['rule']}: {f['detail'][:200]}"
                        for f in proofs.get("integrity", []) + proofs.get("unfinished", [])]
         + [f"error: {e[:200]}" for e in proofs.get("errors", [])]),
    ]
    return [(check, items) for check, items in groups if items]


def render(evidence: dict) -> str:
    head, base = evidence["head"][:7], evidence["base"][:7]
    title = {"verified": "✅ Verified by CI", "attention": "⚠️ Verified by CI, with findings",
             "failed": "❌ CI found failures"}[evidence["verdict"]]
    run = f"[CI run]({evidence['run_url']})" if evidence["run_url"] else "CI run"
    workflow = f" · `{evidence['workflow']}`" if evidence["workflow"] else ""
    rows = []
    tests = evidence["tests"]
    total = sum(tests.values())
    if total:
        state = "fail" if tests["failed"] else "ok"
        rows.append(("Tests", state, f"{tests['passed']:,} passed · {tests['failed']} failed · {tests['skipped']} skipped",
                     f"{evidence['junit_reports']} JUnit report(s)"))
    elif evidence["junit_reports"]:
        rows.append(("Tests", "fail", "no test ran", f"{evidence['junit_reports']} JUnit report(s) with no executed case"))
    else:
        note = f"nothing matched {', '.join(evidence['missing'])}" if evidence["missing"] else "configure --junit"
        rows.append(("Tests", "fail" if evidence["missing"] else "none", "no JUnit report", note))
    guard_items = evidence["guard"]
    kinds = {}
    for item in guard_items:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    rows.append(("Test-diff guard", "warn" if guard_items else "ok",
                 " · ".join(f"{n} {k}" for k, n in kinds.items()) if guard_items else "no test weakened, skipped, or regenerated",
                 "listed below" if guard_items else ""))
    revert = evidence["revert"]
    if revert["ran"] and (revert["new_failing"] or revert["new_passing"] or revert["unloadable"]):
        new = len(revert["new_failing"]) + len(revert["new_passing"])
        parts = [f"{len(revert['new_failing'])} of {new} new tests fail on base `{base}`"] if new else []
        if revert["unloadable"]:
            parts.append(f"{len(revert['unloadable'])} test file(s) do not load on base")
        rows.append(("Fails on revert", "warn" if revert["new_passing"] else "ok", " · ".join(parts),
                     f"{len(revert['new_passing'])} pass without the change" if revert["new_passing"] else
                     "a test that cannot load on base proves the API is new, not its behavior" if revert["unloadable"] and not new else ""))
    else:
        rows.append(("Fails on revert", "none", "not run", revert["detail"]))
    cov = evidence["coverage"]
    if cov["percent"] is not None:
        rows.append(("Changed-line coverage", "ok" if cov["percent"] >= evidence["threshold"] else "warn",
                     f"{cov['percent']:.0f}% of {cov['total']} changed lines (threshold {evidence['threshold']:g}%)",
                     f"{cov['total'] - cov['covered']} uncovered" if cov["covered"] < cov["total"] else ""))
    elif cov["measured"]:
        rows.append(("Changed-line coverage", "none", "no coverable changed line", ""))
    else:
        rows.append(("Changed-line coverage", "none", "no coverage report given", "configure --coverage"))
    criteria = evidence["acceptance"]
    if criteria:
        passing = sum(1 for c in criteria if c["status"] == "passed")
        state = "fail" if any(c["status"] == "failed" for c in criteria) else "ok" if passing == len(criteria) else "warn"
        absent = sum(1 for c in criteria if c["status"] == "not found")
        rows.append(("Spec scenarios", state, f"{passing} of {len(criteria)} scenario tests pass at this commit",
                     f"{absent} not in any CI report" if absent else ""))
    proofs = evidence.get("proofs")
    if proofs:
        proved = sum(1 for t in proofs["theorems"] if t.get("status") == "proved")
        state = {"clean": "ok", "integrity": "fail"}.get(proofs["result"], "warn")
        note = {"integrity": f"{len(proofs['integrity'])} integrity violation(s)",
                "unfinished": f"{len(proofs['theorems']) - proved} unfinished", "not run": "the gate did not run"}.get(proofs["result"], "")
        rows.append(("Proofs", state, f"{proved} of {len(proofs['theorems'])} Lean statements proved"
                     + (f" · `{proofs['toolchain']}`" if proofs.get("toolchain") else ""), note))
    lines = [BEGIN, "", "---", "", f"### {title} at `{head}`", "",
             f"{run}{workflow} · base `{base}` · {evidence['generated']}", "",
             "| Check | | Result | Note |", "| --- | :-: | --- | --- |"]
    lines += [f"| {name} | {ICON[state]} | {result} | {note} |" for name, state, result, note in rows]
    findings = findings_of(evidence, base)
    if findings:
        lines += ["", "**Findings**", ""]
        overflow = []
        for check, items in findings:
            lines += [f"- {check}: {item}" for item in items[:FINDINGS_SHOWN]]
            if len(items) > FINDINGS_SHOWN:
                lines.append(f"- {check}: +{len(items) - FINDINGS_SHOWN} more in Details")
                overflow += [f"**{check}**", ""] + [f"- {item}" for item in items[FINDINGS_SHOWN:]] + [""]
        if overflow:
            lines += ["", "<details><summary>Details</summary>", ""] + overflow + ["</details>"]
    statements = (proofs or {}).get("statements", {})
    if any(statements.values()):
        lines += ["", "**Statements to review.** A proof shows only that these hold; read each one against what it should mean.", "",
                  "| | Statement | Reading |", "| --- | --- | --- |"]
        for kind, mark in (("added", "➕ added"), ("changed", "✏️ changed"), ("removed", "➖ removed")):
            lines += [f"| {mark} | `{' '.join(e['statement'].split())[:300].replace('|', '&#124;')}` | {e['reading'] or 'no reading'} |"
                      for e in statements.get(kind, [])]
    lines += ["", f"<sub>Written by ci-evidence.py in CI from the commit's own runs; anything edited here by hand is replaced on the next push. "
              f"The agent-written sections above are claims; this block is what CI measured.</sub>", END]
    inner = [line.replace("<!--", "&lt;!--").replace("-->", "--&gt;") for line in lines[1:-1]]
    return "\n".join([lines[0], *inner, lines[-1]]) + "\n"


def pending_block(sha: str) -> str:
    return (f"{BEGIN}\n### ⏳ Verifying `{sha[:7]}`\n\nCI is collecting evidence for the new head commit; "
            f"earlier results no longer apply.\n{END}\n")


# ---------------------------------------------------------------------------
# publish, pending, splice
# ---------------------------------------------------------------------------


def place(body: str, block: str) -> str:
    if MARKERS.search(body):
        return MARKERS.sub(lambda _: block.rstrip("\n"), body, count=1)
    return body.rstrip("\n") + "\n\n" + block


def write_body(args: argparse.Namespace, block: str) -> int:
    if args.body_file:
        path = Path(args.body_file)
        path.write_text(place(path.read_text(encoding="utf-8"), block), encoding="utf-8")
        return 0
    current = subprocess.run(["gh", "pr", "view", str(args.pr), "--json", "body", "-q", ".body"],
                             capture_output=True, text=True, check=False)
    if current.returncode != 0:
        print(f"gh pr view failed: {current.stderr.strip()}", file=sys.stderr)
        return 1
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(place(current.stdout, block))
    edited = subprocess.run(["gh", "pr", "edit", str(args.pr), "--body-file", handle.name],
                            capture_output=True, text=True, check=False)
    os.unlink(handle.name)
    if edited.returncode != 0:
        print(f"gh pr edit failed: {edited.stderr.strip()}", file=sys.stderr)
    return edited.returncode


def splice(args: argparse.Namespace) -> int:
    new = Path(args.body)
    current = Path(args.source).read_text(encoding="utf-8") if Path(args.source).is_file() else ""
    found = MARKERS.search(current)
    block = found.group(0) if found else f"{BEGIN}\n{END}"
    new.write_text(place(new.read_text(encoding="utf-8"), block + "\n"), encoding="utf-8")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ci-evidence.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    co = sub.add_parser("collect")
    co.add_argument("--base", required=True)
    co.add_argument("--head", default="HEAD")
    co.add_argument("--out", required=True)
    co.add_argument("--junit", action="append", default=[], help="JUnit XML report or glob, repeatable")
    co.add_argument("--coverage", action="append", default=[], help="Cobertura, JaCoCo, LCOV, or Go profile")
    co.add_argument("--body", help="the PR body, for its dev-acceptance scenario mapping")
    co.add_argument("--revert-cmd", help="runs tests on the base: {tests} is the changed test files, {junit} the report path")
    co.add_argument("--threshold", type=float, default=80.0, help="changed-line coverage the block flags below")
    co.add_argument("--run-url")
    co.add_argument("--workflow")
    co.add_argument("--proofs", help="the JSON report proof-gate.py check --base wrote")
    co.add_argument("--strict", action="store_true", help="exit 1 unless the verdict is verified")
    for name in ("publish", "pending"):
        p = sub.add_parser(name)
        target = p.add_mutually_exclusive_group(required=True)
        target.add_argument("--pr")
        target.add_argument("--body-file")
        if name == "publish":
            p.add_argument("--block", required=True)
        else:
            p.add_argument("--sha", required=True)
    sp = sub.add_parser("splice")
    sp.add_argument("body", help="the new body the agent wrote")
    sp.add_argument("--from", dest="source", required=True, help="the PR's current body")
    args = parser.parse_args(argv[1:])
    if args.command == "collect":
        return collect(args)
    if args.command == "publish":
        return write_body(args, Path(args.block).read_text(encoding="utf-8"))
    if args.command == "pending":
        return write_body(args, pending_block(args.sha))
    return splice(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
