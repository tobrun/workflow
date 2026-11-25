#!/usr/bin/env python3
"""Check a Lean proof project the way an agent cannot argue with.

Usage:
  proof-gate.py check [--project formal] [--spec SPEC.md] [--base REF] [--json OUT] [--recheck]

The project is a Lake package per references/proofs.md: a pinned `lean-toolchain`,
`{Pkg}/Spec.lean` holding each human-approved statement as `def NAME : Prop := ...`,
and proofs elsewhere, each named `NAME_holds` in the statement's namespace.

`check` scans every source with comments and strings stripped for constructs that
widen the trusted base or bend the syntax (axiom, native_decide, implemented_by,
extern, unsafe, debug options, notation, macros, syntax, elaborators, run_cmd),
builds every module with `lake build`, then elaborates a generated file that restates each Spec
constant as a theorem proved by its `_holds` proof and prints its axioms. Only
propext, Classical.choice, and Quot.sound may appear; sorryAx marks a proof as
unfinished. With --spec it compares Spec.lean with the spec's `### Theorems` block;
with --base it lists the statements added, changed, or removed since that commit,
for CI, where no spec exists. With --recheck it replays the modules through
lean4checker when it is installed, and notes the skip when it is not.

Exit 0 when every statement is proved, 1 on any integrity violation (must pass),
2 when the only findings are unfinished proofs (fix and report), 3 when the gate
could not run. Standard library only; vendored into a repository as .verify/proof-gate.py.
Elaborating Lean runs code, so run it where no secret is reachable.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import asdict, dataclass, field
from pathlib import Path

ALLOWED_AXIOMS = ("propext", "Classical.choice", "Quot.sound")
SORRY_AXIOM = "sorryAx"
# Each banned construct, the token that marks it, and why it is banned.
BANNED = (
    (r"\baxiom\b", "axiom", "a new axiom can prove anything"),
    (r"\bnative_decide\b|\+native\b", "native_decide", "trusts the compiler through Lean.ofReduceBool"),
    (r"\bimplemented_by\b", "implemented_by", "runs code other than the definition the proofs see"),
    (r"@\[\s*extern\b|\bextern\b", "extern", "runs foreign code the proofs never see"),
    (r"\bunsafe\b", "unsafe", "escapes the kernel's checks"),
    (r"\bset_option\s+debug\.", "debug option", "debug options such as skipKernelTC switch off checks"),
    (r"\bskipKernelTC\b", "skipKernelTC", "skips the kernel's type check"),
    (r"\b(?:notation|infix|infixl|infixr|prefix|postfix)\b", "notation", "can make a statement read as something else"),
    (r"\b(?:macro|macro_rules|syntax|declare_syntax_cat)\b", "macro", "can rewrite what a statement or proof means"),
    (r"\b(?:elab|elab_rules)\b", "elaborator", "can add declarations around the kernel's view"),
    (r"\b(?:run_cmd|run_elab|run_meta|initialize|builtin_initialize)\b", "meta code", "runs code that can change the environment"),
    (r"\bLean\.(?:ofReduceBool|trustCompiler)\b", "compiler trust", "trusts the compiler instead of the kernel"),
)
UNFINISHED = ((r"\bsorry\b", "sorry"), (r"\badmit\b", "admit"))
STATEMENT = re.compile(r"^def\s+([A-Za-z_][\w']*)\s*:\s*Prop\s*:=", re.MULTILINE)
COMMAND = re.compile(
    r"^(?:def|theorem|lemma|example|abbrev|instance|structure|inductive|class|namespace|end|section|open|"
    r"variable|universe|noncomputable|private|protected|set_option|attribute|#|@\[|/--|/-!)", re.MULTILINE)
NAMESPACE = re.compile(r"^(namespace|section|end)\b\s*([\w.']*)", re.MULTILINE)
DEPENDS = re.compile(r"'([^']+)' depends on axioms: \[([^\]]*)\]", re.DOTALL)
NO_AXIOMS = re.compile(r"'([^']+)' does not depend on any axioms")
# Lean 4.36 writes `error(lean.unknownIdentifier): Unknown identifier`; older releases write `error: unknown identifier`.
LOCATED = re.compile(r"^(?:.*?):(\d+):\d+: error(?:\([^)]*\))?:\s*(.*)$", re.MULTILINE)


@dataclass
class Finding:
    rule: str
    file: str
    line: int
    detail: str


@dataclass
class Theorem:
    name: str
    reading: str
    statement: str
    status: str = "not checked"
    axioms: list[str] = field(default_factory=list)


@dataclass
class Report:
    project: str
    toolchain: str = ""
    result: str = "clean"
    theorems: list[Theorem] = field(default_factory=list)
    integrity: list[Finding] = field(default_factory=list)
    unfinished: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    statements: dict[str, list[dict]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Lean source text
# ---------------------------------------------------------------------------


def strip(text: str) -> str:
    """The source with comments, strings, and char literals blanked, line numbers kept."""
    out, i, n = [], 0, len(text)

    def blank(chunk: str) -> str:
        return re.sub(r"[^\n]", " ", chunk)

    while i < n:
        if text.startswith("--", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append(blank(text[i:end]))
            i = end
        elif text.startswith("/-", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if text.startswith("/-", j):
                    depth, j = depth + 1, j + 2
                elif text.startswith("-/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            out.append(blank(text[i:j]))
            i = j
        elif text[i] == '"' or (text.startswith('r"', i) or text.startswith("r#", i)) and not (i and (text[i - 1].isalnum() or text[i - 1] == "_")):
            raw = text[i] == "r"
            j = i + 1
            if raw:
                hashes = 0
                while j < n and text[j] == "#":
                    hashes, j = hashes + 1, j + 1
                close = '"' + "#" * hashes
                j = text.find(close, j + 1)
                j = n if j < 0 else j + len(close)
            else:
                while j < n and text[j] != '"':
                    j += 2 if text[j] == "\\" else 1
                j = min(j + 1, n)
            out.append(blank(text[i:j]))
            i = j
        elif text[i] == "'" and not (i and (text[i - 1].isalnum() or text[i - 1] in "_'.")):
            literal = re.match(r"'(?:\\.[^']*|[^'\\\n])'", text[i:])
            if literal:
                out.append(blank(literal.group(0)))
                i += len(literal.group(0))
            else:
                out.append(text[i])
                i += 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def normalize(statement: str) -> str:
    return " ".join(strip(statement).split())


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def statements(text: str) -> list[Theorem]:
    """Each `def NAME : Prop :=` of a Spec.lean, qualified by its namespace, with the docstring above it."""
    stripped = strip(text)
    found = []
    for match in STATEMENT.finditer(stripped):
        nxt = COMMAND.search(stripped, match.end())
        end = nxt.start() if nxt else len(stripped)
        scope: list[str] = []
        for ns in NAMESPACE.finditer(stripped, 0, match.start()):
            kind, name = ns.groups()
            if kind != "end":
                scope.append(name if kind == "namespace" else "")
            elif scope:
                scope.pop()
        before = text[: match.start()]
        opened = before.rfind("/--")
        doc = re.fullmatch(r"/--(.*?)-/\s*", before[opened:], re.DOTALL) if opened >= 0 else None
        reading = " ".join(doc.group(1).split()) if doc else ""
        qualified = ".".join([part for part in scope if part] + [match.group(1)])
        found.append(Theorem(qualified, reading, " ".join(stripped[match.start():end].split())))
    return found


def spec_statements(spec: str) -> dict[str, str]:
    """The `def NAME : Prop :=` statements in the spec's ### Theorems block, by bare name."""
    block = re.search(r"^###\s+Theorems\s*$(.*?)(?=^#{1,3}\s|\Z)", spec, re.MULTILINE | re.DOTALL)
    found = {}
    if not block:
        return found
    for fence in re.finditer(r"```lean\s*\n(.*?)```", block.group(1), re.DOTALL):
        for theorem in statements(textwrap.dedent(fence.group(1))):
            found[theorem.name] = theorem.statement
    return found


# ---------------------------------------------------------------------------
# The project
# ---------------------------------------------------------------------------


def sources(project: Path) -> list[Path]:
    return sorted(p for p in project.rglob("*.lean")
                  if not any(part.startswith(".") for part in p.relative_to(project).parts) and p.name != "lakefile.lean")


def module(project: Path, path: Path) -> str:
    return ".".join(path.relative_to(project).with_suffix("").parts)


def modules(project: Path) -> list[str]:
    """Every module the proofs may live in; the oracle's Main is an executable root, not a library module."""
    return [module(project, p) for p in sources(project) if p.name != "Main.lean"]


def scan(project: Path, report: Report) -> None:
    for path in sources(project):
        text = path.read_text(encoding="utf-8")
        stripped = strip(text)
        where = str(path.relative_to(project))
        for pattern, rule, why in BANNED:
            for match in re.finditer(pattern, stripped):
                report.integrity.append(Finding(rule, where, line_of(stripped, match.start()), why))
        for pattern, rule in UNFINISHED:
            for match in re.finditer(pattern, stripped):
                report.unfinished.append(Finding(rule, where, line_of(stripped, match.start()), "an unfinished proof"))


def spec_files(project: Path) -> list[Path]:
    return [p for p in sources(project) if p.name == "Spec.lean"]


def lake(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["lake", *args], cwd=project, capture_output=True, text=True, check=False)


def gate_file(theorems: list[Theorem], modules: list[str]) -> tuple[str, dict[int, Theorem]]:
    """Restate each Spec constant as a theorem its `_holds` proof must prove, and print its axioms.

    Returns the file and, by line number, the statement each restating line checks; an
    error on a `#print axioms` line only echoes the one above it, so those are not mapped.
    """
    lines = [f"import {m}" for m in modules] + [""]
    at: dict[int, Theorem] = {}
    for index, theorem in enumerate(theorems):
        at[len(lines) + 1] = theorem
        lines.append(f"theorem ProofGate.t{index} : {theorem.name} := {theorem.name}_holds")
        lines.append(f"#print axioms ProofGate.t{index}")
    return "\n".join(lines) + "\n", at


def check_axioms(project: Path, report: Report) -> None:
    text, at = gate_file(report.theorems, modules(project))
    with tempfile.TemporaryDirectory() as tmp:
        gate = Path(tmp) / "ProofGate.lean"
        gate.write_text(text, encoding="utf-8")
        result = lake(project, "env", "lean", str(gate))
    output = result.stdout + result.stderr
    by_index = {f"ProofGate.t{i}": t for i, t in enumerate(report.theorems)}
    for line, message in LOCATED.findall(output):
        theorem = at.get(int(line))
        if theorem is None:
            if int(line) - 1 not in at:
                report.errors.append(message.strip())
        elif re.search(r"unknown (?:identifier|constant)", message, re.IGNORECASE):
            theorem.status = "unfinished"
            report.unfinished.append(Finding("no proof", "Spec.lean", 0, f"{theorem.name} has no {theorem.name}_holds"))
        else:
            theorem.status = "integrity"
            report.integrity.append(Finding("wrong statement", "Spec.lean", 0,
                                            f"{theorem.name}_holds does not prove {theorem.name}: {message.strip()[:200]}"))
    for name, listed in DEPENDS.findall(output):
        theorem = by_index.get(name)
        if theorem is None:
            continue
        theorem.axioms = [a.strip() for a in listed.split(",") if a.strip()]
        extra = [a for a in theorem.axioms if a not in ALLOWED_AXIOMS and a != SORRY_AXIOM]
        if extra:
            theorem.status = "integrity"
            report.integrity.append(Finding("axiom", "Spec.lean", 0, f"{theorem.name} depends on {', '.join(extra)}"))
        elif theorem.status != "not checked":
            continue  # the restating line already failed; Lean filled the gap with sorry
        elif SORRY_AXIOM in theorem.axioms:
            theorem.status = "unfinished"
            report.unfinished.append(Finding("sorry", "Spec.lean", 0, f"{theorem.name} rests on sorry"))
        elif theorem.status == "not checked":
            theorem.status = "proved"
    for name in NO_AXIOMS.findall(output):
        theorem = by_index.get(name)
        if theorem is not None and theorem.status == "not checked":
            theorem.status = "proved"
    for theorem in report.theorems:
        if theorem.status == "not checked":
            theorem.status = "unfinished"
            report.unfinished.append(Finding("not checked", "Spec.lean", 0, f"no axiom report for {theorem.name}"))
    if result.returncode and not LOCATED.search(output):
        report.errors.append(output.strip()[-2000:] or f"lake env lean exited {result.returncode}")


def compare_spec(spec: Path, report: Report) -> None:
    approved = spec_statements(spec.read_text(encoding="utf-8"))
    if not approved:
        report.integrity.append(Finding("spec", str(spec), 0, "the spec has no ### Theorems block with lean statements"))
        return
    written = {t.name.rsplit(".", 1)[-1]: t for t in report.theorems}
    for name, statement in approved.items():
        theorem = written.get(name)
        if theorem is None:
            report.integrity.append(Finding("spec", "Spec.lean", 0, f"{name} is approved in the spec but missing from Spec.lean"))
        elif normalize(theorem.statement) != normalize(statement):
            report.integrity.append(Finding("spec", "Spec.lean", 0, f"{name} differs from the approved statement"))
    for name in written:
        if name not in approved:
            report.integrity.append(Finding("spec", "Spec.lean", 0, f"{name} is in Spec.lean but not approved in the spec"))


def statement_diff(project: Path, base: str, report: Report) -> None:
    root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=project, capture_output=True,
                               text=True, check=True).stdout.strip())
    prefix = project.resolve().relative_to(root.resolve()).as_posix()
    listed = subprocess.run(["git", "ls-tree", "-r", "--name-only", base, "--", prefix or "."], cwd=root,
                            capture_output=True, text=True, check=False)
    before: dict[str, Theorem] = {}
    for name in listed.stdout.splitlines():
        if name.endswith("/Spec.lean") or name == "Spec.lean":
            shown = subprocess.run(["git", "show", f"{base}:{name}"], cwd=root, capture_output=True, text=True, check=False)
            before.update({t.name: t for t in statements(shown.stdout)})
    after = {t.name: t for t in report.theorems}
    entry = lambda t: {"name": t.name, "reading": t.reading, "statement": t.statement}
    report.statements = {
        "added": [entry(t) for n, t in after.items() if n not in before],
        "changed": [{**entry(t), "before": before[n].statement} for n, t in after.items()
                    if n in before and normalize(before[n].statement) != normalize(t.statement)],
        "removed": [entry(t) for n, t in before.items() if n not in after],
    }


def check(args: argparse.Namespace) -> Report:
    project = Path(args.project)
    report = Report(str(project))
    pin = project / "lean-toolchain"
    if not pin.is_file():
        report.errors.append(f"no {pin}; pin the toolchain so every run checks with the same kernel")
        report.result = "not run"
        return report
    report.toolchain = pin.read_text(encoding="utf-8").strip()
    for path in spec_files(project):
        report.theorems += statements(path.read_text(encoding="utf-8"))
    if not report.theorems:
        report.errors.append(f"no `def NAME : Prop :=` statement in any Spec.lean under {project}")
    scan(project, report)
    if args.spec:
        compare_spec(Path(args.spec), report)
    if args.base:
        statement_diff(project, args.base, report)
    if shutil.which("lake") is None:
        report.errors.append("lake is not on PATH; install elan and the pinned toolchain")
        report.result = "not run"
        return report
    # Build the very modules the gate imports; a package's default targets may cover none of them.
    built = lake(project, "build", *modules(project))
    if built.returncode:
        report.unfinished.append(Finding("build", "lake build", 0, (built.stdout + built.stderr).strip()[-2000:]))
        for theorem in report.theorems:
            theorem.status = "unfinished"
    elif report.theorems:
        check_axioms(project, report)
        if args.recheck and shutil.which("lean4checker") is None:
            report.notes.append("lean4checker is not installed; the kernel replay was skipped")
        elif args.recheck:
            replay = lake(project, "env", "lean4checker", *modules(project))
            if replay.returncode:
                report.integrity.append(Finding("recheck", "lean4checker", 0, (replay.stdout + replay.stderr).strip()[-500:]))
    if report.integrity:
        report.result = "integrity"
    elif report.errors and not report.theorems:
        report.result = "not run"
    elif report.unfinished or report.errors:
        report.result = "unfinished"
    return report


def show(report: Report) -> None:
    for finding in report.integrity:
        print(f"integrity: {finding.file}:{finding.line}: {finding.rule} - {finding.detail}")
    for finding in report.unfinished:
        print(f"unfinished: {finding.file}:{finding.line}: {finding.rule} - {finding.detail}")
    for error in report.errors:
        print(f"error: {error}")
    for note in report.notes:
        print(f"note: {note}")
    for kind in ("added", "changed", "removed"):
        for entry in report.statements.get(kind, []):
            print(f"statement {kind}: {entry['name']} - {entry['reading'] or 'no reading'}")
    proved = sum(1 for t in report.theorems if t.status == "proved")
    print(f"proof-gate: {report.result} - {proved} of {len(report.theorems)} statements proved ({report.toolchain or 'no toolchain'})")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="proof-gate.py")
    sub = parser.add_subparsers(dest="command", required=True)
    co = sub.add_parser("check")
    co.add_argument("--project", default="formal")
    co.add_argument("--spec", help="the spec whose ### Theorems block approved the statements")
    co.add_argument("--base", help="list the statements changed since this commit")
    co.add_argument("--json", help="write the report here")
    co.add_argument("--recheck", action="store_true", help="replay the modules through lean4checker")
    args = parser.parse_args(argv)
    report = check(args)
    show(report)
    if args.json:
        Path(args.json).write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8")
    return {"clean": 0, "integrity": 1, "unfinished": 2, "not run": 3}[report.result]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
