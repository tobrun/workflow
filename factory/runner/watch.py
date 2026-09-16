"""Live assembly-line view of one run, attached to the terminal.

The view only reads run.json, events.jsonl, and the current attempt's Codex
stream; the detached worker owns the run. Detaching (Ctrl-C, a closed terminal)
never stops the run.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, TextIO

from runner import conditions, config, control, events, hosts, supervise
from runner.dashboard import human_duration
from runner.model import CANCELLED, DONE, NEEDS_HUMAN, PAUSED, QUEUED, RUNNING, STAGES, Run, SchemaError, parse_ts

TERMINAL = (DONE, NEEDS_HUMAN, CANCELLED, PAUSED)
WORKER_DOWN_POLLS = 5


@dataclass
class StreamTail:
    """Incremental reader for the running attempt's stdout.jsonl."""
    path: Path | None = None
    offset: int = 0
    tokens: int = 0
    latest: str | None = None
    lines: list[str] = field(default_factory=list)

    def follow(self, path: Path) -> None:
        if path != self.path:
            self.path, self.offset, self.tokens, self.latest = path, 0, 0, None
        if not path.exists():
            return
        with path.open("rb") as handle:
            handle.seek(self.offset)
            chunk = handle.read()
        complete = chunk.rfind(b"\n") + 1
        self.offset += complete
        for raw in chunk[:complete].decode("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "turn.completed":
                usage = event.get("usage") or {}
                self.tokens += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
            rendered = hosts.render_event(event)
            if rendered:
                self.lines.append(" ".join(rendered.split()) if not rendered.startswith("  exit ") else rendered)
            if rendered and not rendered.startswith(("  exit ", "tokens ", "thread ")):
                self.latest = " ".join(rendered.split())

    def drain(self) -> list[str]:
        lines, self.lines = self.lines, []
        return lines


@dataclass
class Style:
    color: bool

    def paint(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def ok(self, text: str) -> str:
        return self.paint("32", text)

    def active(self, text: str) -> str:
        return self.paint("33", text)

    def bad(self, text: str) -> str:
        return self.paint("31", text)

    def dim(self, text: str) -> str:
        return self.paint("2", text)


def short_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def clock(ts: str | None) -> str:
    parsed = parse_ts(ts)
    return parsed.astimezone().strftime("%H:%M:%S") if parsed else datetime.now().strftime("%H:%M:%S")


def attempt_of(run: Run, stage: str | None, n: int | None) -> dict | None:
    return next((a for a in run.data["attempts"] if a["stage"] == stage and a["n"] == n), None)


def event_line(run: Run, entry: dict) -> str | None:
    """One history line for a notable event; None for plumbing events."""
    name, stage, n, data = entry["event"], entry.get("stage"), entry.get("attempt"), entry.get("data") or {}
    attempt = attempt_of(run, stage, n)
    if name == "stage.started":
        text = f"{stage} attempt {n} started ({data.get('model')}/{data.get('effort')})"
    elif name == "stage.finished":
        seconds = (attempt or {}).get("seconds")
        took = f" in {human_duration(seconds)}" if seconds is not None else ""
        reason = f": {data['reason']}" if data.get("reason") else ""
        text = f"{stage} attempt {n} {data.get('outcome')}{took}{reason}"
    elif name == "retry.scheduled":
        text = f"retry {data.get('used')}/{data.get('budget')} scheduled for {stage}"
    elif name == "run.queued" and data.get("previous"):
        text = f"{stage} queued after {data['previous']}"
    elif name in ("stage.committed", "scope.committed"):
        if data.get("nothing_to_commit"):
            return None
        text = f"{stage} committed {str(data.get('commit'))[:8]} ({len(data.get('paths') or [])} path(s))"
    elif name == "stage.warning":
        text = f"warning: {data.get('warning')}"
    elif name == "process.exited" and (data.get("timed_out") or data.get("spawn_error")):
        text = f"{stage} process {'timed out' if data.get('timed_out') else 'failed to start'}"
    elif name == "run.cancel_requested":
        text = "cancel requested"
    elif name == "run.pause_requested":
        text = "pause requested"
    elif name == "run.paused":
        text = f"paused before {stage}"
    elif name == "run.unpaused":
        text = f"continued at {stage}"
    elif name == "run.note_added":
        text = ("note saved for the next attempt: " if data.get("pending") else f"{stage} attempt {n} got note: ") \
            + str(data.get("note"))
    elif name == "run.resumed":
        text = f"resumed at {stage}"
    elif name == "run.retry_requested":
        text = f"retry requested at {stage}" + (f" with note: {data['note']}" if data.get("note") else "")
    elif name == "run.needs_human":
        text = f"needs you at {stage}: {data.get('reason')}"
    elif name == "run.cancelled":
        text = f"cancelled at {stage}: {data.get('reason')}"
    elif name == "run.done":
        text = f"done: {data.get('pr') or 'pull request ready'}"
    else:
        return None
    return f"{clock(entry.get('ts'))}  {text}"


def stage_rows(run: Run, style: Style, tail: StreamTail, now: float) -> list[str]:
    current = STAGES.index(run.stage)
    rows = []
    for index, stage in enumerate(STAGES):
        attempts = run.stage_attempts(stage)
        spent = sum(int(a.get("seconds") or 0) for a in attempts if a.get("ended_at"))
        label = f"{stage:<13}"
        if index < current or (index == current and run.status == DONE):
            extra = f"{len(attempts)} attempts, " if len(attempts) > 1 else ""
            rows.append(f"  {style.ok('✓')} {label}{style.ok('done')}     {extra}{human_duration(spent)}")
        elif index > current:
            rows.append(f"  {style.dim('·')} {style.dim(label.rstrip())}")
        elif run.status == RUNNING:
            live = attempts[-1] if attempts and not attempts[-1].get("ended_at") else None
            started = parse_ts(live["started_at"]).timestamp() if live else now
            parts = [f"attempt {live['n'] if live else len(attempts) + 1}", human_duration(now - started)]
            if tail.tokens:
                parts.append(f"{short_tokens(tail.tokens)} tokens")
            rows.append(f"  {style.active('▶')} {label}{style.active('running')}  {', '.join(parts)}")
        elif run.status == QUEUED:
            waiting = f"attempt {len(attempts) + 1}" + (", waiting for a stage slot" if stage != "scope" else "")
            rows.append(f"  {style.active('…')} {label}{style.active('queued')}   {waiting}")
        elif run.status == NEEDS_HUMAN:
            rows.append(f"  {style.bad('!')} {label}{style.bad('needs you')}")
        elif run.status == PAUSED:
            rows.append(f"  {style.active('‖')} {label}{style.active('paused')}   attempt {len(attempts) + 1} waits for you")
        elif run.status == CANCELLED:
            rows.append(f"  {style.bad('✗')} {label}{style.bad('cancelled')}")
        else:
            rows.append(f"  {style.active('▶')} {label}{run.status}")
    return rows


def status_block(run: Run, style: Style, tail: StreamTail, now: float, alive: bool, width: int) -> list[str]:
    lines = stage_rows(run, style, tail, now)
    retries = run.data["retries"]
    if alive:
        worker = style.ok("worker alive")
    else:
        worker = style.dim("worker finished") if run.status in TERMINAL else style.bad("worker not running")
    tokens = run.data.get("tokens_total", 0) + (tail.tokens if run.status == RUNNING else 0)
    usage = run.data.get("usage") or {}
    cached = f" ({short_tokens(usage['cached_input'])} cached input)" if usage.get("cached_input") else ""
    waiting = sum(a.get("lease_wait_seconds") or 0 for a in run.data["attempts"][-1:])
    wait = f", waited {waiting:.0f}s for a heavy-command lease" if waiting >= 1 else ""
    footer = f"  retries {retries['used']}/{retries['budget']}, {short_tokens(tokens)} tokens{cached}{wait}, {worker}"
    lines.append(footer)
    if run.status in (NEEDS_HUMAN, CANCELLED, PAUSED) and run.data["human"].get("reason"):
        # Wrapped, not clipped: the reason is what the operator has to act on.
        for line in textwrap.wrap(run.data["human"]["reason"], width - 3)[:BLOCKER_LINES]:
            lines.append("  " + style.bad(line))
    if run.status == RUNNING and tail.latest:
        lines.append("  " + style.dim(clip("> " + tail.latest, width - 3)))
    return lines


BLOCKER_LINES = 12


def clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(1, width - 3)] + "..."


class TerminalKeys:
    """Single-key input from a terminal in cbreak mode, with line prompts in between."""

    def __init__(self, stream: TextIO = sys.stdin):
        self.stream = stream
        self.fd = stream.fileno()
        self.saved = None

    @staticmethod
    def available(stream_in: TextIO = sys.stdin, stream_out: TextIO = sys.stdout) -> bool:
        try:
            return stream_in.isatty() and stream_out.isatty() and os.name == "posix"
        except (AttributeError, ValueError):
            return False

    def __enter__(self) -> "TerminalKeys":
        import termios
        import tty
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc: object) -> None:
        import termios
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)

    def read(self, timeout: float) -> list[str]:
        import select
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return []
        return list(os.read(self.fd, 64).decode("utf-8", errors="ignore"))

    def prompt(self, text: str, write: Callable[[str], None]) -> str:
        import termios
        import tty
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        try:
            write(text)
            return self.stream.readline().strip()
        finally:
            tty.setcbreak(self.fd)


@dataclass
class Watcher:
    run_dir: Path
    out: TextIO = field(default_factory=lambda: sys.stdout)
    tty: bool | None = None
    poll: float = 1.0
    heartbeat_seconds: float = 30
    since: int | None = None
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    width: int | None = None
    keys: object | None = None
    home: Path | None = None
    cfg: config.Config | None = None
    opener: Callable[[Path], None] | None = None

    def __post_init__(self) -> None:
        if self.tty is None:
            self.tty = hasattr(self.out, "isatty") and self.out.isatty()
        self.style = Style(color=bool(self.tty) and not os.environ.get("NO_COLOR"))
        self.tail = StreamTail()
        self.drawn = 0
        self.last_plain: str | None = None
        self.last_plain_at = 0.0
        self.verbose = False
        self.help = False
        self.notices: list[str] = []
        self.home = self.home or self.run_dir.parent.parent
        self.cfg = self.cfg or config.load(self.home)

    @property
    def interactive(self) -> bool:
        return self.keys is not None

    def columns(self) -> int:
        return self.width or shutil.get_terminal_size((100, 24)).columns

    def write(self, text: str) -> None:
        self.out.write(text)
        self.out.flush()

    def notice(self, text: str) -> None:
        self.notices.append(f"{datetime.now().strftime('%H:%M:%S')}  {text}")

    # --- loop ---------------------------------------------------------------------

    def run(self) -> int:
        if self.interactive and hasattr(self.keys, "__enter__"):
            with self.keys:
                return self.loop()
        return self.loop()

    def loop(self) -> int:
        run = Run.load(self.run_dir)
        seen = len(events.read(self.run_dir)) if self.since is None else self.since
        if self.since is None:
            history = [line for line in (event_line(run, e) for e in events.read(self.run_dir)[-40:]) if line][-5:]
            for line in history:
                self.write(self.style.dim(line) + "\n")
        down_polls = 0
        try:
            while True:
                try:
                    run = Run.load(self.run_dir)
                except (SchemaError, OSError):
                    self.sleep(self.poll)
                    continue
                entries = events.read(self.run_dir)
                fresh = [line for line in (event_line(run, e) for e in entries[seen:]) if line]
                seen = len(entries)
                live = run.last_attempt(run.stage) if run.status == RUNNING else None
                current = (run.attempt_dir(live["stage"], live["n"]) / "stdout.jsonl"
                           if live and live.get("host") == "codex" and not live.get("ended_at") else None)
                stream = []
                if self.tail.path is not None and self.tail.path != current:
                    # The followed attempt ended since the last poll: show its final lines before letting go.
                    self.tail.follow(self.tail.path)
                    stream = self.tail.drain()
                    self.tail = StreamTail(latest=self.tail.latest)
                if current is not None:
                    self.tail.follow(current)
                stream += self.tail.drain()
                if self.verbose:
                    fresh += [self.style.dim(f"    | {line}") for line in stream]
                alive = supervise.worker_alive(self.run_dir)
                down_polls = down_polls + 1 if not alive and run.status in (QUEUED, RUNNING) else 0
                worker_down = down_polls >= WORKER_DOWN_POLLS
                self.render(run, fresh + self.notices, alive, worker_down)
                self.notices = []
                if run.status == DONE and not alive:
                    return self.finish(run)
                if not self.interactive:
                    if run.status in TERMINAL and not alive:
                        return self.finish(run)
                    if worker_down:
                        self.clear()
                        self.write(f"The worker for {run.id} stopped while the run is {run.status} at {run.stage}.\n")
                        self.write(f"> factory resume {run.id}\n")
                        return 2
                    self.sleep(self.poll)
                    continue
                for key in self.keys.read(self.poll):
                    # Judge each key against the state at the moment it was pressed, not the last frame.
                    run, alive = Run.load(self.run_dir), supervise.worker_alive(self.run_dir)
                    result = self.handle(key, run, alive, worker_down and not alive)
                    if result is not None:
                        return result
        except KeyboardInterrupt:
            return self.detach(Run.load(self.run_dir))

    # --- controls -------------------------------------------------------------------

    def legend(self, run: Run, alive: bool, worker_down: bool) -> str:
        if run.status in (NEEDS_HUMAN, CANCELLED) and not alive:
            options = "r retry  b retry with a fresh budget  n note  q quit"
        elif run.status == PAUSED and not alive:
            options = "r continue  n note  c cancel  q quit"
        elif worker_down:
            options = "r resume the worker  c cancel  q quit"
        else:
            pause = "p withdraw pause" if (run.dir / "pause").exists() else "p pause"
            options = f"{pause}  n note  c cancel  v {'quiet' if self.verbose else 'verbose'}  d dashboard  q detach"
        return f"  keys: {options}" + ("" if self.help else "  ? help")

    def help_lines(self) -> list[str]:
        return [
            "  p  pause before the next attempt starts; the running attempt finishes first (p again withdraws it)",
            "  n  leave a note the next attempt's prompt carries",
            "  c  cancel: stops the running Codex process and keeps the worktree and any draft PR",
            "  r  retry a parked run, continue a paused one, or resume a dead worker",
            "  b  retry after resetting the shared retry budget",
            "  v  stream every Codex command and message; d opens the dashboard; q or Ctrl-C leaves the run going",
        ]

    def handle(self, key: str, run: Run, alive: bool, worker_down: bool) -> int | None:
        parked = run.status in (NEEDS_HUMAN, CANCELLED, PAUSED) and not alive
        try:
            if key in ("q", "\x04"):
                return self.detach(run)
            if key == "?":
                self.help = not self.help
            elif key == "v":
                self.verbose = not self.verbose
                self.notice("streaming Codex activity" if self.verbose else "Codex activity stream off")
            elif key == "d":
                control.refresh(self.home, self.cfg)
                path = self.home / "dashboard.html"
                (self.opener or open_path)(path)
                self.notice(f"dashboard: {path}")
            elif key == "n":
                text = self.ask("Note for the next attempt (Enter to skip): ")
                if text:
                    self.emit(control.add_note(self.run_dir, text))
            elif key == "p" and not parked:
                if (run.dir / "pause").exists():
                    self.emit(control.withdraw_pause(self.run_dir))
                else:
                    self.emit(control.request_pause(self.home, self.cfg, self.run_dir))
            elif key == "c" and run.status not in (DONE, CANCELLED):
                if self.ask(f"Cancel {run.id}? The running Codex process stops. [y/N] ").lower() in ("y", "yes"):
                    self.emit(control.cancel(self.home, self.cfg, self.run_dir))
            elif key in ("r", "b") and run.status == PAUSED and not alive:
                self.emit(control.resume(self.home, self.cfg, self.run_dir))
            elif key in ("r", "b") and run.status in (NEEDS_HUMAN, CANCELLED) and not alive:
                note = self.ask("Note for the retry (Enter for none): ")
                self.emit(control.retry(self.home, self.cfg, self.run_dir, note=note or None, reset_budget=key == "b"))
            elif key == "r" and worker_down:
                self.emit(control.resume(self.home, self.cfg, self.run_dir))
        except control.ControlError as error:
            self.notice(self.style.bad(f"not done: {error}"))
        return None

    def ask(self, text: str) -> str:
        self.clear()
        return self.keys.prompt(text, self.write)

    def emit(self, outcome: control.Outcome) -> None:
        for line in outcome.lines:
            if not line.startswith("> "):
                self.notice(line)

    def detach(self, run: Run) -> int:
        self.clear()
        if run.status in (QUEUED, RUNNING) or supervise.worker_alive(self.run_dir):
            self.write(f"\nDetached from {run.id}; the run keeps going.\n> factory watch {run.id}\n")
            return 0
        self.write("\n")
        return self.finish(run)

    # --- rendering ----------------------------------------------------------------------

    def render(self, run: Run, fresh: list[str], alive: bool, worker_down: bool = False) -> None:
        width = self.columns()
        if not self.tty:
            for line in fresh:
                self.write(line + "\n")
            summary = self.plain_summary(run)
            if summary != self.last_plain or self.now() - self.last_plain_at >= max(60.0, self.heartbeat_seconds):
                if run.status not in TERMINAL:
                    self.write(f"{datetime.now().strftime('%H:%M:%S')}  {summary}\n")
                self.last_plain, self.last_plain_at = summary, self.now()
            return
        block = status_block(run, self.style, self.tail, self.now(), alive, width)
        if self.interactive and not (run.status == DONE and not alive):
            if self.help:
                block += [self.style.dim(clip(line, width - 1)) for line in self.help_lines()]
            block.append(self.style.dim(clip(self.legend(run, alive, worker_down), width - 1)))
        self.clear()
        for line in fresh:
            self.write(clip(line, width - 1) + "\n")
        self.write("\n".join(block) + "\n")
        self.drawn = len(block)

    def clear(self) -> None:
        if self.tty and self.drawn:
            self.write(f"\x1b[{self.drawn}F\x1b[J")
            self.drawn = 0

    def plain_summary(self, run: Run) -> str:
        if run.status == RUNNING:
            live = run.last_attempt(run.stage)
            return f"{run.stage} running attempt {live['n'] if live else '?'}"
        if run.status == QUEUED:
            return f"{run.stage} queued for a stage slot"
        return f"{run.stage} {run.status}"

    def finish(self, run: Run) -> int:
        self.write("\n")
        if run.status == DONE:
            pr = run.data["pr"]
            self.write(f"Done: {pr.get('url') or 'pull request recorded'}"
                       + (" (draft)" if pr.get("draft") else "") + "\n")
            return 0
        reason = run.data["human"].get("reason") or "no reason recorded"
        if run.status == NEEDS_HUMAN:
            open_conditions = conditions.open_for(run, run.stage)
            if open_conditions:
                lines = [f"Needs you at {run.stage}: {reason}"]
                for condition in open_conditions:
                    lines.append(f"  {condition['id']} {condition['code']}: {condition['summary']}")
                    lines += [f"    evidence: {item}" for item in condition.get("evidence", [])[:3]]
                    lines.append(f"> {conditions.next_action(condition, run.id)}")
                self.write("\n".join(lines) + "\n")
            else:
                self.write(f"Needs you at {run.stage}: {reason}\n> factory retry {run.id} --note \"...\"\n")
        elif run.status == PAUSED:
            self.write(f"Paused before {run.stage}.\n> factory resume {run.id}\n")
        else:
            exhausted = run.budget_remaining() <= 0
            self.write(f"Cancelled at {run.stage}: {reason}\n> factory retry {run.id}"
                       + (" --reset-budget" if exhausted else "") + "\n")
        return 1


def open_path(path: Path) -> None:
    import subprocess
    subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(path)], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
