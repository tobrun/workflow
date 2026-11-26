#!/usr/bin/env python3
"""Record how each scope, build, and ship run went, and sync the records to a private repository.

Usage:
  workflow-log.py start --phase scope|build|ship|yolo --plan .dev/{plan-name}
  workflow-log.py end   --phase scope|build|ship|yolo --plan .dev/{plan-name}
  workflow-log.py setup --repo git@github.com:OWNER/REPO.git
  workflow-log.py sync [--no-harvest]
  workflow-log.py report [--out FILE]
  workflow-log.py status

`start` notes when a phase began. `end` reads the plan directory's own files for
the facts - the spec's decisions and scenarios, build's notes and test evidence,
ship's Quality table, open calls, and review verdicts - adds the tokens Claude
Code's session logs recorded for this repository between start and end, and
queues one record. Nothing is taken from what an agent says about its run.

`sync` clones or pulls the configured repository, appends the queue to
runs/{host}/{YYYY-MM}.jsonl (one file per machine, so pushes never conflict),
harvests each recorded pull request's outcome with gh into outcomes/, writes
report.html, commits, and pushes. A failure leaves the queue for the next sync;
recording and syncing never fail the run that called them.

State lives under $DEV_WORKFLOW_HOME (default ~/.local/share/dev-workflow).
DEV_WORKFLOW_LOG=off turns recording off. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import hashlib
import html
import json
import os
import re
import shutil
import socket
import statistics
import subprocess
import sys
import uuid
from pathlib import Path

SCHEMA = "dev.workflow-run/1"
OUTCOME_SCHEMA = "dev.workflow-outcome/1"
PHASES = ("scope", "build", "ship", "yolo")
ESCAPE_DAYS = 30  # a fix or revert on a PR's files within this many days of merge counts as an escaped defect
HERE = Path(__file__).resolve().parent


def home() -> Path:
    return Path(os.environ.get("DEV_WORKFLOW_HOME") or Path.home() / ".local" / "share" / "dev-workflow")


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso(moment: datetime.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str | None) -> datetime.datetime | None:
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def run(*args: str, cwd: Path | None = None, timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(list(args), 1, "", str(error))


# ---------------------------------------------------------------------------
# Context every record carries
# ---------------------------------------------------------------------------


def workflow_version() -> str:
    """The installed skill set's version: the portable SKILL.md's metadata, or the plugin manifest above the source."""
    skill = HERE.parent / "SKILL.md"
    if skill.is_file():
        match = re.search(r'^\s+version:\s*"?([^"\n]+)"?', skill.read_text(encoding="utf-8"), re.MULTILINE)
        if match:
            return match.group(1).strip()
    for parent in HERE.parents:
        manifest = parent / ".claude-plugin" / "plugin.json"
        if manifest.is_file():
            return json.loads(manifest.read_text(encoding="utf-8")).get("version", "unknown")
        if (parent / "dev" / ".claude-plugin" / "plugin.json").is_file():
            return json.loads((parent / "dev" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")).get("version", "unknown")
    return "unknown"


def harness() -> str:
    if os.environ.get("CLAUDECODE"):
        return "claude-code"
    if any(key.startswith("CODEX_") for key in os.environ):
        return "codex"
    if any(key.startswith("PI_") for key in os.environ):
        return "pi"
    return "unknown"


def repo_context(plan: Path) -> dict:
    cwd = plan if plan.is_dir() else Path.cwd()
    root = run("git", "rev-parse", "--show-toplevel", cwd=cwd).stdout.strip()
    remote = run("git", "remote", "get-url", "origin", cwd=cwd).stdout.strip()
    slug = re.sub(r"\.git$", "", re.sub(r"^.*[:/]([^/:]+/[^/]+)$", r"\1", remote)) if remote else Path(root or cwd).name
    return {"repo": slug, "root": root, "branch": run("git", "branch", "--show-current", cwd=cwd).stdout.strip(),
            "head": run("git", "rev-parse", "--short", "HEAD", cwd=cwd).stdout.strip()}


# ---------------------------------------------------------------------------
# Facts read from the plan directory
# ---------------------------------------------------------------------------


def section(text: str, name: str) -> str:
    match = re.search(rf"^##\s+{re.escape(name)}\s*$(.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    return match.group(1) if match else ""


def scope_facts(plan: Path) -> dict:
    spec = plan / "spec.md"
    if not spec.is_file():
        return {}
    text = spec.read_text(encoding="utf-8")
    research = section(text, "Research")
    heads = re.findall(r"^(D-[a-z0-9-]+):\s*(.*)$", research, re.MULTILINE)
    chosen = re.findall(r"^\s+✓\s+(.*)$", research, re.MULTILINE)
    confidences = [int(c) for line in chosen for c in re.findall(r"Confidence:\s*(\d{1,3})%", line)]
    plan_text = section(text, "Change plan")
    scenarios: dict[str, int] = {}
    for tests in re.findall(r"^\s*tests:\s*(.*)$", plan_text, re.MULTILINE | re.IGNORECASE):
        for tag in re.findall(r"\[([a-z0-9]+)\]", tests):
            scenarios[tag] = scenarios.get(tag, 0) + 1
    risk = re.search(r"^Risk:\s*(low|medium|high)\b", text, re.MULTILINE | re.IGNORECASE)
    return {
        "decisions": len(heads),
        "high_impact": sum(1 for _, q in heads if "[impact: high]" in q),
        "open": sum(1 for _, q in heads if "[open]" in q),
        "settled_by_user": sum(1 for line in chosen if re.search(r"\(user\b", line)),
        "auto_applied": len(confidences),
        "confidences": confidences,
        "not_doing": len(re.findall(r"^\s+⊘", research, re.MULTILINE)),
        "change_sets": len(re.findall(r"^\d+\.\s+\S", plan_text, re.MULTILINE)),
        "scenarios": scenarios,
        "kinds_planned": len(re.findall(r"^\s*[-*]\s+[a-z-]+:\s*yes\b", section(text, "Scope"), re.MULTILINE)),
        "risk": risk.group(1).lower() if risk else None,
    }


def build_facts(plan: Path) -> dict:
    facts: dict = {}
    notes = plan / "implementation-notes.md"
    if notes.is_file():
        text = notes.read_text(encoding="utf-8")
        facts["change_sets_built"] = len(re.findall(r"^##\s+Change set\s+\d+", text, re.MULTILINE | re.IGNORECASE))
        facts["deviations"] = len(re.findall(r"^\s*-\s*Deviations from spec:", text, re.MULTILINE | re.IGNORECASE))
        facts["fixups"] = len(re.findall(r"^##\s+Fixup:", text, re.MULTILINE))
    evidence = plan / "test-evidence.json"
    if evidence.is_file():
        try:
            mapped = json.loads(evidence.read_text(encoding="utf-8")).get("scenarios", [])
        except ValueError:
            mapped = []
        facts["scenarios_mapped"] = len(mapped)
        facts["scenarios_deferred"] = sum(1 for m in mapped if isinstance(m, dict) and "receipt" not in m)
    return facts


def count(cell: str) -> int | None:
    match = re.match(r"\s*(\d+)", cell or "")
    return int(match.group(1)) if match else None


def ship_facts(plan: Path) -> dict:
    facts: dict = {}
    body = plan / "pr.md"
    if body.is_file():
        text = body.read_text(encoding="utf-8")
        kinds = {}
        for line in section(text, "Quality").splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 5 and re.match(r"^`?[a-z-]+`?$", cells[0]) and cells[0] != "kind":
                kinds[cells[0].strip("`")] = {"result": "ran" if cells[1].startswith("ran") else "skipped",
                                              "found": count(cells[2]), "fixed": count(cells[3]), "surviving": count(cells[4])}
        facts["kinds"] = kinds
        facts["found"] = sum(k["found"] or 0 for k in kinds.values())
        facts["surviving"] = sum(k["surviving"] or 0 for k in kinds.values())
        calls = section(text, "Open calls")
        facts["open_calls"] = 0 if re.match(r"\s*none\b", calls, re.IGNORECASE) else len(re.findall(r"^\s*(?:[-*]|\d+\.)\s+\S", calls, re.MULTILINE))
    reviews = sorted(plan.glob("review_*.md"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))
    verdicts = []
    for review in reviews:
        match = re.search(r"Verdict:\s*\**\s*([A-Z]+)", review.read_text(encoding="utf-8"))
        verdicts.append(match.group(1) if match else "UNKNOWN")
    if verdicts:
        facts["reviews"] = len(verdicts)
        facts["first_verdict"], facts["last_verdict"] = verdicts[0], verdicts[-1]
    pr = run("gh", "pr", "view", "--json", "number,url", cwd=plan, timeout=20)
    if pr.returncode == 0:
        try:
            facts["pr"] = json.loads(pr.stdout).get("number")
        except ValueError:
            pass
    return facts


FACTS = {"scope": scope_facts, "build": build_facts, "ship": ship_facts,
         "yolo": lambda plan: {**scope_facts(plan), **build_facts(plan), **ship_facts(plan)}}


# ---------------------------------------------------------------------------
# Tokens from Claude Code's own session logs
# ---------------------------------------------------------------------------


def claude_usage(root: str, started: datetime.datetime, ended: datetime.datetime) -> dict | None:
    """Tokens of every assistant turn, subagents included, run in this repository between start and end."""
    logs = os.environ.get("DEV_WORKFLOW_CLAUDE_LOGS") or str(Path.home() / ".claude" / "projects")
    totals = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "turns": 0}
    models: dict[str, int] = {}
    seen: set[str] = set()
    for name in glob.glob(os.path.join(logs, "*", "**", "*.jsonl"), recursive=True):
        if os.path.getmtime(name) < started.timestamp():
            continue
        with open(name, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                message = entry.get("message") or {}
                usage = message.get("usage") if isinstance(message, dict) else None
                moment = parse_time(entry.get("timestamp"))
                if not usage or moment is None or not (started <= moment <= ended):
                    continue
                if root and not str(entry.get("cwd", "")).startswith(root):
                    continue
                key = message.get("id") or entry.get("uuid") or line
                if key in seen:  # a streamed message is logged once per content block
                    continue
                seen.add(key)
                totals["input"] += usage.get("input_tokens") or 0
                totals["cache_write"] += usage.get("cache_creation_input_tokens") or 0
                totals["cache_read"] += usage.get("cache_read_input_tokens") or 0
                totals["output"] += usage.get("output_tokens") or 0
                totals["turns"] += 1
                model = message.get("model") or "unknown"
                models[model] = models.get(model, 0) + 1
    return {**totals, "models": models} if totals["turns"] else None


# ---------------------------------------------------------------------------
# start / end
# ---------------------------------------------------------------------------


def run_key(context: dict, plan: Path, phase: str) -> str:
    return hashlib.sha256(f"{context['repo']}|{plan.resolve()}|{phase}".encode()).hexdigest()[:16]


def start(args: argparse.Namespace) -> int:
    plan = Path(args.plan)
    context = repo_context(plan)
    opened = home() / "open"
    opened.mkdir(parents=True, exist_ok=True)
    (opened / f"{run_key(context, plan, args.phase)}.json").write_text(json.dumps({"started": iso(now())}), encoding="utf-8")
    print(f"workflow-log: {args.phase} started")
    return 0


def end(args: argparse.Namespace) -> int:
    plan = Path(args.plan)
    context = repo_context(plan)
    ended = now()
    marker = home() / "open" / f"{run_key(context, plan, args.phase)}.json"
    started = None
    if marker.is_file():
        started = parse_time(json.loads(marker.read_text(encoding="utf-8")).get("started"))
        marker.unlink()
    record = {
        "schema": SCHEMA, "id": str(uuid.uuid4()), "phase": args.phase,
        "started": iso(started) if started else None, "ended": iso(ended),
        "duration_s": round((ended - started).total_seconds()) if started else None,
        "host": socket.gethostname(), "harness": harness(), "workflow_version": workflow_version(),
        "repo": context["repo"], "branch": context["branch"], "head": context["head"], "plan": plan.name,
        "facts": FACTS[args.phase](plan),
        "usage": claude_usage(context["root"], started, ended) if started and harness() == "claude-code" else None,
    }
    queue = home() / "pending"
    queue.mkdir(parents=True, exist_ok=True)
    with open(queue / f"{ended:%Y-%m}.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    took = f" in {record['duration_s'] // 60}m" if record["duration_s"] is not None else " (no start recorded)"
    print(f"workflow-log: {args.phase} recorded{took}; `workflow-log.py sync` sends it")
    return 0


# ---------------------------------------------------------------------------
# setup / sync / harvest
# ---------------------------------------------------------------------------


def config() -> dict:
    path = home() / "config.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def setup(args: argparse.Namespace) -> int:
    home().mkdir(parents=True, exist_ok=True)
    (home() / "config.json").write_text(json.dumps({"repo": args.repo}, indent=2) + "\n", encoding="utf-8")
    print(f"workflow-log: records sync to {args.repo}")
    return 0


def checkout(url: str) -> Path | None:
    """A local clone of the records repository, current with its remote; an empty remote gets a first branch."""
    path = home() / "repo"
    if not (path / ".git").is_dir():
        shutil.rmtree(path, ignore_errors=True)
        if run("git", "clone", "-q", url, str(path), timeout=120).returncode:
            return None
    if run("git", "rev-parse", "--verify", "-q", "HEAD", cwd=path).returncode:
        run("git", "checkout", "-q", "-b", "main", cwd=path)
        return path
    if run("git", "pull", "-q", "--rebase", cwd=path, timeout=120).returncode:
        return None
    return path


def all_runs(repo: Path) -> list[dict]:
    runs, seen = [], set()
    for name in sorted(glob.glob(str(repo / "runs" / "**" / "*.jsonl"), recursive=True)):
        for line in Path(name).read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("id") not in seen:  # a sync interrupted after its push may resend a record
                seen.add(record.get("id"))
                runs.append(record)
    return runs


def harvest(repo: Path, runs: list[dict]) -> int:
    """Each recorded pull request's outcome, re-read until it has merged or closed and its escape window has passed."""
    done = 0
    for slug, number in sorted({(r["repo"], r["facts"].get("pr")) for r in runs if r.get("facts", {}).get("pr")}):
        target = repo / "outcomes" / slug.replace("/", "__") / f"{number}.json"
        if target.is_file() and json.loads(target.read_text(encoding="utf-8")).get("final"):
            continue
        view = run("gh", "pr", "view", str(number), "--repo", slug, "--json",
                   "state,createdAt,mergedAt,closedAt,body,commits,reviews,files,baseRefName,mergeCommit", timeout=60)
        if view.returncode:
            continue
        pr = json.loads(view.stdout)
        created, merged = parse_time(pr.get("createdAt")), parse_time(pr.get("mergedAt"))
        verdict = re.search(r"###\s+(✅|⚠️|❌)", pr.get("body") or "")
        outcome = {
            "schema": OUTCOME_SCHEMA, "repo": slug, "pr": number, "state": pr.get("state"),
            "created": pr.get("createdAt"), "merged": pr.get("mergedAt"),
            "hours_to_merge": round((merged - created).total_seconds() / 3600, 1) if merged and created else None,
            "commits": len(pr.get("commits") or []),
            "changes_requested": sum(1 for r in pr.get("reviews") or [] if r.get("state") == "CHANGES_REQUESTED"),
            "ci_verdict": {"✅": "verified", "⚠️": "attention", "❌": "failed"}.get(verdict.group(1)) if verdict else None,
            "escaped": None, "final": False, "harvested": iso(now()),
        }
        if merged:
            outcome["escaped"] = escaped_fixes(slug, pr, merged)
            outcome["final"] = now() - merged > datetime.timedelta(days=ESCAPE_DAYS)
        elif pr.get("state") == "CLOSED":
            outcome["final"] = True
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")
        done += 1
    return done


def escaped_fixes(slug: str, pr: dict, merged: datetime.datetime) -> int:
    """Fix or revert commits on the base branch touching the PR's files within the escape window after merge."""
    until = merged + datetime.timedelta(days=ESCAPE_DAYS)
    own = (pr.get("mergeCommit") or {}).get("oid")
    shas: set[str] = set()
    for change in (pr.get("files") or [])[:15]:
        listed = run("gh", "api", f"repos/{slug}/commits", "-X", "GET", "-f", f"sha={pr.get('baseRefName') or 'main'}",
                     "-f", f"path={change['path']}", "-f", f"since={iso(merged)}", "-f", f"until={iso(until)}",
                     "--jq", '.[] | [.sha, .commit.message] | @tsv', timeout=60)
        for line in listed.stdout.splitlines():
            sha, _, message = line.partition("\t")
            if sha == own:  # a squash merge titled "fix: ..." is the PR itself, not a fix of it
                continue
            if re.match(r"(fix|revert)\b", message, re.IGNORECASE) or message.lower().startswith('revert "'):
                shas.add(sha)
    return len(shas)


def sync(args: argparse.Namespace) -> int:
    url = config().get("repo")
    if not url:
        print("workflow-log: no records repository; run `workflow-log.py setup --repo URL`")
        return 0
    repo = checkout(url)
    if repo is None:
        print("workflow-log: could not reach the records repository; the queue waits for the next sync")
        return 0
    queue = sorted((home() / "pending").glob("*.jsonl"))
    host = re.sub(r"[^\w.-]", "-", socket.gethostname())
    sent = 0
    for pending in queue:
        lines = pending.read_text(encoding="utf-8")
        target = repo / "runs" / host / pending.name
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(lines)
        sent += len(lines.splitlines())
    runs = all_runs(repo)
    harvested = 0 if args.no_harvest else harvest(repo, runs)
    (repo / "report.html").write_text(report_html(runs, outcomes(repo)), encoding="utf-8")
    run("git", "add", "-A", cwd=repo)
    if run("git", "diff", "--cached", "--quiet", cwd=repo).returncode:
        run("git", "-c", "user.name=workflow-log", "-c", "user.email=workflow-log@localhost", "commit", "-qm",
            f"runs: {sent} from {host}, {harvested} outcomes", cwd=repo)
        pushed = run("git", "push", "-q", "-u", "origin", "HEAD", cwd=repo, timeout=120)
        if pushed.returncode:
            shutil.rmtree(repo, ignore_errors=True)  # the queue still holds every record; the next sync clones afresh
            print(f"workflow-log: push failed ({pushed.stderr.strip()[:200]}); the queue waits for the next sync")
            return 0
    for pending in queue:
        pending.unlink()
    print(f"workflow-log: synced {sent} record(s) and {harvested} outcome(s) to {url}")
    return 0


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def outcomes(repo: Path) -> list[dict]:
    return [json.loads(Path(p).read_text(encoding="utf-8")) for p in sorted(glob.glob(str(repo / "outcomes" / "*" / "*.json")))]


def version_key(version: str) -> tuple:
    return tuple(int(p) if p.isdigit() else -1 for p in re.split(r"[.-]", version))


def median(values: list) -> str:
    values = [v for v in values if v is not None]
    if not values:
        return "-"
    middle = statistics.median(values)
    return f"{middle:,.0f}" if abs(middle) >= 10 else f"{middle:.1f}"


def share(hits: int, total: int) -> str:
    return f"{100 * hits / total:.0f}%" if total else "-"


def metrics(runs: list[dict], results: list[dict]) -> list[tuple[str, str, callable]]:
    """Each row of the scorecard: its group, its name, and how one cohort of runs computes it."""
    by_pr = {(o["repo"], o["pr"]): o for o in results}

    def of(phase: str, cohort: list[dict]) -> list[dict]:
        return [r for r in cohort if r["phase"] in (phase, "yolo")]

    def prs(cohort: list[dict]) -> list[dict]:
        return [by_pr[key] for key in {(r["repo"], r["facts"].get("pr")) for r in of("ship", cohort)} if key in by_pr]

    def tokens(r: dict) -> int | None:
        usage = r.get("usage")
        return sum(usage[k] for k in ("input", "cache_write", "cache_read", "output")) if usage else None

    def per_change_set(r: dict) -> float | None:
        sets = r["facts"].get("change_sets_built") or r["facts"].get("change_sets")
        return r["duration_s"] / 60 / sets if r.get("duration_s") and sets else None

    rows = [
        ("Quality", "Escaped defects per merged PR (matured)", lambda c: median([o["escaped"] for o in prs(c) if o.get("final") and o.get("merged")])),
        ("Quality", "CI verified at the last harvest", lambda c: share(sum(1 for o in prs(c) if o.get("ci_verdict") == "verified"), len(prs(c)))),
        ("Quality", "Review PASS on the first panel", lambda c: share(sum(1 for r in of("ship", c) if r["facts"].get("first_verdict") == "PASS"),
                                                                      sum(1 for r in of("ship", c) if r["facts"].get("first_verdict")))),
        ("Quality", "Verify findings surviving per ship", lambda c: median([r["facts"].get("surviving") for r in of("ship", c)])),
        ("Human effort", "PRs with changes requested", lambda c: share(sum(1 for o in prs(c) if o.get("changes_requested")), len(prs(c)))),
        ("Human effort", "Hours from PR to merge", lambda c: median([o.get("hours_to_merge") for o in prs(c)])),
        ("Human effort", "Decisions asked of you per spec", lambda c: median([r["facts"].get("settled_by_user") for r in of("scope", c)])),
        ("Calibration", "Median confidence of applied decisions", lambda c: median([x for r in of("scope", c) for x in r["facts"].get("confidences", [])])),
        ("Build", "Deviations per change set", lambda c: median([r["facts"]["deviations"] / r["facts"]["change_sets_built"] for r in of("build", c)
                                                                if r["facts"].get("change_sets_built")])),
        ("Build", "Fixups per build", lambda c: median([r["facts"].get("fixups") for r in of("build", c)])),
        ("Cost", "Minutes per change set (build)", lambda c: median([per_change_set(r) for r in of("build", c)])),
        ("Cost", "Minutes per ship", lambda c: median([r["duration_s"] / 60 for r in of("ship", c) if r.get("duration_s")])),
        ("Cost", "Tokens per scope", lambda c: median([tokens(r) for r in of("scope", c)])),
        ("Cost", "Tokens per build", lambda c: median([tokens(r) for r in of("build", c)])),
        ("Cost", "Tokens per ship", lambda c: median([tokens(r) for r in of("ship", c)])),
    ]
    return rows


def report_html(runs: list[dict], results: list[dict]) -> str:
    versions = sorted({r.get("workflow_version", "unknown") for r in runs}, key=version_key)[-6:]
    cohorts = {v: [r for r in runs if r.get("workflow_version") == v] for v in versions}
    heads = "".join(f"<th>{html.escape(v)}</th>" for v in versions)
    counts = "".join("<td>" + " · ".join(f"{sum(1 for r in cohorts[v] if r['phase'] == p)} {p}" for p in PHASES[:3]) + "</td>" for v in versions)
    rows, group = [], None
    for name_group, name, compute in metrics(runs, results):
        if name_group != group:
            rows.append(f'<tr class="group"><th colspan="{len(versions) + 1}">{html.escape(name_group)}</th></tr>')
            group = name_group
        rows.append(f"<tr><th scope=\"row\">{html.escape(name)}</th>" + "".join(f"<td>{compute(cohorts[v])}</td>" for v in versions) + "</tr>")
    generated = iso(now())
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Workflow scorecard</title>
<style>
body {{ margin: 0; font: 14px/1.5 system-ui, sans-serif; background: #f9f9f7; color: #0b0b0b; }}
main {{ max-width: 1000px; margin: 0 auto; padding: 28px 24px; }}
@media (prefers-color-scheme: dark) {{ body {{ background: #0d0d0d; color: #fff; }} td, th {{ border-color: #2c2c2a !important; }} }}
table {{ width: 100%; border-collapse: collapse; }} th, td {{ text-align: left; padding: 7px 10px; border-bottom: 1px solid #e1e0d9; }}
td {{ font-variant-numeric: tabular-nums; }} tr.group th {{ text-transform: uppercase; font-size: 11.5px; letter-spacing: .06em; color: #898781; padding-top: 18px; }}
th[scope=row] {{ font-weight: 500; }} .muted {{ color: #898781; }}
</style></head><body><main>
<h1>Workflow scorecard</h1>
<p class="muted">Medians per workflow version, the newest {len(versions)} shown, from {len(runs)} recorded runs and {len(results)} pull requests.
A cohort with few runs is noise: read the counts row first. Escaped defects count only pull requests merged more than {ESCAPE_DAYS} days ago.
Generated {generated}.</p>
<table><thead><tr><th>Metric</th>{heads}</tr></thead><tbody>
<tr><th scope="row" class="muted">Runs</th>{counts}</tr>
{"".join(rows)}
</tbody></table></main></body></html>
"""


def report(args: argparse.Namespace) -> int:
    url = config().get("repo")
    repo = checkout(url) if url else None
    runs = all_runs(repo) if repo else []
    for pending in sorted((home() / "pending").glob("*.jsonl")):
        runs += [json.loads(line) for line in pending.read_text(encoding="utf-8").splitlines() if line.strip()]
    out = Path(args.out)
    out.write_text(report_html(runs, outcomes(repo) if repo else []), encoding="utf-8")
    print(f"workflow-log: scorecard of {len(runs)} runs written to {out}")
    return 0


def status(args: argparse.Namespace) -> int:
    queued = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in (home() / "pending").glob("*.jsonl"))
    print(f"workflow-log: {queued} record(s) queued; repository: {config().get('repo') or 'not set'}; "
          f"recording {'off' if os.environ.get('DEV_WORKFLOW_LOG') == 'off' else 'on'}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="workflow-log.py")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "end"):
        p = sub.add_parser(name)
        p.add_argument("--phase", choices=PHASES, required=True)
        p.add_argument("--plan", required=True)
    sub.add_parser("setup").add_argument("--repo", required=True)
    sub.add_parser("sync").add_argument("--no-harvest", action="store_true")
    sub.add_parser("report").add_argument("--out", default="workflow-report.html")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.command in ("start", "end") and os.environ.get("DEV_WORKFLOW_LOG") == "off":
        return 0
    try:
        return {"start": start, "end": end, "setup": setup, "sync": sync, "report": report, "status": status}[args.command](args)
    except Exception as error:  # recording must never fail the run that called it
        print(f"workflow-log: {args.command} skipped ({type(error).__name__}: {error})")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
