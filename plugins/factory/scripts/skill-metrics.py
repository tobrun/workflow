#!/usr/bin/env python3
"""Measure one skill run: time, tokens, agents, tool calls, and git delta.

Usage:
  python3 skill-metrics.py start {skill}
  python3 skill-metrics.py end {skill} [--count key=value ...] [--friction text ...]

`start` snapshots the git state and finds the session that is running the
skill, in whichever harness hosts it: Claude Code, Codex, opencode, or Pi. Each
harness tells its shell tool which session it is (CLAUDE_CODE_SESSION_ID,
CODEX_THREAD_ID, OPENCODE_SESSION_ID, PI_SESSION_FILE), and every harness saves
a tool call before running it, so the run anchors on the `start` call itself.
Harnesses nest and their variables leak into child processes, so when several
are set the session holding the newest copy of this `start` call wins.

`end` sums everything from that anchor across the session and every subagent
session it spawned, diffs git against the snapshot, prints a markdown table,
and appends a row to .dev/metrics.jsonl so later runs can be compared against
earlier ones. Every value is measured; nothing here is narrated from memory.
A value a harness does not record is printed as "n/a", never as zero.

`end` also appends one entry to the cross-repository run journal under
$DEV_MEMORY_DIR (default ~/.dev-workflow/memory/journal/), which the reflect
skill consolidates into cited claims about the skills themselves. The entry
holds the friction signals measured from the session (interrupts, denied or
failed tool calls, the user's own turns, and how often each deterministic
checker ran and failed), each with its transcript path and line, plus at most
three `--friction` lines the skill narrates, kept apart as narrated. opencode
keeps sessions in SQLite, so its run is exported as JSONL beside the journal
for those lines to point into. Set DEV_MEMORY_DIR=off, or
`"memory": {"enabled": false}` in .dev/config.json, to skip the journal.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

TEST_FILE = re.compile(r"(^|/)(tests?|specs?|__tests__)(/|$)|[._-](test|spec)s?\.[A-Za-z]+$|^test_.*\.py$")
TOKEN_KEYS = ("input", "cache_read", "cache_write", "output")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
# The deterministic checkers the skills loop against; a failed run of one is loop
# progress, so it is counted per checker instead of logged as a tool error.
CHECKERS = ("lint-spec.py", "check-tests.py", "pr-evidence.py", "architecture-check.py",
            "check-agents-md.py", "aggregate-findings.py", "claims.py")
# A checker counts only where a command runs it, never where one greps or edits it.
CHECKER_RUN = re.compile(r"(?:^|[;&|(]\s*|\bpython3?\s+(?:-\S+\s+)*)\S*?(" + "|".join(re.escape(c) for c in CHECKERS) + r")(?=\s|$)",
                         re.M)
CLAUDE_INTERRUPT = "[Request interrupted by user"
DENIED = re.compile(r"doesn't want to proceed|was rejected|Permission denied by user|rejected permission"
                    r"|rejected by (?:the )?user|user (?:rejected|denied|declined)", re.I)
SIGNAL_KINDS = ("interrupt", "denied", "tool_error", "user_turn")
SIGNAL_CAP = 20
EXCERPT = 240
MAX_FRICTION = 3
# A gap with no activity in any of the run's sessions longer than this is a pause, not work.
IDLE_GAP = 30 * 60
# How long `start` waits for the harness to save the call that is running it.
LOCATE_WAIT = 5.0


def now_iso() -> str:
    return iso(time.time())


def iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(value: str) -> float:
    value = value.rstrip("Z")
    fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in value else "%Y-%m-%dT%H:%M:%S"
    return datetime.strptime(value[:26], fmt).replace(tzinfo=timezone.utc).timestamp()


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


def start_call(skill: str) -> re.Pattern:
    return re.compile(rf"skill-metrics\.py['\"]?\s+start\s+['\"]?{re.escape(skill)}(?=$|[\s;&|'\")])")


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(text_of(item.get("text", item.get("content", ""))) if isinstance(item, dict)
                         else str(item) for item in content)
    if isinstance(content, dict):
        return text_of(content.get("text", content.get("content", "")))
    return ""


def read_jsonl(path: Path):
    try:
        with path.open(encoding="utf-8") as handle:
            for index, raw in enumerate(handle):
                try:
                    yield index, json.loads(raw)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


# --- harness readers ----------------------------------------------------------
#
# Each reader turns one harness's session store into the same events:
#   usage  {id, tokens{input, cache_read, cache_write, output[, reasoning]}}  one model response
#   tool   {id, name, command}                                               a tool call
#   result {id, error, text}                                                 its outcome
#   prompt {text}                                                            something the user typed
#   interrupt {text}                                                         the user stopped the run
# Every event carries `line` (its line in the session's citable JSONL file) and `ts`.
# A session is {"harness", "id", "path"}; `path` is the file those lines point into.

class Claude:
    name = "claude"
    unmeasured: tuple[str, ...] = ()

    def config(self) -> Path:
        return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()

    def candidates(self) -> list[dict]:
        session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
        if not session_id:
            return []
        mains = sorted((self.config() / "projects").glob(f"*/{session_id}.jsonl"))
        found = []
        recent = time.time() - 15 * 60
        for main in mains:
            found.append({"harness": self.name, "id": session_id, "path": str(main)})
            # A subagent shares its parent's session id; its tool calls land in its own file.
            for sub in subagent_files(main):
                if sub.stat().st_mtime >= recent:
                    found.append({"harness": self.name, "id": sub.stem, "path": str(sub), "subagent": True})
        return found

    def events(self, session: dict, start: int = 0) -> list[dict]:
        out: list[dict] = []
        for index, obj in read_jsonl(Path(session["path"])):
            if index < start:
                continue
            ts = parse_iso(obj["timestamp"]) if obj.get("timestamp") else None
            message = obj.get("message") or {}
            base = {"line": index, "ts": ts}
            if obj.get("type") == "assistant":
                usage = message.get("usage")
                if usage:
                    out.append({**base, "kind": "usage", "id": message.get("id") or obj.get("uuid"), "tokens": {
                        "input": int(usage.get("input_tokens") or 0),
                        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
                        "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
                        "output": int(usage.get("output_tokens") or 0)}})
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        out.append({**base, "kind": "tool", "id": block.get("id", ""), "name": block.get("name", "?"),
                                    "command": str((block.get("input") or {}).get("command", ""))})
                continue
            if obj.get("type") != "user":
                if ts is not None:
                    out.append({**base, "kind": "tick"})
                continue
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                text = text_of(block.get("content", block.get("text", "")))
                if block.get("type") == "text" and CLAUDE_INTERRUPT in text:
                    out.append({**base, "kind": "interrupt", "text": text})
                elif block.get("type") == "tool_result":
                    out.append({**base, "kind": "result", "id": block.get("tool_use_id", ""),
                                "error": bool(block.get("is_error")), "text": text})
            if isinstance(content, str) and CLAUDE_INTERRUPT in content:
                out.append({**base, "kind": "interrupt", "text": content})
            else:
                typed = claude_human_text(obj)
                out.append({**base, "kind": "prompt", "text": typed} if typed else {**base, "kind": "tick"})
        return out

    def children(self, session: dict, since: float) -> list[dict] | None:
        if session.get("subagent"):
            return None  # nested agents share the parent's folder and cannot be told apart
        found = []
        for path in subagent_files(Path(session["path"])):
            first = next((parse_iso(o["timestamp"]) for _, o in read_jsonl(path) if o.get("timestamp")), None)
            if first is not None and first >= since:
                found.append({"harness": self.name, "id": path.stem, "path": str(path)})
        return found


def subagent_files(transcript: Path) -> list[Path]:
    folder = transcript.with_suffix("") / "subagents"
    return sorted(folder.glob("*.jsonl")) if folder.is_dir() else []


def claude_human_text(obj: dict) -> str | None:
    """The text a person typed, or None for tool results and injected turns."""
    if obj.get("isMeta") or obj.get("isSidechain"):
        return None
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        content = text_of([b for b in content if isinstance(b, dict) and b.get("type") == "text"])
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text.startswith("<") or CLAUDE_INTERRUPT in text or text.startswith("Base directory for this skill"):
        return None
    return text


EXIT_CODE = re.compile(r"(?:Process exited with code|Exit code:?)\s*(-?\d+)")


class Codex:
    name = "codex"
    unmeasured: tuple[str, ...] = ()

    def sessions_dir(self) -> Path:
        return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser() / "sessions"

    def candidates(self) -> list[dict]:
        thread = os.environ.get("CODEX_THREAD_ID")
        if not thread:
            return []
        paths = sorted(self.sessions_dir().glob(f"*/*/*/rollout-*{thread}.jsonl"))
        return [{"harness": self.name, "id": thread, "path": str(paths[-1])}] if paths else []

    def events(self, session: dict, start: int = 0) -> list[dict]:
        out: list[dict] = []
        cumulative: list[tuple[dict, dict]] = []
        recorded = False
        for index, obj in read_jsonl(Path(session["path"])):
            ts = parse_iso(obj["timestamp"]) if obj.get("timestamp") else None
            payload = obj.get("payload") or {}
            kind, ptype = obj.get("type"), payload.get("type")
            base = {"line": index, "ts": ts}
            if kind == "event_msg" and ptype == "token_count" and (payload.get("info") or {}).get("total_token_usage"):
                cumulative.append((base, payload["info"]["total_token_usage"]))
            if index < start:
                continue
            if kind == "token_usage_record":
                recorded = True
                out.append({**base, "kind": "usage", "id": payload.get("response_id") or index,
                            "tokens": codex_tokens(payload.get("usage") or {})})
            elif kind == "response_item" and ptype in ("function_call", "custom_tool_call"):
                command = ""
                if ptype == "function_call":
                    try:
                        args = json.loads(payload.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    command = args.get("cmd") or args.get("command") or ""
                    command = " ".join(command) if isinstance(command, list) else str(command)
                out.append({**base, "kind": "tool", "id": payload.get("call_id", ""),
                            "name": payload.get("name", "?"), "command": command})
            elif kind == "response_item" and ptype in ("function_call_output", "custom_tool_call_output"):
                text = text_of(payload.get("output", ""))
                code = EXIT_CODE.search(text[:400])
                failed = (code is not None and code.group(1) != "0") or bool(DENIED.search(text[:400]))
                out.append({**base, "kind": "result", "id": payload.get("call_id", ""), "error": failed, "text": text})
            elif kind == "event_msg" and ptype == "user_message":
                out.append({**base, "kind": "prompt", "text": str(payload.get("message", ""))})
            elif kind == "event_msg" and ptype == "item_completed" and (payload.get("item") or {}).get("type") == "UserMessage":
                out.append({**base, "kind": "prompt", "text": text_of(payload["item"].get("content", []))})
            elif kind == "event_msg" and ptype == "turn_aborted" and payload.get("reason") == "interrupted":
                out.append({**base, "kind": "interrupt", "text": "turn aborted: interrupted"})
            elif ts is not None:
                out.append({**base, "kind": "tick"})
        if not recorded and cumulative:
            # Older Codex logs only running totals; the run's share is the last total minus the one before it began.
            before = [usage for where, usage in cumulative if where["line"] < start]
            last_where, last = cumulative[-1]
            first = codex_tokens(before[-1]) if before else dict.fromkeys(TOKEN_KEYS, 0)
            now = codex_tokens(last)
            if last_where["line"] >= start:
                out.append({**last_where, "kind": "usage", "id": "cumulative",
                            "tokens": {k: max(0, now.get(k, 0) - first.get(k, 0)) for k in now}})
        # Some Codex versions log a typed message both as user_message and as a completed UserMessage item.
        prompts: dict[str, float] = {}
        kept = []
        for event in out:
            if event["kind"] == "prompt":
                seen = prompts.get(event["text"].strip())
                prompts[event["text"].strip()] = event["ts"] or 0
                if seen is not None and abs((event["ts"] or 0) - seen) < 5:
                    continue
            kept.append(event)
        return kept

    def children(self, session: dict, since: float) -> list[dict]:
        day = datetime.fromtimestamp(since, timezone.utc).date() - timedelta(days=1)
        folders = []
        while day <= datetime.now(timezone.utc).date() + timedelta(days=1):
            folders.append(self.sessions_dir() / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}")
            day += timedelta(days=1)
        metas = []
        for folder in folders:
            for path in sorted(folder.glob("rollout-*.jsonl")) if folder.is_dir() else []:
                meta = next((o.get("payload") or {} for _, o in read_jsonl(path) if o.get("type") == "session_meta"), {})
                # `source` is a plain string ("cli", "vscode") for top-level sessions.
                spawn = dig(meta, "source", "subagent", "thread_spawn")
                parent = meta.get("parent_thread_id") or (spawn or {}).get("parent_thread_id")
                if parent and meta.get("timestamp") and parse_iso(meta["timestamp"]) >= since:
                    metas.append((meta.get("id"), parent, path))
        family, found = {session["id"]}, []
        grew = True
        while grew:
            grew = False
            for child, parent, path in metas:
                if parent in family and child not in family:
                    family.add(child)
                    found.append({"harness": self.name, "id": child, "path": str(path)})
                    grew = True
        return found


def dig(value, *keys):
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def codex_tokens(usage: dict) -> dict:
    # Codex counts cached and cache-written tokens inside input_tokens, and reasoning inside output_tokens.
    cached = int(usage.get("cached_input_tokens") or 0)
    written = int(usage.get("cache_write_input_tokens") or 0)
    return {"input": max(0, int(usage.get("input_tokens") or 0) - cached - written), "cache_read": cached,
            "cache_write": written, "output": int(usage.get("output_tokens") or 0),
            "reasoning": int(usage.get("reasoning_output_tokens") or 0)}


class OpenCode:
    name = "opencode"
    unmeasured: tuple[str, ...] = ()

    def db_path(self) -> Path:
        data = Path(os.environ.get("XDG_DATA_HOME") or "~/.local/share").expanduser()
        return data / "opencode" / "opencode.db"

    def connect(self) -> sqlite3.Connection | None:
        path = self.db_path()
        if not path.exists():
            return None
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            db.execute("select 1 from session_message limit 1")
            return db
        except sqlite3.Error:
            return None

    def candidates(self) -> list[dict]:
        session_id = os.environ.get("OPENCODE_SESSION_ID")
        if not session_id or self.connect() is None:
            return []
        return [{"harness": self.name, "id": session_id, "path": None}]

    def rows(self, session_id: str, start: int) -> list[tuple]:
        db = self.connect()
        if db is None:
            return []
        try:
            return db.execute("select seq, type, time_created, data from session_message "
                              "where session_id = ? and seq >= ? order by seq", (session_id, start)).fetchall()
        except sqlite3.Error:
            return []

    def events(self, session: dict, start: int = 0) -> list[dict]:
        """Events from row `start` on; `line` is the row's line in the exported slice, `seq` its own number."""
        out: list[dict] = []
        for index, (seq, rtype, created, raw) in enumerate(self.rows(session["id"], start)):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            base = {"line": index, "seq": seq, "ts": created / 1000}
            if rtype == "user":
                out.append({**base, "kind": "prompt", "text": str(data.get("text", ""))})
                continue
            if rtype != "assistant":
                out.append({**base, "kind": "tick"})
                continue
            tokens = data.get("tokens")
            if isinstance(tokens, dict):
                cache = tokens.get("cache") or {}
                reasoning = int(tokens.get("reasoning") or 0)
                out.append({**base, "kind": "usage", "id": f"{session['id']}:{seq}", "tokens": {
                    "input": int(tokens.get("input") or 0), "cache_read": int(cache.get("read") or 0),
                    "cache_write": int(cache.get("write") or 0),
                    "output": int(tokens.get("output") or 0) + reasoning, "reasoning": reasoning}})
            for part in data.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and "[user interrupted]" in str(part.get("text", "")):
                    out.append({**base, "kind": "interrupt", "text": "[user interrupted]"})
                if part.get("type") != "tool":
                    continue
                state = part.get("state") or {}
                inputs = state.get("input") or {}
                out.append({**base, "kind": "tool", "id": part.get("id", ""), "name": part.get("name", "?"),
                            "command": str(inputs.get("command", ""))})
                status = state.get("status")
                if status in ("completed", "error"):
                    exit_code = (state.get("metadata") or {}).get("exit")
                    text = text_of(state.get("content", "")) or str((state.get("error") or {}).get("message", ""))
                    failed = status == "error" or (isinstance(exit_code, int) and exit_code != 0)
                    out.append({**base, "kind": "result", "id": part.get("id", ""), "error": failed, "text": text})
            if (data.get("error") or {}).get("type") == "aborted":
                out.append({**base, "kind": "interrupt", "text": str(data["error"].get("message", "aborted"))})
        return out

    def children(self, session: dict, since: float) -> list[dict]:
        db = self.connect()
        if db is None:
            return []
        family, found, frontier = {session["id"]}, [], [session["id"]]
        while frontier:
            marks = ",".join("?" * len(frontier))
            rows = db.execute(f"select id from session_v2 where parent_id in ({marks}) and time_created >= ? "
                              "order by time_created", (*frontier, int(since * 1000))).fetchall()
            frontier = [r[0] for r in rows if r[0] not in family]
            family.update(frontier)
            found += [{"harness": self.name, "id": sid, "path": None} for sid in frontier]
        return found

    def export(self, session: dict, start: int, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for seq, rtype, created, raw in self.rows(session["id"], start):
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = raw
                handle.write(json.dumps({"seq": seq, "type": rtype, "timestamp": iso(created / 1000),
                                         "session": session["id"], "message": data}) + "\n")


class Pi:
    name = "pi"
    # Pi has no permission prompts, and ship's Pi panel runs with --no-session, leaving no subagent log.
    unmeasured = ("denied",)

    def candidates(self) -> list[dict]:
        path = os.environ.get("PI_SESSION_FILE")
        if not path or not Path(path).is_file():
            return []
        return [{"harness": self.name, "id": os.environ.get("PI_SESSION_ID") or Path(path).stem, "path": path}]

    def events(self, session: dict, start: int = 0) -> list[dict]:
        out: list[dict] = []
        for index, obj in read_jsonl(Path(session["path"])):
            if index < start:
                continue
            ts = parse_iso(obj["timestamp"]) if obj.get("timestamp") else None
            base = {"line": index, "ts": ts}
            message = obj.get("message") or {}
            role = message.get("role") if obj.get("type") == "message" else None
            if role == "assistant":
                usage = message.get("usage") or {}
                if usage:
                    out.append({**base, "kind": "usage", "id": obj.get("id") or index, "tokens": {
                        "input": int(usage.get("input") or 0), "cache_read": int(usage.get("cacheRead") or 0),
                        "cache_write": int(usage.get("cacheWrite") or 0), "output": int(usage.get("output") or 0)}})
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "toolCall":
                        args = block.get("arguments") or {}
                        out.append({**base, "kind": "tool", "id": block.get("id", ""), "name": block.get("name", "?"),
                                    "command": str(args.get("command", "")) if isinstance(args, dict) else ""})
                if message.get("stopReason") == "aborted":
                    out.append({**base, "kind": "interrupt", "text": str(message.get("errorMessage") or "aborted")})
            elif role == "toolResult":
                out.append({**base, "kind": "result", "id": message.get("toolCallId", ""),
                            "error": bool(message.get("isError")), "text": text_of(message.get("content", ""))})
            elif role == "user":
                text = text_of(message.get("content", "")).strip()
                out.append({**base, "kind": "prompt", "text": text} if text and not text.startswith("<")
                           else {**base, "kind": "tick"})
            elif ts is not None:
                out.append({**base, "kind": "tick"})
        return out

    def children(self, session: dict, since: float) -> list[dict] | None:
        return None


HARNESSES = {h.name: h for h in (Claude(), Codex(), OpenCode(), Pi())}


def locate(skill: str) -> tuple[dict, int, float, bool] | None:
    """The session running this `start {skill}` call: (session, anchor position, call time, anchored).

    The call being run is the one with no result yet; an older copy of the same command, from an earlier
    run or quoted inside another command, already has one. A harness may save the call a moment after
    running it, so this polls briefly. With no such call, a lone session is anchored at its end instead.
    """
    pattern = start_call(skill)
    deadline = time.time() + LOCATE_WAIT
    while True:
        best, mains = None, []
        for harness in HARNESSES.values():
            for session in harness.candidates():
                events = harness.events(session)
                if not session.get("subagent"):
                    mains.append((session, events))
                answered = {e["id"] for e in events if e["kind"] == "result"}
                for event in reversed(events):
                    if event["kind"] == "tool" and event["id"] not in answered and pattern.search(event["command"]):
                        if best is None or (event["ts"] or 0) > best[2]:
                            best = (session, event.get("seq", event["line"]), event["ts"] or 0, True)
                        break
        if best or not mains or time.time() >= deadline:
            break
        time.sleep(0.25)
    if best is None and len(mains) == 1:
        session, events = mains[0]
        last = events[-1] if events else {"line": -1}
        best = (session, last.get("seq", last["line"]) + 1, time.time(), False)
    return best


# --- friction signals --------------------------------------------------------

def collect_signals(runs: list[tuple[dict, list[dict]]], main_id: str) -> dict:
    """Friction signals and checker runs across the main session and its subagents."""
    state: dict = {"counts": {}, "signals": [], "checkers": {}}
    for session, events in runs:
        pending: dict[str, str] = {}
        for event in events:
            where = {"transcript": session["path"], "line": event["line"]}
            if event["kind"] == "tool":
                match = CHECKER_RUN.search(event["command"])
                if match:
                    pending[event["id"]] = match.group(1)
                    state["checkers"].setdefault(match.group(1), {"runs": 0, "failed": 0})["runs"] += 1
            elif event["kind"] == "result" and event["error"]:
                checker = pending.get(event["id"])
                if checker:
                    entry = state["checkers"][checker]
                    entry["failed"] += 1
                    entry.setdefault("first_failure", where)
                else:
                    kind = "denied" if DENIED.search(event["text"]) else "tool_error"
                    note(state, kind, where, event["text"])
            elif event["kind"] == "interrupt":
                note(state, "interrupt", where, event["text"])
            elif event["kind"] == "prompt" and session["id"] == main_id and event["text"].strip():
                note(state, "user_turn", where, event["text"])
    return state


def note(state: dict, kind: str, where: dict, text: str) -> None:
    state["counts"][kind] = state["counts"].get(kind, 0) + 1
    if state["counts"][kind] <= SIGNAL_CAP:
        state["signals"].append({"type": kind, **where, "excerpt": " ".join(text.split())[:EXCERPT]})


def aggregate(events: list[dict]) -> dict:
    usage: dict = {}
    tools: Counter = Counter()
    for event in events:
        if event["kind"] == "usage":
            usage[event["id"]] = event["tokens"]
        elif event["kind"] == "tool":
            tools[event["name"]] += 1
    tokens = {key: sum(u.get(key, 0) for u in usage.values()) for key in TOKEN_KEYS}
    reasoning = [u["reasoning"] for u in usage.values() if "reasoning" in u]
    if reasoning:
        tokens["reasoning"] = sum(reasoning)
    return {"turns": len(usage), "tokens": tokens, "tools": tools}


def active_seconds(stamps: list[float], start: float, end: float) -> int:
    points = sorted({start, end, *(s for s in stamps if start <= s <= end)})
    return int(sum(b - a for a, b in zip(points, points[1:]) if b - a <= IDLE_GAP))


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


def run_id(started_at: str, skill: str) -> str:
    return f"r-{re.sub(r'[^0-9T]', '', started_at)[:15]}-{skill}"


def write_journal(root: Path, record: dict, ledger: Path, ledger_line: int, state: dict,
                  friction: list[str], found: dict, subagents: list[str], unmeasured: list[str]) -> tuple[Path, str]:
    rid = run_id(record["started_at"], record["skill"])
    entry = {
        "id": rid,
        "skill": record["skill"],
        "repo": repo_root().name,
        "repo_path": str(repo_root()),
        "harness": record["harness"],
        "started_at": record["started_at"],
        "ended_at": record["ended_at"],
        "seconds": record["seconds"],
        "transcript": state.get("transcript"),
        "anchor_line": state.get("anchor_line"),
        "subagent_transcripts": subagents,
        "ledger": str(ledger),
        "ledger_line": ledger_line,
        "counts": record["counts"],
        "signal_counts": found["counts"],
        "unmeasured_signals": unmeasured,
        "checkers": found["checkers"],
        "signals": found["signals"],
        "friction": friction,
    }
    journal = root / "journal" / f"{record['ended_at'][:10]}.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return journal, rid


# --- git ---------------------------------------------------------------------

def numstat(ref: str) -> tuple[int, int, set[str]]:
    output = run_git("diff", "--numstat", ref) or ""
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


def git_snapshot() -> dict | None:
    if run_git("rev-parse", "--git-dir") is None:
        return None
    head = run_git("rev-parse", "--verify", "-q", "HEAD")
    head = head.strip() if head else None
    # A repository with no commit yet diffs against the empty tree.
    added, removed, files = numstat(head or EMPTY_TREE)
    return {"head": head, "added": added, "removed": removed, "files": sorted(files)}


def git_delta(start: dict | None) -> dict | None:
    if start is None or run_git("rev-parse", "--git-dir") is None:
        return None
    head = start.get("head")
    added, removed, files = numstat(head or EMPTY_TREE)
    has_head = run_git("rev-parse", "--verify", "-q", "HEAD") is not None
    commits = run_git("rev-list", "--count", f"{head}..HEAD" if head else "HEAD") if has_head else "0"
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
    found = locate(skill)
    state: dict = {"skill": skill, "git": git_snapshot()}
    if found:
        session, position, stamp, anchored = found
        state.update({"harness": session["harness"], "session": session, "anchor": position,
                      "started_at": iso(stamp), "anchored": anchored,
                      "transcript": session["path"], "anchor_line": position if session["path"] else 0})
        where = f"{session['harness']} session {session['id']}, line {position}"
        if not anchored:
            where += " (its end; the start call itself was not found in it)"
    else:
        state.update({"harness": None, "session": None, "anchor": 0, "started_at": now_iso(), "anchored": False,
                      "transcript": None, "anchor_line": None})
        where = "now; no Claude Code, Codex, opencode, or Pi session holds this start call"
    (state_dir() / f"{skill}.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    print(f"metrics: {skill} run started, anchored at {where}")
    return 0


def fmt_tokens(tokens: dict) -> str:
    text = "in {} / cache read {} / cache write {} / out {}".format(*(human(tokens.get(k, 0)) for k in TOKEN_KEYS))
    return text + (f" (reasoning {human(tokens['reasoning'])})" if tokens.get("reasoning") else "")


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
    ended = time.time()
    session = state.get("session")
    harness = HARNESSES.get(state.get("harness") or "")
    memory = memory_dir()
    rid = run_id(state["started_at"], skill)

    main_events: list[dict] = []
    children: list[dict] | None = None
    runs: list[tuple[dict, list[dict]]] = []
    if harness and session:
        if harness.name == "opencode" and memory is not None:
            # SQLite rows are not lines a reader can open; export the run so its signals cite real lines.
            folder = memory / "transcripts" / rid
            session = {**session, "path": str(folder.with_suffix(".jsonl"))}
            harness.export(session, state["anchor"], Path(session["path"]))
        elif harness.name == "opencode":
            session = {**session, "path": f"opencode:{session['id']}"}
        main_events = harness.events(session, state["anchor"])
        children = harness.children(session, since)
        runs.append((session, main_events))
        for child in children or []:
            if harness.name == "opencode":
                child["path"] = (str(memory / "transcripts" / rid / "subagents" / f"{child['id']}.jsonl")
                                 if memory is not None else f"opencode:{child['id']}")
                if memory is not None:
                    harness.export(child, 0, Path(child["path"]))
            runs.append((child, harness.events(child)))
        state["transcript"] = session["path"]

    main = aggregate(main_events)
    agents = [aggregate(events) for _, events in runs[1:]]
    agent_tokens = {key: sum(a["tokens"].get(key, 0) for a in agents) for key in TOKEN_KEYS}
    if any("reasoning" in a["tokens"] for a in agents):
        agent_tokens["reasoning"] = sum(a["tokens"].get("reasoning", 0) for a in agents)
    all_tokens = {key: main["tokens"].get(key, 0) + agent_tokens.get(key, 0) for key in TOKEN_KEYS}
    tools = Counter(main["tools"])
    for agent in agents:
        tools.update(agent["tools"])
    measured = bool(main["turns"])
    stamps = [e["ts"] for _, events in runs for e in events if e.get("ts")]
    active = active_seconds(stamps, since, ended) if runs else None
    delta = git_delta(state.get("git"))
    found = collect_signals(runs, session["id"]) if session else {"counts": {}, "signals": [], "checkers": {}}
    unmeasured = list(harness.unmeasured) if harness and session else list(SIGNAL_KINDS)

    record = {
        "skill": skill,
        "harness": harness.name if harness and session else None,
        "started_at": state["started_at"],
        "ended_at": iso(ended),
        "seconds": int(ended - since),
        "active_seconds": active,
        "anchored": state["anchored"],
        "turns": main["turns"],
        "agents": len(agents) if children is not None else None,
        "tools": dict(tools),
        "tokens_measured": measured,
        "tokens_main": main["tokens"] if measured else None,
        "tokens_agents": agent_tokens if children is not None else None,
        "tokens_total": total(all_tokens) if measured else None,
        "git": delta,
        "counts": counts,
        "signal_counts": found["counts"],
        "unmeasured_signals": unmeasured,
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
            # Rows from runs whose harness was not read carry zero or no tokens; they are not a baseline.
            if row.get("skill") == skill and row.get("tokens_total"):
                previous.append(row)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger_line = sum(1 for _ in ledger.open(encoding="utf-8")) + 1 if ledger.exists() else 1
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    subagent_paths = [child["path"] for child, _ in runs[1:]]
    journaled = (write_journal(memory, record, ledger, ledger_line, state, friction, found, subagent_paths, unmeasured)
                 if memory is not None else None)
    print_table(skill, state, record, main, agents, children, tools, all_tokens, agent_tokens, delta, counts,
                found, unmeasured, previous, active, ended - since)
    print(f"\nledger: {ledger}")
    if journaled:
        print(f"journal: {journaled[0]} ({journaled[1]}, {len(friction)} narrated friction lines)")
    return 0


def print_table(skill, state, record, main, agents, children, tools, all_tokens, agent_tokens, delta, counts,
                found, unmeasured, previous, active, elapsed) -> None:
    name = record["harness"]
    measured = record["tokens_measured"]
    why = (f"{name} recorded no model responses since the start call" if name
           else "no harness session found; run inside Claude Code, Codex, opencode, or Pi")
    if name == "pi":
        no_agents = "n/a on pi (its panel runs with --no-session)"
    elif name == "claude":
        no_agents = "n/a (run inside a subagent)"
    else:
        no_agents = "n/a"
    top_tools = ", ".join(f"{tool} {n}" for tool, n in tools.most_common(4)) or "none"
    span = duration(elapsed) + " wall"
    if active is not None and elapsed - active > 60:
        span += f", {duration(active)} active (pauses over {IDLE_GAP // 60}m left out)"
    if name and not state["anchored"]:
        span += " (from the start call's end; the call itself was not found in the session)"
    rows = [
        ("harness", f"{name} (session {state['session']['id']})" if name else "n/a (not detected)"),
        ("duration", span),
        ("orchestrator turns / tool calls",
         f"{main['turns']} / {sum(tools.values())} ({top_tools})" if name else "n/a"),
        ("agents dispatched", str(len(agents)) if children is not None else no_agents),
        ("tokens orchestrator", fmt_tokens(main["tokens"]) if measured else f"n/a ({why})"),
        ("tokens agents", (fmt_tokens(agent_tokens) if agents else "0") if children is not None else no_agents),
        ("tokens total", (human(total(all_tokens)) + ("" if children is not None else " (orchestrator only)"))
         if measured else "n/a"),
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
    shown = [f"{n} {kind.replace('_', ' ')}" for kind, n in sorted(found["counts"].items())]
    shown += [f"{tool} {c['runs']} runs / {c['failed']} failed" for tool, c in sorted(found["checkers"].items())]
    text = ", ".join(shown) or "none measured"
    if unmeasured and name:
        text += f"; {', '.join(k.replace('_', ' ') for k in unmeasured)} n/a on {name}"
    elif not name:
        text = "n/a (no harness session found)"
    rows.append(("friction signals", text))
    if previous and measured:
        med_tokens = statistics.median(r["tokens_total"] for r in previous)
        med_secs = statistics.median(r["seconds"] for r in previous)
        change = (total(all_tokens) - med_tokens) / med_tokens * 100
        rows.append((
            f"vs previous {skill} runs",
            (f"{len(previous)} with tokens on record, median {human(med_tokens)} tokens in {duration(med_secs)}; "
             f"this run {change:+.0f}% tokens"),
        ))

    width = max(len(label) for label, _ in rows)
    print(f"## {skill} run metrics\n")
    print(f"| {'metric'.ljust(width)} | value |")
    print(f"| {'-' * width} | ----- |")
    for label, value in rows:
        print(f"| {label.ljust(width)} | {value} |")


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
