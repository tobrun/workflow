#!/usr/bin/env python3
"""Render a dev:remote run's Claude Code stream for people.

Usage:
  view.py RUN_DIR      follow a run live in a terminal (the sandbox desktop's viewer)

runner.sh opens this in a terminal on the workspace desktop, so an operator
watching over VNC sees each phase as it happens: what the agent says, the
tools it calls, errors, retries, phase changes, and how the run ended. It
only reads RUN_DIR (run.json, status.json, logs/*.jsonl) and never changes
the run. It keeps the window open after the run ends; the next run on the
workspace closes it.

remote.py imports parse() and render_event() from here for `logs`, so the
laptop and the desktop read the stream the same way.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import textwrap
import time
import traceback
from pathlib import Path
from typing import NamedTuple


class Item(NamedTuple):
    kind: str  # session, result, say, tool, error, retry, task
    text: str
    name: str = ""  # the tool name, for tool items
    detail: str = ""  # a tool's command or other full input
    nested: bool = False  # emitted by a subagent
    ok: bool = True  # result items: the session ended successfully


HINT_KEYS = ("description", "command", "file_path", "pattern", "skill", "url", "prompt", "query")


def brief(value: object, limit: int = 140) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content)
    return json.dumps(content, ensure_ascii=False)


def parse(line: str) -> list[Item]:
    """The items of one stream-json line worth showing a person; most lines have none."""
    if '"thinking_tokens"' in line[:120]:
        return []
    try:
        event = json.loads(line)
    except ValueError:
        return [Item("say", line.strip())] if line.strip() else []
    if not isinstance(event, dict):
        return []
    kind, subtype = event.get("type"), event.get("subtype")
    nested = bool(event.get("parent_tool_use_id"))
    if kind == "system" and subtype == "init":
        return [Item("session", f"session {event.get('session_id')} started (model {event.get('model', '?')})")]
    if kind == "result":
        ok = subtype == "success" and not event.get("is_error")
        return [Item("result", f"{subtype} after {event.get('num_turns', '?')} turns, "
                               f"{round((event.get('duration_ms') or 0) / 60000, 1)} min, "
                               f"error={event.get('is_error')}", ok=ok)]
    if kind == "system" and subtype == "api_retry":
        why = " ".join(str(v) for v in (event.get("error_status"), event.get("error")) if v)
        return [Item("retry", f"api retry {event.get('attempt')}/{event.get('max_retries')}: {why or 'unknown'}")]
    if kind == "system" and subtype == "task_notification" and event.get("status") not in (None, "completed"):
        return [Item("task", f"task {event.get('status')}: {event.get('summary') or event.get('task_id')}")]
    out = []
    content = (event.get("message") or {}).get("content")
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if kind == "assistant" and block.get("type") == "text" and block.get("text", "").strip():
            out.append(Item("say", block["text"].strip(), nested=nested))
        elif kind == "assistant" and block.get("type") == "tool_use":
            inp = block.get("input") if isinstance(block.get("input"), dict) else {}
            key = next((k for k in HINT_KEYS if isinstance(inp.get(k), str) and inp[k].strip()), None)
            command = inp.get("command") if key != "command" and isinstance(inp.get("command"), str) else ""
            out.append(Item("tool", inp[key] if key else json.dumps(inp, ensure_ascii=False),
                            name=str(block.get("name")), detail=command, nested=nested))
        elif kind == "user" and block.get("type") == "tool_result" and block.get("is_error"):
            out.append(Item("error", text_of(block.get("content")).strip(), nested=nested))
    return out


LABELS = {"say": "say: ", "tool": "tool: ", "error": "tool error: ", "retry": "retry: ", "task": "",
          "session": "", "result": "result: "}


def render_event(line: str) -> list[str]:
    """One-line renderings for `remote.py logs`."""
    out = []
    for item in parse(line):
        if item.kind == "tool":
            body = f"{item.name} {brief(item.text, 110)}"
        else:
            body = brief(item.text, 120 if item.kind == "error" else 140)
        out.append(("  " if item.nested else "") + LABELS[item.kind] + body)
    return out


# --- the live terminal view ----------------------------------------------------

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = (f"\033[{n}m" for n in (31, 32, 33, 34, 35, 36))
STATE_COLOR = {"starting": BLUE, "running": BLUE, "done": GREEN, "failed": RED, "stopped": YELLOW, "lost": RED}
ENDED = ("done", "failed", "stopped")


def width() -> int:
    return max(40, shutil.get_terminal_size((120, 40)).columns)


def emit(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def wrapped(text: str, first: str, rest: str, color: str = "", limit: int | None = None) -> None:
    """Print text wrapped to the terminal, `first` before line one and `rest` before the others."""
    span = width() - max(len(first), len(rest)) - 1
    lines = []
    for para in text.splitlines() or [""]:
        lines.extend(textwrap.wrap(para, span, replace_whitespace=False, drop_whitespace=True) or [""])
    if limit is not None and len(lines) > limit:
        lines = lines[:limit] + [f"... {len(lines) - limit} more lines"]
    for i, line in enumerate(lines):
        emit((first if i == 0 else rest) + color + line + (RESET if color else ""))


def clip(text: str, span: int) -> str:
    return text if len(text) <= span else text[: max(0, span - 3)] + "..."


def settle() -> None:
    """Wait for the window to reach its size: a terminal starts small and is maximized after."""
    seen, steady = None, 0
    for _ in range(20):
        size = shutil.get_terminal_size((0, 0))
        steady = steady + 1 if size == seen else 0
        if steady >= 3:
            return
        seen = size
        time.sleep(0.1)


def show(item: Item, checkout: re.Pattern | None = None) -> None:
    bar = f"{DIM}│{RESET} " if item.nested else ""
    if checkout is not None and item.kind == "tool":
        # Every session runs in the checkout, so a leading `cd` into it says nothing.
        item = item._replace(text=checkout.sub("", item.text, count=1), detail=checkout.sub("", item.detail, count=1))
    if item.kind == "say":
        emit(bar)
        wrapped(item.text, f"{bar}{BOLD}●{RESET} ", f"{bar}  ")
    elif item.kind == "tool":
        # The hint's first line heads the call; a multi-line hint (a heredoc command) or the
        # command behind a description follows dimmed, three lines at most.
        head, *more = [line.rstrip() for line in item.text.strip().splitlines()] or [""]
        emit(f"{bar}  {CYAN}{BOLD}{item.name}{RESET} {clip(head, width() - len(item.name) - 6)}")
        lines = more if more else [line.rstrip() for line in item.detail.strip().splitlines()]
        mark = "  " if more else "$ "
        for line in lines[:3]:
            emit(f"{bar}    {DIM}{mark}{clip(line, width() - 8)}{RESET}")
        if len(lines) > 3:
            emit(f"{bar}    {DIM}  ... {len(lines) - 3} more lines{RESET}")
    elif item.kind == "error":
        wrapped(item.text, f"{bar}  {RED}✗{RESET} ", f"{bar}    ", RED, limit=4)
    elif item.kind == "retry":
        emit(f"{bar}  {YELLOW}↻ {item.text}{RESET}")
    elif item.kind == "task":
        emit(f"{bar}  {YELLOW}◦ {clip(item.text, width() - 6)}{RESET}")
    elif item.kind == "session":
        emit(f"{DIM}{item.text}{RESET}")
    elif item.kind == "result":
        emit()
        emit(f"{GREEN if item.ok else RED}{BOLD}result: {item.text}{RESET}")


def rule(label: str, color: str) -> None:
    text = f"━━ {label} "
    emit()
    emit(f"{color}{BOLD}{text}{'━' * max(0, width() - len(text))}{RESET}")


def title(text: str) -> None:
    sys.stdout.write(f"\033]0;{text}\007")
    sys.stdout.flush()


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def unit_active(run: str) -> bool | None:
    try:
        done = subprocess.run(["systemctl", "is-active", "--quiet", f"dev-run-{run}"], check=False, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode == 0


class Follower:
    """Prints a run's phase logs and state changes as they happen."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.cfg = read_json(run_dir / "run.json")
        self.run = str(self.cfg.get("id") or run_dir.name)
        phases = self.cfg.get("phases")
        self.phases = [str(p) for p in phases] if isinstance(phases, list) else []
        self.offsets: dict[str, int] = {}
        self.partial: dict[str, bytes] = {}
        self.shown_phase = ""
        self.state = ""
        self.gate = ""
        self.resumed: str | None = None  # None until the first status read, so a replay shows no resume
        self.last_event = time.time()
        self.lost = False
        self.next_unit_check = 0.0
        repo = self.cfg.get("repo")
        self.checkout = re.compile(r"^cd (%s)/?\s*(;|&&)\s*" % "|".join(
            re.escape(p) for p in (f"{Path.home()}/work/{repo}", f"~/work/{repo}", f"$HOME/work/{repo}"))
        ) if repo else None

    def header(self) -> None:
        cfg = self.cfg
        emit(f"{BOLD}dev:remote{RESET} run {BOLD}{self.run}{RESET}")
        emit(f"{DIM}plan {cfg.get('plan', '?')} · branch {cfg.get('branch', '?')} · "
             f"phases {' → '.join(self.phases) or '?'} · publish {cfg.get('publish', '?')}{RESET}")

    def drain(self, phase: str, status: dict) -> None:
        path = self.run_dir / "logs" / f"{phase}.jsonl"
        try:
            with path.open("rb") as handle:
                handle.seek(self.offsets.get(phase, 0))
                chunk = handle.read()
                self.offsets[phase] = handle.tell()
        except OSError:
            return
        if not chunk:
            return
        self.last_event = time.time()
        data = self.partial.pop(phase, b"") + chunk
        lines = data.split(b"\n")
        if lines[-1]:
            self.partial[phase] = lines[-1]  # an event still being written
        for raw in lines[:-1]:
            items = parse(raw.decode("utf-8", errors="replace"))
            if items and phase != self.shown_phase:
                if self.shown_phase:
                    self.show_gate(status)  # the gate ran between the phase before and this one
                self.shown_phase = phase
                rule(f"phase {phase}  {time.strftime('%H:%M')}", MAGENTA)
            for item in items:
                show(item, self.checkout)

    # One poll can see several steps at once (a resume, the end of a phase, its gate, the next
    # phase), so each kind of change prints where it happened relative to the logs.

    def show_opening(self, status: dict) -> None:
        """Before the logs: a resume or a start, which precede what the poll's new events say."""
        resumed = str(status.get("resumed") or "")
        if resumed and self.resumed is not None and resumed != self.resumed:
            self.lost = False
            rule(f"resumed  {time.strftime('%H:%M')}", BLUE)
        self.resumed = resumed
        if status.get("state") == "starting" and self.state != "starting" and not resumed:
            self.state = "starting"
            rule(f"starting  {time.strftime('%H:%M')}", BLUE)

    def show_gate(self, status: dict) -> None:
        gate = status.get("gate")
        gate = str(gate.get("build", "")) if isinstance(gate, dict) else ""
        if gate and gate != self.gate:
            self.gate = gate
            emit()
            emit(f"{GREEN if gate == 'pass' else YELLOW}{BOLD}build gate: {gate}{RESET}")

    def show_closing(self, status: dict) -> None:
        """After the logs: how the run ended, or that its runner vanished."""
        state = str(status.get("state") or "")
        if state and state != self.state:
            self.state = state
            if state in ENDED:
                reason = status.get("reason")
                rule(f"{state}{': ' + str(reason) if reason else ''}  {time.strftime('%H:%M')}",
                     STATE_COLOR.get(state, BOLD))
        if state in ("starting", "running") and time.time() >= self.next_unit_check:
            self.next_unit_check = time.time() + 10
            active = unit_active(self.run)
            if active is False and not self.lost:
                # status.json still says running, but the runner is gone: remote.py reports this as lost.
                self.lost = True
                rule(f"lost: the runner unit ended without recording an end  {time.strftime('%H:%M')}", RED)
            elif active:
                self.lost = False

    def tick(self) -> None:
        status = read_json(self.run_dir / "status.json")
        self.show_opening(status)
        for phase in self.phases:
            self.drain(phase, status)
        self.show_gate(status)
        self.show_closing(status)
        state = "lost" if self.lost else (self.state or "waiting")
        phase = str(status.get("phase") or "")
        idle = int(time.time() - self.last_event)
        quiet = f" · idle {idle // 60}m" if state == "running" and idle >= 120 else ""
        title(f"dev:remote {self.run} · {phase + ' · ' if phase else ''}{state}{quiet}")


def follow(run_dir: Path) -> None:
    settle()
    follower = Follower(run_dir)
    follower.header()
    while True:
        follower.tick()
        if not run_dir.is_dir():
            emit(f"{DIM}run directory removed; nothing left to follow{RESET}")
            while True:
                time.sleep(3600)
        time.sleep(1)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip().split("\n\n")[1], file=sys.stderr)
        return 2
    try:
        follow(Path(argv[0]))
    except KeyboardInterrupt:
        return 130
    except Exception:  # keep the window: a viewer that vanishes says nothing about why
        emit(f"{RED}view.py stopped following this run:{RESET}")
        emit(traceback.format_exc())
        while True:
            time.sleep(3600)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
