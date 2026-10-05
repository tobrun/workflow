#!/usr/bin/env python3
"""Measure one skill run: time, tokens, agents, tool calls, and git delta.

Usage:
  python3 skill-metrics.py start {skill}
  python3 skill-metrics.py end {skill} [--count key=value ...] [--friction text ...]

`start` snapshots the git state and locates the session transcript under
$CLAUDE_CONFIG_DIR (default ~/.claude), anchoring on the transcript line that
invoked the skill. `end` sums everything from that anchor across the main
transcript and every subagent transcript the run spawned, diffs git against
the snapshot, prints a markdown table, and appends a row to .dev/metrics.jsonl
so later runs can be compared against earlier ones. Every value is measured;
nothing here is narrated from memory. Missing transcript or git degrade to
"n/a" rather than failing the run.

`end` also appends one entry to the cross-repository run journal under
$DEV_MEMORY_DIR (default ~/.dev-workflow/memory/journal/), which the reflect
skill consolidates into cited claims about the skills themselves. The entry
holds the friction signals measured from the transcript (interrupts, denied or
failed tool calls, the user's own turns, and how often each deterministic
checker ran and failed), each with its transcript path and line, plus at most
three `--friction` lines the skill narrates, kept apart as narrated. Set
DEV_MEMORY_DIR=off, or `"memory": {"enabled": false}` in .dev/config.json, to
skip the journal.
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
# The deterministic checkers the skills loop against; a failed run of one is loop
# progress, so it is counted per checker instead of logged as a tool error.
CHECKERS = ("lint-spec.py", "check-tests.py", "pr-evidence.py", "architecture-check.py",
            "check-agents-md.py", "aggregate-findings.py", "claims.py")
# A checker counts only where a command runs it, never where one greps or edits it.
CHECKER_RUN = re.compile(r"(?:^|[;&|(]\s*|\bpython3?\s+(?:-\S+\s+)*)\S*?(" + "|".join(re.escape(c) for c in CHECKERS) + r")(?=\s|$)",
                         re.M)
INTERRUPT = "[Request interrupted by user"
DENIED = ("doesn't want to proceed", "was rejected", "Permission denied by user")
SIGNAL_CAP = 20
EXCERPT = 240
MAX_FRICTION = 3


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


# --- friction signals --------------------------------------------------------

def block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block_text(item.get("text", item.get("content", ""))) if isinstance(item, dict)
                         else str(item) for item in content)
    return ""


def human_text(obj: dict) -> str | None:
    """The text a person typed, or None for tool results and injected turns."""
    if obj.get("isMeta") or obj.get("isSidechain"):
        return None
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        content = block_text([b for b in content if isinstance(b, dict) and b.get("type") == "text"])
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text.startswith("<") or INTERRUPT in text or text.startswith("Base directory for this skill"):
        return None
    return text


def signals(path: Path, start: int, since: float | None, state: dict) -> None:
    """Add one transcript's friction signals and checker runs to `state`."""
    pending: dict[str, str] = {}
    first = True
    for index, obj in iter_lines(path, start):
        stamp = obj.get("timestamp")
        if first and stamp:
            first = False
            if since is not None and parse_iso(stamp) < since:
                return
        message = obj.get("message") or {}
        if obj.get("type") == "assistant":
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    command = str((block.get("input") or {}).get("command", ""))
                    match = CHECKER_RUN.search(command)
                    checker = match.group(1) if match else None
                    if checker:
                        pending[block.get("id", "")] = checker
                        state["checkers"].setdefault(checker, {"runs": 0, "failed": 0})["runs"] += 1
            continue
        if obj.get("type") != "user":
            continue
        where = {"transcript": str(path), "line": index}
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            text = block_text(block.get("content", block.get("text", "")))
            if block.get("type") == "text" and INTERRUPT in text:
                note(state, "interrupt", where, text)
            elif block.get("type") == "tool_result" and block.get("is_error"):
                checker = pending.get(block.get("tool_use_id", ""))
                if checker:
                    entry = state["checkers"][checker]
                    entry["failed"] += 1
                    entry.setdefault("first_failure", where)
                else:
                    note(state, "denied" if any(d in text for d in DENIED) else "tool_error", where, text)
        if isinstance(content, str) and INTERRUPT in content:
            note(state, "interrupt", where, content)
        elif path == state["main"]:
            typed = human_text(obj)
            if typed:
                note(state, "user_turn", where, typed)


def note(state: dict, kind: str, where: dict, text: str) -> None:
    state["counts"][kind] = state["counts"].get(kind, 0) + 1
    if state["counts"][kind] <= SIGNAL_CAP:
        state["signals"].append({"type": kind, **where, "excerpt": " ".join(text.split())[:EXCERPT]})


def collect_signals(transcript: Path | None, anchor: int, since: float) -> dict:
    state: dict = {"main": transcript, "counts": {}, "signals": [], "checkers": {}}
    if transcript and transcript.exists():
        # Skip the invocation line itself: it is the skill's own prompt, not a reaction to it.
        signals(transcript, anchor + 1, None, state)
        for path in subagent_files(transcript):
            signals(path, 0, since, state)
    state.pop("main")
    return state


# --- run journal -------------------------------------------------------------

def memory_dir() -> Path | None:
    value = os.environ.get("DEV_MEMORY_DIR", "~/.dev-workflow/memory")
    if value.strip().lower() in ("off", "0", "false", ""):
        return None
    config = repo_root() / ".dev" / "config.json"
    try:
        if json.loads(config.read_text(encoding="utf-8")).get("memory", {}).get("enabled") is False:
            return None
    except (OSError, ValueError, AttributeError):
        pass
    return Path(value).expanduser()


def write_journal(record: dict, ledger: Path, ledger_line: int, state: dict,
                  friction: list[str], found: dict) -> tuple[Path, str] | None:
    root = memory_dir()
    if root is None:
        return None
    stamp = re.sub(r"[^0-9T]", "", record["started_at"])[:15]
    run_id = f"r-{stamp}-{record['skill']}"
    entry = {
        "id": run_id,
        "skill": record["skill"],
        "repo": repo_root().name,
        "repo_path": str(repo_root()),
        "started_at": record["started_at"],
        "ended_at": record["ended_at"],
        "seconds": record["seconds"],
        "transcript": state.get("transcript"),
        "anchor_line": state.get("anchor_line"),
        "ledger": str(ledger),
        "ledger_line": ledger_line,
        "counts": record["counts"],
        "signal_counts": found["counts"],
        "checkers": found["checkers"],
        "signals": found["signals"],
        "friction": friction,
    }
    journal = root / "journal" / f"{record['ended_at'][:10]}.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return journal, run_id


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
            with Path(path).open(encoding="utf-8", errors="ignore") as handle:
                added += sum(1 for _ in handle)
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


def cmd_end(skill: str, counts: dict[str, str], friction: list[str]) -> int:
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
    found = collect_signals(transcript, state["anchor_line"], since)

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
        "signal_counts": found["counts"],
        "checkers": found["checkers"],
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
    ledger_line = sum(1 for _ in ledger.open(encoding="utf-8")) + 1 if ledger.exists() else 1
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    journaled = write_journal(record, ledger, ledger_line, state, friction, found)

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
    measured = [f"{n} {kind.replace('_', ' ')}" for kind, n in sorted(found["counts"].items())]
    measured += [f"{name} {c['runs']} runs / {c['failed']} failed" for name, c in sorted(found["checkers"].items())]
    rows.append(("friction signals", ", ".join(measured) or "none measured"))
    if previous and main:
        med_tokens = statistics.median([r["tokens_total"] for r in previous if r.get("tokens_total")] or [0])
        med_secs = statistics.median(r["seconds"] for r in previous)
        change = (total(all_tokens) - med_tokens) / med_tokens * 100 if med_tokens else 0
        rows.append((
            f"vs previous {skill} runs",
            (
                f"{len(previous)} on record, median {human(med_tokens)} tokens in {duration(med_secs)}; "
                f"this run {change:+.0f}% tokens"
            ),
        ))

    width = max(len(name) for name, _ in rows)
    print(f"## {skill} run metrics\n")
    print(f"| {'metric'.ljust(width)} | value |")
    print(f"| {'-' * width} | ----- |")
    for name, value in rows:
        print(f"| {name.ljust(width)} | {value} |")
    print(f"\nledger: {ledger}")
    if journaled:
        print(f"journal: {journaled[0]} ({journaled[1]}, {len(friction)} narrated friction lines)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start").add_argument("skill")
    end = sub.add_parser("end")
    end.add_argument("skill")
    end.add_argument("--count", action="append", default=[], metavar="KEY=VALUE",
                     help="skill-specific measured counter to include, repeatable")
    end.add_argument("--friction", action="append", default=[], metavar="TEXT",
                     help=f"where this run fought the skill's own instructions, at most {MAX_FRICTION}")
    args = parser.parse_args()
    if args.command == "start":
        return cmd_start(args.skill)
    counts = {}
    for item in args.count:
        if "=" not in item:
            parser.error(f"--count expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        counts[key.strip()] = value.strip()
    friction = [" ".join(line.split()) for line in args.friction if line.strip()]
    if len(friction) > MAX_FRICTION:
        parser.error(f"--friction takes at most {MAX_FRICTION} lines, got {len(friction)}; keep the sharpest")
    return cmd_end(args.skill, counts, friction)


if __name__ == "__main__":
    sys.exit(main())
