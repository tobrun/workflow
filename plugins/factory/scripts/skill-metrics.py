#!/usr/bin/env python3
"""Measure one skill run: time, tokens, agents, tool calls, and git delta.

Usage:
  python3 skill-metrics.py start {skill}
  python3 skill-metrics.py end {skill} [--count key=value ...]

`start` snapshots the git state and locates the session transcript under
$CLAUDE_CONFIG_DIR (default ~/.claude), anchoring on the transcript line that
invoked the skill. `end` sums everything from that anchor across the main
transcript and every subagent transcript the run spawned, diffs git against
the snapshot, prints a markdown table, and appends a row to .dev/metrics.jsonl
so later runs can be compared against earlier ones. Every value is measured;
nothing here is narrated from memory. Missing transcript or git degrade to
"n/a" rather than failing the run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

TEST_FILE = re.compile(r"(^|/)(tests?|specs?|__tests__)(/|$)|[._-](test|spec)s?\.[A-Za-z]+$|^test_.*\.py$")
TOKEN_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(value: str) -> float:
    return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def run_git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def repo_root() -> Path:
    top = run_git("rev-parse", "--show-toplevel")
    return Path(top.strip()) if top else Path.cwd()


def state_dir() -> Path:
    path = Path("/tmp") / repo_root().name / "metrics"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- transcript -------------------------------------------------------------

def find_transcript() -> Path | None:
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path.cwd()))
    project = config / "projects" / slug
    if not project.is_dir():
        return None
    files = sorted(project.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def iter_lines(path: Path, start: int = 0):
    with path.open(encoding="utf-8") as handle:
        for index, raw in enumerate(handle):
            if index < start:
                continue
            try:
                yield index, json.loads(raw)
            except json.JSONDecodeError:
                continue


def find_anchor(path: Path, skill: str) -> tuple[int, str | None]:
    """Line index and timestamp of the last invocation of this skill."""
    marker = re.compile(rf"<command-name>/(?:[\w-]+:)?{re.escape(skill)}</command-name>")
    anchor, stamp, count = 0, None, 0
    for index, obj in iter_lines(path):
        count = index + 1
        if obj.get("type") != "user":
            continue
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, str) and marker.search(content):
            anchor, stamp = index, obj.get("timestamp")
    if stamp is None:
        anchor = count
    return anchor, stamp


def aggregate(path: Path, start: int, since: float | None) -> dict:
    """Token, turn and tool totals for one transcript from a line index."""
    usage: dict[str, dict] = {}
    tools: Counter = Counter()
    models: Counter = Counter()
    first_stamp = None
    for _, obj in iter_lines(path, start):
        stamp = obj.get("timestamp")
        if first_stamp is None and stamp:
            first_stamp = stamp
        if obj.get("type") != "assistant":
            continue
        message = obj.get("message") or {}
        message_id = message.get("id") or obj.get("uuid")
        if message.get("usage"):
            usage[message_id] = message["usage"]
            if message.get("model"):
                models[message["model"]] = 1
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tools[block.get("name", "?")] += 1
    if since is not None and first_stamp and parse_iso(first_stamp) < since:
        return {}
    tokens = {key: sum(int(u.get(key) or 0) for u in usage.values()) for key in TOKEN_KEYS}
    return {"turns": len(usage), "tokens": tokens, "tools": dict(tools), "models": sorted(models)}


def subagent_files(transcript: Path) -> list[Path]:
    folder = transcript.with_suffix("") / "subagents"
    return sorted(folder.glob("*.jsonl")) if folder.is_dir() else []


# --- git ---------------------------------------------------------------------

def numstat(ref: str | None) -> tuple[int, int, set[str]]:
    args = ["diff", "--numstat"] + ([ref] if ref else [])
    output = run_git(*args) or ""
    added = removed = 0
    files: set[str] = set()
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added += int(parts[0]) if parts[0].isdigit() else 0
        removed += int(parts[1]) if parts[1].isdigit() else 0
        files.add(parts[2])
    untracked = (run_git("ls-files", "--others", "--exclude-standard") or "").split()
    for path in untracked:
        files.add(path)
        try:
            added += sum(1 for _ in Path(path).open(encoding="utf-8", errors="ignore"))
        except OSError:
            pass
    return added, removed, files


def git_snapshot() -> dict:
    head = run_git("rev-parse", "HEAD")
    added, removed, files = numstat("HEAD" if head else None)
    return {"head": head.strip() if head else None, "added": added, "removed": removed, "files": sorted(files)}


def git_delta(start: dict) -> dict | None:
    head = start.get("head")
    if not head:
        return None
    added, removed, files = numstat(head)
    commits = run_git("rev-list", "--count", f"{head}..HEAD")
    return {
        "commits": int(commits.strip()) if commits and commits.strip().isdigit() else 0,
        "files": len(files),
        "test_files": sum(1 for f in files if TEST_FILE.search(f)),
        "added": max(0, added - start["added"]),
        "removed": max(0, removed - start["removed"]),
        "baseline_dirty_files": len(start["files"]),
    }


# --- commands ----------------------------------------------------------------

def cmd_start(skill: str) -> int:
    transcript = find_transcript()
    anchor, stamp = find_anchor(transcript, skill) if transcript else (0, None)
    state = {
        "skill": skill,
        "started_at": stamp or now_iso(),
        "anchored": stamp is not None,
        "transcript": str(transcript) if transcript else None,
        "anchor_line": anchor,
        "git": git_snapshot(),
    }
    (state_dir() / f"{skill}.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    where = f"transcript line {anchor}" if stamp else "now (no invocation marker found)"
    print(f"metrics: {skill} run started, anchored at {where}")
    return 0


def fmt_tokens(tokens: dict) -> str:
    return "in {} / cache read {} / cache write {} / out {}".format(
        *(human(tokens[key]) for key in TOKEN_KEYS)
    )


def human(value: float) -> str:
    for unit, size in (("M", 1_000_000), ("k", 1_000)):
        if value >= size:
            return f"{value / size:.1f}{unit}"
    return str(int(value))


def duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {secs:02d}s"


def total(tokens: dict) -> int:
    return sum(tokens.get(key, 0) for key in TOKEN_KEYS)


def cmd_end(skill: str, counts: dict[str, str]) -> int:
    state_file = state_dir() / f"{skill}.json"
    if not state_file.exists():
        print(f"metrics: no start snapshot for {skill}; run `start {skill}` at invocation", file=sys.stderr)
        return 1
    state = json.loads(state_file.read_text(encoding="utf-8"))
    since = parse_iso(state["started_at"])
    elapsed = time.time() - since

    main: dict = {}
    agents: list[dict] = []
    transcript = Path(state["transcript"]) if state.get("transcript") else None
    if transcript and transcript.exists():
        main = aggregate(transcript, state["anchor_line"], None)
        agents = [a for a in (aggregate(p, 0, since) for p in subagent_files(transcript)) if a]
    agent_tokens = {key: sum(a["tokens"][key] for a in agents) for key in TOKEN_KEYS}
    all_tokens = {key: main.get("tokens", {}).get(key, 0) + agent_tokens[key] for key in TOKEN_KEYS}
    tools = Counter(main.get("tools", {}))
    for agent in agents:
        tools.update(agent["tools"])
    delta = git_delta(state["git"])

    record = {
        "skill": skill,
        "started_at": state["started_at"],
        "ended_at": now_iso(),
        "seconds": int(elapsed),
        "anchored": state["anchored"],
        "turns": main.get("turns", 0),
        "agents": len(agents),
        "tools": dict(tools),
        "tokens_main": main.get("tokens", {}),
        "tokens_agents": agent_tokens,
        "tokens_total": total(all_tokens),
        "git": delta,
        "counts": counts,
    }
    ledger = repo_root() / ".dev" / "metrics.jsonl"
    previous = []
    if ledger.exists():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("skill") == skill:
                previous.append(row)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")

    top_tools = ", ".join(f"{name} {n}" for name, n in tools.most_common(4)) or "none"
    rows = [
        ("duration", duration(elapsed) + ("" if state["anchored"] else " (from start call, invocation marker not found)")),
        ("orchestrator turns / tool calls", f"{main.get('turns', 0)} / {sum(tools.values())} ({top_tools})"),
        ("agents dispatched", str(len(agents))),
        ("tokens orchestrator", fmt_tokens(main["tokens"]) if main else "n/a (transcript not found)"),
        ("tokens agents", fmt_tokens(agent_tokens) if agents else "0"),
        ("tokens total", human(total(all_tokens)) if main else "n/a"),
    ]
    if delta:
        rows.append((
            "git since start",
            f"{delta['commits']} commits, {delta['files']} files (+{delta['added']}/-{delta['removed']}), "
            f"{delta['test_files']} test files"
            + (f"; {delta['baseline_dirty_files']} files were already dirty" if delta["baseline_dirty_files"] else ""),
        ))
    else:
        rows.append(("git since start", "n/a (not a git repo)"))
    for key, value in counts.items():
        rows.append((key.replace("_", " "), value))
    if previous and main:
        med_tokens = statistics.median(r["tokens_total"] for r in previous if r.get("tokens_total"))
        med_secs = statistics.median(r["seconds"] for r in previous)
        change = (total(all_tokens) - med_tokens) / med_tokens * 100 if med_tokens else 0
        rows.append((
            f"vs previous {skill} runs",
            f"{len(previous)} on record, median {human(med_tokens)} tokens in {duration(med_secs)}; "
            f"this run {change:+.0f}% tokens",
        ))

    width = max(len(name) for name, _ in rows)
    print(f"## {skill} run metrics\n")
    print(f"| {'metric'.ljust(width)} | value |")
    print(f"| {'-' * width} | ----- |")
    for name, value in rows:
        print(f"| {name.ljust(width)} | {value} |")
    print(f"\nledger: {ledger}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start").add_argument("skill")
    end = sub.add_parser("end")
    end.add_argument("skill")
    end.add_argument("--count", action="append", default=[], metavar="KEY=VALUE",
                     help="skill-specific measured counter to include, repeatable")
    args = parser.parse_args()
    if args.command == "start":
        return cmd_start(args.skill)
    counts = {}
    for item in args.count:
        if "=" not in item:
            parser.error(f"--count expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        counts[key.strip()] = value.strip()
    return cmd_end(args.skill, counts)


if __name__ == "__main__":
    sys.exit(main())
