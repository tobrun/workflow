#!/usr/bin/env python3
"""Turn e2e evidence into a PR-ready Evidence section, publish its images, and gate the body.

    pr-evidence.py extract <e2e-report.json> --out <dir> [--scenario ID ...] [--branch pr-evidence]
                           [--url-template URL] [--remote origin]
    pr-evidence.py publish <dir> [--branch pr-evidence] [--remote origin]
    pr-evidence.py check <pr-body.md> [--kind frontend|non-frontend] [--spec spec.md]
    pr-evidence.py summarize <pr-body.md>
    pr-evidence.py acceptance <pr-body.md> --evidence .dev/{plan}/test-evidence.json
    pr-evidence.py flow <pr-body.md> --spec .dev/{plan}/spec.md --base REF

`extract` reads build's JSON e2e report, decodes every
screenshot to `<dir>/{plan}/{scenario}/{step}.png`, and writes
`<dir>/evidence.md`: one "## Evidence" section with the images (frontend) or
before/after state tables (non-frontend) a reviewer can read inline on the PR.
Image links use --url-template, where `{path}` is the file's path under <dir>;
on a GitHub remote the template is derived from --branch when omitted.

`publish` commits everything under <dir> onto the assets branch and pushes it,
using a temporary index and plumbing commands only: the working tree, the
current branch, and the index are never touched. Data URIs do not render on a
PR, so this is how screenshots become visible.

`check` exits non-zero unless the body has a non-empty "## Evidence" section
holding real proof: an image served over http(s), or a labeled before/after
(or merge-base/branch) pair of fenced blocks. Placeholders fail it. It also
requires a "## Quality" table with one row per verification kind of
references/verification.md, each `ran` or `skipped (reason)`; with --spec, a
kind the spec's Verification block marks yes may not be skipped, a "## What the
tests check" paragraph is required (plain prose of at most 120 words, no table and
no scenario ids), and the hidden dev-acceptance marker must match the spec's
scenarios and the test-evidence.json beside it. The body keeps empty DEV-EVIDENCE
markers: CI's ci-evidence.py owns what goes between them. A "## Not verified"
section is required, each of its bullets led by a bold category
("- **Coverage:** ..."), and the grouped verification summary must be what
`summarize` writes. An "## Acceptance criteria" table is refused.

`acceptance` writes the dev-acceptance marker, each scenario id and the runner case
that proves it, from test-evidence.json; CI reads it to check every scenario's test
at the head commit, and no person has to read it.

`flow` copies the spec's ### Flow mermaid diagram into a "## Flow" section when it
still matches the code: every name it draws is in a tracked file at HEAD, and at
least one is in a file the branch changed since --base. Otherwise it writes no
section, removes a stale one, and prints the names it could not find.

`summarize` rewrites the QUALITY-SUMMARY block from the Quality table: the twenty
kinds grouped, each marked ran clean, ran with open calls, or skipped.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
# TODO and TBD count only in capitals, the way placeholders are written, so an app named todo is not one.
PLACEHOLDERS = re.compile(r"\bTODO\b|\bTBD\b|(?i:\{[a-z][a-z0-9-]*\}|<[^>]*placeholder[^>]*>|screenshot here|data:image/)")
INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1")
PROVENANCE_COMMENT = re.compile(r"<!--\s*dev-evidence:.*?-->", re.DOTALL)
IMAGE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
PAIR_LABELS = {
    "before": "before", "after": "after",
    "on merge base": "base", "on the merge base": "base", "merge base": "base",
    "on this branch": "branch", "on the branch": "branch", "this branch": "branch",
    "on known-bad revision": "base", "on corrected revision": "branch",
}
LABEL_LINE = re.compile(r"^\s*\*\*([^*]+?)\*\*", re.IGNORECASE)
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
PROVENANCE = re.compile(r"<!-- dev-evidence:\s*(\{.*?\})\s*-->", re.DOTALL)
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def fail(message: str) -> None:
    print(f"pr-evidence: {message}", file=sys.stderr)
    sys.exit(1)


def slug(text: str, fallback: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned[:60] or fallback


def load_e2e_data(report: Path) -> dict:
    """The e2e report is one JSON document in the shape build's e2e-report.md defines."""
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"{report} is not a readable JSON e2e report: {error}")
    if not isinstance(data, dict):
        fail(f"{report} holds no JSON object")
    return data


def git(*args: str, env: dict | None = None, check: bool = True) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, env=env, check=False)
    if check and result.returncode != 0:
        fail(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def github_slug(remote: str) -> str | None:
    url = git("remote", "get-url", remote, check=False)
    match = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$", url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def url_template(args: argparse.Namespace) -> str | None:
    if args.url_template:
        if "{path}" not in args.url_template:
            fail("--url-template must contain {path}")
        return args.url_template
    repo = github_slug(args.remote)
    if repo:
        return f"https://github.com/{repo}/blob/{args.branch}/{{path}}?raw=true"
    return None


def fenced(value: object, language: str = "json") -> str:
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text.rstrip()}\n{fence}"


def extract(args: argparse.Namespace) -> None:
    data = load_e2e_data(Path(args.report))
    kind = data.get("kind", "non-frontend")
    plan = slug(str(data.get("planName") or data.get("title") or ""), "plan")
    scenarios = data.get("scenarios", []) or []
    summary = data.get("summary") or {}
    if args.scenario:
        known = {str(s.get("id")): s for s in scenarios}
        missing = [wanted for wanted in args.scenario if wanted not in known]
        if missing:
            fail(f"no scenario with id {', '.join(missing)}; the report has: {', '.join(known) or 'none'}")
        scenarios = [known[wanted] for wanted in args.scenario]
        summary = {"passed": sum(1 for s in scenarios if s.get("status") == "pass"), "total": len(scenarios)}
    out = Path(args.out)
    template = url_template(args) if kind == "frontend" else None
    if kind == "frontend" and template is None:
        fail("frontend report but no --url-template and the remote is not GitHub; pass --url-template")

    lines = ["## Evidence", "",
             f"Captured from the e2e run of `{plan}` at {data.get('generatedAt', 'unknown time')}: "
             + f"{summary.get('passed', 0)}/{summary.get('total', len(scenarios))} scenarios passed.", ""]
    provenance = data.get("provenance")
    if provenance:
        lines += [f"<!-- dev-evidence: {json.dumps(provenance, ensure_ascii=False)} -->", "",
                  f"Tested revision: `{provenance.get('revision', 'unknown')}`.", ""]
    written: list[str] = []
    for index, scenario in enumerate(scenarios, 1):
        scenario_id = slug(str(scenario.get("id") or scenario.get("title") or index), f"scenario-{index}")
        lines += [f"### {scenario.get('title', scenario_id)} - {scenario.get('status', 'unknown')}", "",
                  f"Given {scenario.get('given', '?')}, when {scenario.get('when', '?')}, then {scenario.get('then', '?')}.", ""]
        for step_index, shot in enumerate(scenario.get("screenshots", []) or [], 1):
            uri = shot.get("dataUri", "")
            if not isinstance(uri, str) or not uri.startswith("data:image/") or ";" not in uri or "," not in uri.split(";", 1)[1]:
                fail(f"{scenario_id} step {step_index} has no well-formed base64 data URI screenshot (data:image/TYPE;base64,DATA)")
            ext = "." + slug(uri[len("data:image/"):uri.index(";")].replace("jpeg", "jpg"), "png")
            relative = Path(plan) / scenario_id / f"{step_index:02d}-{slug(shot.get('step', ''), 'step')}{ext}"
            target = out / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                target.write_bytes(base64.b64decode(uri.split(",", 1)[1], validate=True))
            except ValueError as error:
                fail(f"{scenario_id} step {step_index} has an undecodable data URI screenshot: {error}")
            written.append(relative.as_posix())
            url = template.replace("{path}", relative.as_posix())
            lines += [f"**{shot.get('step', 'step')}** - {shot.get('caption', '')}", "",
                      f"![{shot.get('caption') or shot.get('step', 'screenshot')}]({url})", ""]
        for state in scenario.get("dataModelState", []) or []:
            lines += [f"**{state.get('step', 'step')}** - {state.get('caption', '')} (`{state.get('entity', '?')}`)", "",
                      "**Before**", "", fenced(state.get("before", "")), "",
                      "**After**", "", fenced(state.get("after", "")), ""]
        output = scenario.get("logsOrOutput")
        if output:
            lines += ["<details><summary>Captured output</summary>", "", fenced(output, ""), "", "</details>", ""]
    out.mkdir(parents=True, exist_ok=True)
    (out / "evidence.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(json.dumps({"kind": kind, "plan": plan, "scenarios": len(scenarios), "images": written,
                      "evidence": str(out / "evidence.md"), "urlTemplate": template}, indent=2))


def publish(args: argparse.Namespace) -> None:
    root = Path(args.dir).resolve()
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
    if not files:
        fail(f"nothing to publish: no image files under {root}")
    ref = f"refs/remotes/{args.remote}/{args.branch}"
    subprocess.run(["git", "fetch", args.remote, args.branch], capture_output=True, text=True, check=False)
    parent = git("rev-parse", "--verify", "--quiet", ref, check=False) or None
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if parent:
            git("read-tree", parent, env=env)
        for path in files:
            blob = git("hash-object", "-w", str(path))
            git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path.relative_to(root).as_posix()}", env=env)
        tree = git("write-tree", env=env)
    message = f"evidence: {len(files)} screenshots captured {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
    commit = git("commit-tree", tree, *(["-p", parent] if parent else []), "-m", message)
    git("push", args.remote, f"{commit}:refs/heads/{args.branch}")
    print(json.dumps({"branch": args.branch, "commit": commit, "files": [p.relative_to(root).as_posix() for p in files]}, indent=2))


# The verification kinds of references/verification.md, in its order; lint-spec.py holds the same list.
KINDS = (
    "unit", "functional", "component", "contract", "property", "fuzz", "formal", "static-analysis", "lint",
    "coverage", "mutation", "regression", "acceptance", "smoke", "benchmark", "performance", "ui-ux",
    "usability", "threat-model", "penetration",
)
QUALITY_ROW = re.compile(r"^\|\s*`?([a-z-]+)`?\s*\|\s*([^|]*?)\s*\|")
PLANNED = re.compile(r"^\s*[-*]\s+([a-z-]+):\s*(yes|no)\b")


GROUPS = (
    ("Tests", ("unit", "functional", "component", "contract", "acceptance", "regression", "smoke")),
    ("Generated inputs and models", ("property", "fuzz", "formal")),
    ("Code analysis", ("static-analysis", "lint", "coverage", "mutation")),
    ("Speed", ("benchmark", "performance")),
    ("Experience", ("ui-ux", "usability")),
    ("Security", ("threat-model", "penetration")),
)
# What a person reads for each kind; the Quality table keeps the slug for tools.
KIND_NAMES = {"static-analysis": "Static analysis", "ui-ux": "UI/UX", "threat-model": "Threat model"}
EVIDENCE_BEGIN, EVIDENCE_END = "<!-- BEGIN DEV-EVIDENCE -->", "<!-- END DEV-EVIDENCE -->"
SUMMARY_BEGIN, SUMMARY_END = "<!-- BEGIN QUALITY-SUMMARY -->", "<!-- END QUALITY-SUMMARY -->"
SUMMARY = re.compile(re.escape(SUMMARY_BEGIN) + r".*?" + re.escape(SUMMARY_END), re.DOTALL)
SCENARIO_TESTS = re.compile(r"^\s*tests:\s*(.*)$", re.IGNORECASE)


def quality_table(body: str) -> dict[str, tuple[str, str]]:
    """Each kind's Result and Surviving cells."""
    found = {}
    for line in (evidence_section(body, "Quality") or "").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0].strip("`") in KINDS:
            found[cells[0].strip("`")] = (cells[1], cells[4] if len(cells) > 4 else "")
    return found


def summary_block(body: str) -> str:
    table = quality_table(body)
    rows = []
    for group, kinds in GROUPS:
        marks = []
        for kind in kinds:
            result, surviving = table.get(kind, ("missing", ""))
            name = KIND_NAMES.get(kind, kind.capitalize())
            if result == "ran":
                marks.append(f"{'⚠️' if surviving not in ('', '0') else '✅'} {name}")
            elif result.startswith("skipped"):
                marks.append(f"⏭️ {name}")
            else:
                marks.append(f"❓ {name}")
        rows.append(f"| {group} | {' · '.join(marks)} |")
    ran = sum(1 for r, _ in table.values() if r == "ran")
    return "\n".join([SUMMARY_BEGIN, f"{ran} of {len(KINDS)} kinds ran; the rest were skipped with the reasons in the table below.", "",
                      "| Area | Kinds |", "| --- | --- |", *rows, "",
                      "<sub>✅ ran clean · ⚠️ ran, open calls remain · ⏭️ skipped with a reason. "
                      "Reported by the agent; the CI block at the end re-measures tests, coverage, and test edits.</sub>",
                      SUMMARY_END])


def spec_scenarios(spec: Path) -> list[str]:
    """Positional scenario ids CS{n}-S{m} of the spec's change plan, as check-tests.py numbers them."""
    ids, current, in_plan = [], None, False
    for line in spec.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_plan = line[3:].strip().lower() == "change plan"
            continue
        if not in_plan:
            continue
        change_set = re.match(r"^(\d+)\.\s+\S", line)
        if change_set:
            current = int(change_set.group(1))
        tests = SCENARIO_TESTS.match(line)
        if tests and current is not None and not tests.group(1).strip().lower().startswith("none"):
            ids += [f"CS{current}-S{i}" for i, s in enumerate(tests.group(1).split(";"), 1) if s.strip()]
    return ids


def check_structure(body: str, spec: Path | None, problems: list[str]) -> None:
    if EVIDENCE_BEGIN not in body or EVIDENCE_END not in body:
        problems.append(f"no {EVIDENCE_BEGIN} / {EVIDENCE_END} markers; CI writes the verified evidence between them")
    else:
        inside = body.split(EVIDENCE_BEGIN, 1)[1].split(EVIDENCE_END, 1)[0]
        if inside.strip():
            problems.append("the DEV-EVIDENCE block is not empty; only CI's ci-evidence.py writes there")
    evidence_block = re.compile(re.escape(EVIDENCE_BEGIN) + r".*?" + re.escape(EVIDENCE_END), re.DOTALL)
    not_verified = SUMMARY.sub("", evidence_block.sub("", evidence_section(body, "Not verified") or ""))
    if not not_verified.strip():
        problems.append("no non-empty '## Not verified' section; name what was not verified, or say nothing was skipped and why")
    found = SUMMARY.search(body)
    if not found:
        problems.append("no QUALITY-SUMMARY block; run pr-evidence.py summarize")
    elif found.group(0) != summary_block(body):
        problems.append("the QUALITY-SUMMARY block does not match the Quality table; run pr-evidence.py summarize, never edit it")
    check_readable(body, spec, problems)


TESTS_SECTION = "What the tests check"
TESTS_WORDS = 120
ACCEPTANCE_MARKER = re.compile(r"<!--\s*dev-acceptance:.*?-->\n?", re.DOTALL)
CATEGORY_BULLET = re.compile(r"^- \*\*[^*:\n]{1,40}:\*\*\s+\S")
MERMAID_WORDS = {"sequencediagram", "flowchart", "graph", "participant", "actor", "as", "note", "loop", "alt", "else", "opt",
                 "par", "and", "rect", "end", "activate", "deactivate", "autonumber", "title", "subgraph", "classdef", "class",
                 "style", "linkstyle", "click", "td", "tb", "lr", "rl", "bt", "over", "left", "right", "of", "critical", "break"}


def check_readable(body: str, spec: Path | None, problems: list[str]) -> None:
    """The parts a reviewer reads: the tests paragraph, the hidden scenario mapping, and categorized gaps."""
    if evidence_section(body, "Acceptance criteria") is not None:
        problems.append("the '## Acceptance criteria' table is dropped; write '## What the tests check' "
                        "and run pr-evidence.py acceptance for the mapping CI reads")
    scenarios = spec_scenarios(spec) if spec is not None else []
    paragraph = evidence_section(body, TESTS_SECTION)
    text = ACCEPTANCE_MARKER.sub("", paragraph or "").strip()
    if paragraph is None or not text:
        if scenarios:
            problems.append(f"no non-empty '## {TESTS_SECTION}' paragraph; say in plain words what the tests validate")
    else:
        if re.search(r"^\s*\|", text, re.MULTILINE):
            problems.append(f"'## {TESTS_SECTION}' holds a table; write it as one short paragraph")
        if re.search(r"\bCS\d+-S\d+\b", text):
            problems.append(f"'## {TESTS_SECTION}' names scenario ids; describe the behavior they check instead")
        words = len(text.split())
        if words > TESTS_WORDS:
            problems.append(f"'## {TESTS_SECTION}' runs {words} words; keep it under {TESTS_WORDS}")
    if scenarios:
        marker = ACCEPTANCE_MARKER.search(body)
        evidence = spec.parent / "test-evidence.json"
        if not marker:
            problems.append("no dev-acceptance marker; run pr-evidence.py acceptance")
        elif evidence.is_file():
            expected = acceptance_marker(evidence)
            if marker.group(0).strip() != expected.strip():
                problems.append(f"the dev-acceptance marker differs from {evidence}; run pr-evidence.py acceptance")
            mapped = {m["id"] for m in json.loads(expected.split("dev-acceptance:", 1)[1].rsplit("-->", 1)[0])}
            for scenario_id in scenarios:
                if scenario_id not in mapped:
                    problems.append(f"the dev-acceptance marker maps no test for {scenario_id}")
    gaps = evidence_section(body, "Not verified") or ""
    for line in gaps.splitlines():
        if line.startswith("- ") and not CATEGORY_BULLET.match(line):
            problems.append(f"a Not verified item needs a bold category first, as '- **Coverage:** ...': {line[:60]!r}")


def acceptance_marker(evidence: Path) -> str:
    manifest = json.loads(evidence.read_text(encoding="utf-8"))
    mapped = [{"id": m["id"], "case": m["case"]} for m in manifest.get("scenarios", [])
              if isinstance(m, dict) and m.get("id") and m.get("case")]
    return f"<!-- dev-acceptance: {json.dumps(mapped, ensure_ascii=False)} -->\n"


def acceptance(args: argparse.Namespace) -> None:
    path = Path(args.body)
    body = path.read_text(encoding="utf-8")
    try:
        marker = acceptance_marker(Path(args.evidence))
    except (OSError, ValueError, AttributeError) as error:
        fail(f"cannot read {args.evidence}: {error}")
    if ACCEPTANCE_MARKER.search(body):
        body = ACCEPTANCE_MARKER.sub(lambda _: marker, body, count=1)
    else:
        body = insert_before(body, ("## Evidence", EVIDENCE_BEGIN), marker + "\n")
    path.write_text(body, encoding="utf-8")
    print(f"pr-evidence: dev-acceptance marker written to {path}")


def insert_before(body: str, anchors: tuple[str, ...], text: str) -> str:
    """Insert text before the first anchor line found, or at the end."""
    for anchor in anchors:
        at = re.search(rf"^{re.escape(anchor)}", body, re.MULTILINE)
        if at:
            return body[: at.start()] + text + body[at.start():]
    return body.rstrip("\n") + "\n\n" + text


def spec_flow(spec: Path) -> str | None:
    """The mermaid source of the spec's ### Flow block, or None."""
    block = re.search(r"^###\s+Flow\s*$(.*?)(?=^#{1,3}\s|\Z)", spec.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)
    fence = re.search(r"```mermaid\s*\n(.*?)```", block.group(1), re.DOTALL) if block else None
    return fence.group(1).rstrip() + "\n" if fence else None


def flow_names(source: str) -> list[str]:
    """The names a diagram draws: participant labels (or ids) and node labels (or ids)."""
    names: dict[str, str] = {}
    for raw in source.splitlines():
        line = raw.strip()
        declared = re.match(r"^(?:participant|actor)\s+([\w.]+)(?:\s+as\s+(.+))?$", line)
        if declared:
            names[declared.group(1)] = (declared.group(2) or declared.group(1)).strip()
            continue
        if re.match(r"^(?:note|title|%%|classDef|class|style|linkStyle|click)\b", line):
            continue
        message = re.match(r"^([\w.]+)\s*-{1,2}(?:>>|>|x|\))\s*([\w.]+)\s*:", line)
        if message:  # a sequence message; its text after the colon is prose, not names
            for node in message.groups():
                names.setdefault(node, node)
            continue
        node_label = re.compile(r"([\w.]+)\s*(?:\[\(|\(\(|\[|\(|\{|>)\s*\"?([^\]\)\}\"|]+)\"?[\]\)\}]*")
        for node, label in node_label.findall(line):
            names[node] = label.strip()
        bare = node_label.sub(lambda m: m.group(1), line)
        for pair in re.findall(r"([\w.]+)\s*(?:-->|---|-\.->|==>)\s*(?:\|[^|]*\|)?\s*([\w.]+)", bare):
            for node in pair:
                names.setdefault(node, node)
    cleaned = []
    for label in names.values():
        label = re.sub(r"<br\s*/?>", " ", label).strip("`'\" ")
        if label and label.lower() not in MERMAID_WORDS and label not in cleaned:
            cleaned.append(label)
    return cleaned


def flow(args: argparse.Namespace) -> None:
    path = Path(args.body)
    body = path.read_text(encoding="utf-8")
    stale = re.compile(r"^## Flow\s*\n.*?(?=^## |" + re.escape(EVIDENCE_BEGIN) + r"|\Z)", re.MULTILINE | re.DOTALL)
    body = stale.sub("", body)
    source = spec_flow(Path(args.spec))
    if source is None:
        path.write_text(body, encoding="utf-8")
        print("pr-evidence: the spec has no ### Flow diagram; no Flow section")
        return
    names = flow_names(source)
    tracked = git("ls-files").splitlines()
    changed = git("diff", "--name-only", f"{args.base}...HEAD").splitlines()

    def found(name: str, paths: list[str]) -> bool:
        """The name is in a path's file name or in its content at HEAD."""
        if any(name.lower() in path.lower() for path in paths):
            return True
        return bool(paths) and subprocess.run(["git", "grep", "-qiF", "-e", name, "HEAD", "--", *paths],
                                              capture_output=True).returncode == 0

    missing = [n for n in names if not found(n, tracked)]
    touched = [n for n in names if found(n, changed)]
    if missing or not touched:
        path.write_text(body, encoding="utf-8")
        why = f"names not in the code at HEAD: {', '.join(missing)}" if missing else "no name it draws is in a file this branch changed"
        print(f"pr-evidence: the spec's Flow diagram no longer matches; no Flow section ({why})")
        return
    head = git("rev-parse", "--short", "HEAD").strip()
    section = (f"## Flow\n\nFrom the spec; each of the {len(names)} names it draws is in the code at `{head}`.\n\n"
               f"```mermaid\n{source}```\n\n")
    path.write_text(insert_before(body, ("## Evidence", "## Not verified", EVIDENCE_BEGIN), section), encoding="utf-8")
    print(f"pr-evidence: Flow section written to {path} ({len(names)} names, {len(touched)} in changed files)")


def summarize(args: argparse.Namespace) -> None:
    path = Path(args.body)
    body = path.read_text(encoding="utf-8")
    block = summary_block(body)
    body = SUMMARY.sub(lambda _: block, body, count=1) if SUMMARY.search(body) else body.replace(
        "<details>", block + "\n\n<details>", 1) if "<details>" in body else body.rstrip("\n") + "\n\n" + block + "\n"
    path.write_text(body, encoding="utf-8")
    print(f"pr-evidence: summary written to {path}")


def planned_kinds(spec: Path) -> set[str]:
    """The kinds a spec's ### Verification block marks yes."""
    planned, inside = set(), False
    for line in spec.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            inside = line.strip().lower() == "### verification"
            continue
        match = PLANNED.match(line) if inside else None
        if match and match.group(2) == "yes":
            planned.add(match.group(1))
    return planned


def check_quality(body: str, spec: Path | None, problems: list[str]) -> None:
    section = evidence_section(body, "Quality")
    if not section:
        problems.append("no '## Quality' section; it holds one row per verification kind")
        return
    results: dict[str, str] = {}
    for line in section.splitlines():
        match = QUALITY_ROW.match(line.strip())
        if match and match.group(1) in KINDS:
            kind, result = match.groups()
            if kind in results:
                problems.append(f"Quality lists {kind} twice")
            results[kind] = result
    for kind in KINDS:
        result = results.get(kind)
        if result is None:
            problems.append(f"Quality has no row for {kind}; every kind is ran or skipped (reason)")
        elif not (result == "ran" or re.fullmatch(r"skipped \(.*\S.*\)", result)):
            problems.append(f"Quality row {kind} reads '{result}'; write ran, or skipped (reason)")
    if spec is not None:
        for kind in sorted(planned_kinds(spec)):
            if results.get(kind, "").startswith("skipped"):
                problems.append(f"the spec plans {kind} (yes in its Verification block), but Quality skips it")


def evidence_section(body: str, name: str = "Evidence") -> str | None:
    """Top-level headings inside captured code are evidence, not delimiters."""
    collected = []
    inside, fence = False, None
    for line in body.splitlines(keepends=True):
        match = FENCE.match(line)
        if match:
            token, suffix = match.groups()
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not suffix.strip():
                fence = None
        elif fence is None and re.match(r"^##\s+", line):
            if inside:
                break
            inside = bool(re.match(rf"^##\s+{name}\s*$", line))
            continue
        if inside:
            collected.append(line)
    return "".join(collected) if inside else None


def labeled_pairs(section: str) -> set[str]:
    """Labels (before/after/base/branch) that head a fenced block within the next two lines."""
    found: set[str] = set()
    lines = section.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        match = LABEL_LINE.match(line)
        if not match:
            continue
        label = PAIR_LABELS.get(match.group(1).strip().lower())
        if not label:
            continue
        opening = next((j for j in range(i, min(i + 2, len(lines))) if FENCE.match(lines[j])), None)
        if opening is None:
            continue
        token = FENCE.match(lines[opening]).group(1)
        for end in range(opening + 1, len(lines)):
            closing = FENCE.match(lines[end])
            if closing and closing.group(1)[0] == token[0] and len(closing.group(1)) >= len(token) and not closing.group(2).strip():
                if any(line.strip() for line in lines[opening + 1:end]):
                    found.add(label)
                i = end + 1
                break
        else:
            break
    return found


def check_provenance(section: str, repo_root: Path, problems: list[str]) -> None:
    marker = PROVENANCE.search(section)
    if marker is None:
        problems.append("missing dev-evidence provenance marker; capture revision, command, and source hashes")
        return
    try:
        data = json.loads(marker.group(1))
        if not isinstance(data, dict) or not isinstance(data.get("revision"), str) or not data["revision"].strip():
            raise ValueError("provenance requires the actual tested revision")
        command = data.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
            raise ValueError("provenance requires the capture command")
        sources = data.get("sources")
        if not isinstance(sources, dict) or not sources:
            raise ValueError("provenance requires tested source hashes")
        for path, digest in sources.items():
            target = (repo_root / path).resolve()
            target.relative_to(repo_root.resolve())
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError(f"evidence is stale for {path}; rerun affected scenarios")
    except (OSError, ValueError, TypeError) as error:
        problems.append(str(error))


def prose(section: str) -> str:
    """The section without fenced blocks or inline code: captured evidence may hold braces, only prose is checked."""
    kept, fence = [], None
    for line in section.splitlines():
        match = FENCE.match(line)
        if match:
            token, suffix = match.groups()
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not suffix.strip():
                fence = None
            continue
        if fence is None:
            kept.append(INLINE_CODE.sub("", line))
    return "\n".join(kept)


def derived_kind(spec: Path | None) -> str | None:
    """A spec that marks ui-ux yes has a UI a screenshot can show; anything else is not derivable."""
    if spec is not None and "ui-ux" in planned_kinds(spec):
        return "frontend"
    return None


def check(args: argparse.Namespace) -> None:
    section = evidence_section(Path(args.body).read_text(encoding="utf-8"))
    problems: list[str] = []
    if section is None or not section.strip():
        problems.append("no non-empty '## Evidence' section")
        section = ""
    placeholder = PLACEHOLDERS.search(prose(PROVENANCE_COMMENT.sub("", section)))  # the marker holds file paths, not prose
    if placeholder:
        problems.append(f"placeholder or inline data URI in Evidence: {placeholder.group(0)!r}")
    kind = args.kind or derived_kind(Path(args.spec) if args.spec else None)
    images = IMAGE.findall(section)
    labels = labeled_pairs(section)
    pair = {"before", "after"} <= labels or {"base", "branch"} <= labels
    if kind == "frontend" and not images:
        problems.append("frontend change but no http(s) image in Evidence")
    if kind == "non-frontend" and not pair:
        problems.append("non-frontend change but no labeled Before/After or merge-base/branch pair in Evidence")
    if not images and not pair:
        problems.append("Evidence holds neither an http(s) image nor a labeled before/after pair")
    if args.require_current:
        check_provenance(section, Path(args.repo_root), problems)
    body = Path(args.body).read_text(encoding="utf-8")
    check_quality(body, Path(args.spec) if args.spec else None, problems)
    check_structure(body, Path(args.spec) if args.spec else None, problems)
    if problems:
        fail("PR body rejected:\n  - " + "\n  - ".join(problems))
    print(f"pr-evidence: ok ({len(images)} images, pairs: {', '.join(sorted(labels)) or 'none'})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("extract")
    ex.add_argument("report")
    ex.add_argument("--out", required=True)
    ex.add_argument("--scenario", action="append", default=[],
                    help="keep only this scenario id, in this order; repeat per scenario (default: all)")
    ex.add_argument("--branch", default="pr-evidence")
    ex.add_argument("--remote", default="origin")
    ex.add_argument("--url-template")
    ex.set_defaults(run=extract)
    pub = sub.add_parser("publish")
    pub.add_argument("dir")
    pub.add_argument("--branch", default="pr-evidence")
    pub.add_argument("--remote", default="origin")
    pub.set_defaults(run=publish)
    ck = sub.add_parser("check")
    ck.add_argument("body")
    ck.add_argument("--kind", choices=["frontend", "non-frontend"],
                    help="gate the evidence by the e2e report's kind; derived as frontend from --spec when it marks ui-ux yes")
    ck.add_argument("--require-current", action="store_true", help="require provenance matching current tested sources")
    ck.add_argument("--repo-root", default=".")
    ck.add_argument("--spec", help="spec.md whose Verification block names the kinds that may not be skipped")
    ck.set_defaults(run=check)
    sm = sub.add_parser("summarize")
    sm.add_argument("body")
    sm.set_defaults(run=summarize)
    ac = sub.add_parser("acceptance")
    ac.add_argument("body")
    ac.add_argument("--evidence", required=True, help="the plan's test-evidence.json")
    ac.set_defaults(run=acceptance)
    fl = sub.add_parser("flow")
    fl.add_argument("body")
    fl.add_argument("--spec", required=True)
    fl.add_argument("--base", required=True, help="the PR's base; a name the diagram draws must be in a file changed since it")
    fl.set_defaults(run=flow)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
