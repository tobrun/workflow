#!/usr/bin/env python3
"""Turn e2e evidence into a PR-ready Evidence section, publish its images, and gate the body.

    pr-evidence.py extract <e2e-report.html> --out <dir> [--branch pr-evidence]
                           [--url-template URL]
    pr-evidence.py publish <dir> [--branch pr-evidence] [--remote origin]
    pr-evidence.py check <pr-body.md> [--kind frontend|non-frontend]

`extract` reads the E2E_DATA block of a rendered e2e report, decodes every
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
(or merge-base/branch) pair of fenced blocks. Placeholders fail it.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DATA_BLOCK = re.compile(r"E2E_DATA_START.*?const\s+E2E_DATA\s*=\s*(\{.*?\})\s*;\s*/\*\s*E2E_DATA_END", re.S)
IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
PLACEHOLDERS = re.compile(r"\bTODO\b|\bTBD\b|\{[a-z][a-z0-9-]*\}|<[^>]*placeholder[^>]*>|screenshot here|data:image/", re.I)
IMAGE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
PAIR_LABELS = {
    "before": "before", "after": "after",
    "on merge base": "base", "on the merge base": "base", "merge base": "base",
    "on this branch": "branch", "on the branch": "branch", "this branch": "branch",
}
LABEL_LINE = re.compile(r"^\s*\*\*([^*]+?)\*\*", re.I)
FENCE_OPEN = re.compile(r"^\s*```")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def fail(message: str) -> None:
    print(f"pr-evidence: {message}", file=sys.stderr)
    sys.exit(1)


def slug(text: str, fallback: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned[:60] or fallback


def load_e2e_data(report: Path) -> dict:
    """The data block is a JS object literal; accept JSON, then a lightly normalized form."""
    match = DATA_BLOCK.search(report.read_text(encoding="utf-8", errors="replace"))
    if not match:
        fail(f"no E2E_DATA block between the markers in {report}")
    literal = match.group(1)
    for candidate in (literal, node_json(literal), js_to_json(literal)):
        if candidate is None:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    fail("E2E_DATA is not parseable as JSON, through node, or as a plain JS object literal")
    return {}


def js_to_json(literal: str) -> str:
    """Rewrite a plain JS object literal as JSON without touching string contents.

    Handles bare keys, single-quoted strings, trailing commas, and comments;
    string bodies (where prose can contain colons and commas) pass through as-is.
    """
    out: list[str] = []
    i, n = 0, len(literal)
    while i < n:
        ch = literal[i]
        if ch in "\"'":
            quote, j, body = ch, i + 1, []
            while j < n and literal[j] != quote:
                if literal[j] == "\\" and j + 1 < n:
                    body.append(literal[j:j + 2])
                    j += 2
                else:
                    body.append(literal[j])
                    j += 1
            text = "".join(body)
            if quote == "'":
                text = text.replace("\\'", "'").replace('"', '\\"')
            out.append(f'"{text}"')
            i = j + 1
        elif literal.startswith("//", i):
            i = literal.find("\n", i) if literal.find("\n", i) != -1 else n
        elif literal.startswith("/*", i):
            end = literal.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif ch == ",":
            j = i + 1
            while j < n and literal[j] in " \t\r\n":
                j += 1
            if j < n and literal[j] in "}]":
                i += 1
            else:
                out.append(ch)
                i += 1
        else:
            ident = IDENT.match(literal, i)
            if ident:
                j = ident.end()
                while j < n and literal[j] in " \t\r\n":
                    j += 1
                if j < n and literal[j] == ":":
                    out.append(f'"{ident.group(0)}"')
                else:
                    out.append(ident.group(0))
                i = ident.end()
            else:
                out.append(ch)
                i += 1
    return "".join(out)


def node_json(literal: str) -> str | None:
    """A JS object literal is exactly what node parses; use it when installed."""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(literal)
    script = f"process.stdout.write(JSON.stringify(eval('(' + require('fs').readFileSync({handle.name!r}, 'utf8') + ')')))"
    try:
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        return result.stdout if result.returncode == 0 else None
    except OSError:
        return None
    finally:
        os.unlink(handle.name)


def git(*args: str, env: dict | None = None, check: bool = True) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, env=env)
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
    return f"```{language}\n{text.rstrip()}\n```"


def extract(args: argparse.Namespace) -> None:
    data = load_e2e_data(Path(args.report))
    kind = data.get("kind", "non-frontend")
    plan = data.get("planName") or slug(data.get("title", ""), "plan")
    scenarios = data.get("scenarios", []) or []
    summary = data.get("summary") or {}
    out = Path(args.out)
    template = url_template(args) if kind == "frontend" else None
    if kind == "frontend" and template is None:
        fail("frontend report but no --url-template and the remote is not GitHub; pass --url-template")

    lines = ["## Evidence", "",
             f"Captured from the e2e run of `{plan}` at {data.get('generatedAt', 'unknown time')}: "
             f"{summary.get('passed', 0)}/{summary.get('total', len(scenarios))} scenarios passed.", ""]
    written: list[str] = []
    for index, scenario in enumerate(scenarios, 1):
        scenario_id = slug(str(scenario.get("id") or scenario.get("title") or index), f"scenario-{index}")
        lines += [f"### {scenario.get('title', scenario_id)} - {scenario.get('status', 'unknown')}", "",
                  f"Given {scenario.get('given', '?')}, when {scenario.get('when', '?')}, then {scenario.get('then', '?')}.", ""]
        for step_index, shot in enumerate(scenario.get("screenshots", []) or [], 1):
            uri = shot.get("dataUri", "")
            if not uri.startswith("data:image/"):
                fail(f"{scenario_id} step {step_index} has no data URI screenshot")
            ext = "." + uri[len("data:image/"):uri.index(";")].replace("jpeg", "jpg")
            relative = Path(plan) / scenario_id / f"{step_index:02d}-{slug(shot.get('step', ''), 'step')}{ext}"
            target = out / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(uri.split(",", 1)[1]))
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
    subprocess.run(["git", "fetch", args.remote, args.branch], capture_output=True, text=True)
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


def evidence_section(body: str) -> str | None:
    match = re.search(r"^##\s+Evidence\s*$(.*?)(?=^##\s|\Z)", body, re.M | re.S)
    return match.group(1) if match else None


def labeled_pairs(section: str) -> set[str]:
    """Labels (before/after/base/branch) that head a fenced block within the next two lines."""
    found: set[str] = set()
    lines = section.splitlines()
    for i, line in enumerate(lines):
        match = LABEL_LINE.match(line)
        if not match:
            continue
        label = PAIR_LABELS.get(match.group(1).strip().lower())
        if label and any(FENCE_OPEN.match(nxt) for nxt in lines[i + 1:i + 3]):
            found.add(label)
    return found


def check(args: argparse.Namespace) -> None:
    section = evidence_section(Path(args.body).read_text(encoding="utf-8"))
    problems: list[str] = []
    if section is None or not section.strip():
        problems.append("no non-empty '## Evidence' section")
        section = ""
    placeholder = PLACEHOLDERS.search(section)
    if placeholder:
        problems.append(f"placeholder or inline data URI in Evidence: {placeholder.group(0)!r}")
    images = IMAGE.findall(section)
    labels = labeled_pairs(section)
    pair = {"before", "after"} <= labels or {"base", "branch"} <= labels
    if args.kind == "frontend" and not images:
        problems.append("frontend change but no http(s) image in Evidence")
    if args.kind == "non-frontend" and not pair:
        problems.append("non-frontend change but no labeled Before/After or merge-base/branch pair in Evidence")
    if not images and not pair:
        problems.append("Evidence holds neither an http(s) image nor a labeled before/after pair")
    if problems:
        fail("PR body rejected:\n  - " + "\n  - ".join(problems))
    print(f"pr-evidence: ok ({len(images)} images, pairs: {', '.join(sorted(labels)) or 'none'})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("extract")
    ex.add_argument("report")
    ex.add_argument("--out", required=True)
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
    ck.add_argument("--kind", choices=["frontend", "non-frontend"])
    ck.set_defaults(run=check)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
