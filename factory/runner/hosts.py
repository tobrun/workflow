"""Host CLI argv builders, the attempt environment, and Codex event-stream parsing."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from runner.config import binary


def codex_argv(*, prompt: str, model: str, effort: str, sandbox: str, worktree: Path, writable: list[Path],
               last_message: Path, child_agents: int | None = None, agent_depth: int | None = None) -> list[str]:
    """`writable` are the extra roots a workspace-write stage may write besides the worktree.

    They are the run's report, evidence, and scratch directories and the Git metadata a
    linked worktree needs, never the run directory (runner-owned records) or the common
    Git directory (config, hooks, and excludes the runner's own Git calls read).
    """
    argv = [binary("codex"), "exec", prompt, "-m", model, "-c", f'model_reasoning_effort="{effort}"']
    if sandbox == "workspace-write":
        argv += [
            "-c", 'approval_policy="never"',
            "-s", "workspace-write",
            "-c", "sandbox_workspace_write.network_access=true",
        ]
    elif sandbox == "bypass":
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        raise ValueError(f"unknown codex sandbox mode {sandbox!r}")
    argv += ["-c", 'shell_environment_policy.inherit="all"']
    if child_agents is not None:
        # Enforced by Codex itself (it rejects values below 1), not by a prompt instruction.
        argv += ["-c", f"agents.max_concurrent_threads_per_session={child_agents}"]
    if agent_depth is not None:
        argv += ["-c", f"agents.max_depth={agent_depth}"]
    for root in writable:
        argv += ["--add-dir", str(root)]
    argv += [
        "-C", str(worktree),
        "--skip-git-repo-check",
        "--json",
        "-o", str(last_message),
    ]
    return argv


FOREMAN_SANDBOXES = ("read-only", "workspace-write")


def codex_foreman_argv(*, prompt: str, model: str, effort: str, sandbox: str, worktree: Path, writable: list[Path],
                       last_message: Path, schema: Path, thread_id: str | None = None) -> list[str]:
    """One foreman turn: a new session, or `exec resume` of the run's session.

    `resume` takes no -s, -C, or --add-dir, so the sandbox and writable roots go through config
    overrides and the working directory through the executor request; a new session uses the flags.
    """
    if sandbox not in FOREMAN_SANDBOXES:
        raise ValueError(f"unknown foreman sandbox {sandbox!r}")
    argv = [binary("codex"), "exec"]
    argv += ["resume", thread_id, prompt] if thread_id else [prompt]
    argv += ["-m", model, "-c", f'model_reasoning_effort="{effort}"', "-c", 'approval_policy="never"',
             "-c", 'shell_environment_policy.inherit="all"']
    if thread_id:
        argv += ["-c", f'sandbox_mode="{sandbox}"']
        if sandbox == "workspace-write":
            roots = json.dumps([str(worktree), *map(str, writable)])
            argv += ["-c", "sandbox_workspace_write.network_access=true",
                     "-c", f"sandbox_workspace_write.writable_roots={roots}"]
    else:
        argv += ["-s", sandbox]
        if sandbox == "workspace-write":
            argv += ["-c", "sandbox_workspace_write.network_access=true"]
            for root in writable:
                argv += ["--add-dir", str(root)]
        argv += ["-C", str(worktree)]
    argv += ["--skip-git-repo-check", "--json", "-o", str(last_message), "--output-schema", str(schema)]
    return argv


def sandbox_capabilities(sandbox: str, writable: list[Path], worktree: Path) -> dict:
    """What the configured sandbox actually protects, recorded per attempt instead of assumed."""
    if sandbox == "bypass":
        return {"mode": "bypass", "enforced": False, "writable": "everything the runner's user can write",
                "protects": []}
    return {"mode": sandbox, "enforced": True, "writable": [str(worktree), *map(str, writable)],
            "protects": ["run.json, events, receipts, and intent files in the run directory",
                         "Git config, hooks, and info/exclude in the common Git directory"]}


def claude_argv(*, prompt: str | None, model: str, effort: str, plugin_root: Path, run_dir: Path,
                session_id: str, resume: bool) -> list[str]:
    argv = [binary("claude")]
    if not resume:
        argv.append(prompt or "/factory:scope")
    argv += ["--model", model, "--effort", effort, "--plugin-dir", str(plugin_root), "--add-dir", str(run_dir)]
    argv += ["--resume", session_id] if resume else ["--session-id", session_id]
    return argv


def github_token(env: dict) -> str | None:
    """The host's gh token, read outside the sandbox.

    Codex's workspace-write sandbox blocks the macOS keychain where gh keeps its
    token, so gh inside an attempt fails with HTTP 401 unless the token arrives
    through the environment.
    """
    if env.get("GH_TOKEN") or env.get("GITHUB_TOKEN"):
        return None
    try:
        result = subprocess.run([binary("gh"), "auth", "token"], capture_output=True, text=True, timeout=30,
                                stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = result.stdout.strip()
    return token if result.returncode == 0 and token else None


def attempt_env(*, run_dir: Path, plan: str, stage: str, attempt: int, base: dict | None = None,
                forward_github_token: bool = False) -> dict:
    env = dict(os.environ if base is None else base)
    env.update({
        "FACTORY_RUN_DIR": str(run_dir),
        "FACTORY_PLAN": plan,
        "FACTORY_STAGE": stage,
        "FACTORY_ATTEMPT": str(attempt),
    })
    if forward_github_token:
        token = github_token(env)
        if token:
            env["GH_TOKEN"] = token
    return env


@dataclass
class CodexStream:
    thread_id: str | None = None
    tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    usage_events: int = 0
    child_agents_spawned: int = 0
    child_agents_failed: int = 0
    turns: int = 0
    failures: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unknown: int = 0
    truncated: bool = False


def usage_record(stream: CodexStream) -> dict:
    """Usage by category without double counting, or unknowns when the host reported none.

    Codex reports cached and cache-write input as parts of `input_tokens`, and reasoning
    output as part of `output_tokens`. Sub-agent usage does not appear in the parent's
    stream, so it is recorded as unknown rather than zero.
    """
    if not stream.usage_events:
        return {"reported": False, "input": None, "cached_input": None, "cache_write_input": None,
                "uncached_input": None, "output": None, "reasoning_output": None, "total": None,
                "child_agents": stream.child_agents_spawned, "child_usage": "unknown"}
    return {"reported": True, "input": stream.input_tokens, "cached_input": stream.cached_input_tokens,
            "cache_write_input": stream.cache_write_input_tokens,
            "uncached_input": max(0, stream.input_tokens - stream.cached_input_tokens),
            "output": stream.output_tokens, "reasoning_output": stream.reasoning_output_tokens,
            "total": stream.input_tokens + stream.output_tokens,
            "child_agents": stream.child_agents_spawned, "child_usage": "unknown"}


def combine_usage(records: list[dict]) -> dict:
    """A run's usage across attempts: categories sum only over attempts that reported them."""
    reported = [r for r in records if r and r.get("reported")]
    keys = ("input", "cached_input", "cache_write_input", "uncached_input", "output", "reasoning_output", "total")
    total = {key: sum(r[key] for r in reported) for key in keys}
    total.update({"reported_attempts": len(reported), "unreported_attempts": len(records) - len(reported),
                  "child_agents": sum(r.get("child_agents", 0) for r in records if r),
                  "child_usage": "unknown", "scope_usage": "unknown"})
    return total


KNOWN_TYPES = {
    "thread.started", "turn.started", "turn.completed", "turn.failed", "item.started", "item.updated",
    "item.completed", "error",
}


def parse_codex_stream(path: Path) -> CodexStream:
    """Summarize a `codex exec --json` stream. Never decides the stage outcome."""
    stream = CodexStream()
    if not path.exists():
        return stream
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for index, raw in enumerate(lines):
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                stream.truncated = True
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started":
            stream.thread_id = event.get("thread_id") or stream.thread_id
        elif kind == "turn.completed":
            usage = event.get("usage") or {}
            stream.turns += 1
            if usage:
                stream.usage_events += 1
            stream.input_tokens += int(usage.get("input_tokens") or 0)
            stream.output_tokens += int(usage.get("output_tokens") or 0)
            stream.cached_input_tokens += int(usage.get("cached_input_tokens") or 0)
            stream.cache_write_input_tokens += int(usage.get("cache_write_input_tokens") or 0)
            stream.reasoning_output_tokens += int(usage.get("reasoning_output_tokens") or 0)
        elif kind == "item.completed" and isinstance(event.get("item"), dict) \
                and event["item"].get("type") == "collab_tool_call" and event["item"].get("tool") == "spawn_agent":
            if event["item"].get("status") == "completed":
                stream.child_agents_spawned += 1
            else:
                stream.child_agents_failed += 1
        elif kind == "turn.failed":
            error = event.get("error") or {}
            stream.failures.append(str(error.get("message") if isinstance(error, dict) else error))
        elif kind == "error":
            stream.errors.append(str(event.get("message") or event))
        elif kind not in KNOWN_TYPES:
            stream.unknown += 1
    stream.tokens = stream.input_tokens + stream.output_tokens
    return stream


def render_event(event: dict) -> str | None:
    """One compact log line for a known Codex event; None for events shown only in --raw."""
    kind = event.get("type")
    if kind == "thread.started":
        return f"thread {event.get('thread_id')}"
    if kind == "turn.completed":
        usage = event.get("usage") or {}
        return f"tokens in {usage.get('input_tokens', 0)} / out {usage.get('output_tokens', 0)}"
    if kind == "turn.failed":
        error = event.get("error") or {}
        return f"model error: {error.get('message') if isinstance(error, dict) else error}"
    if kind == "error":
        return f"error: {event.get('message')}"
    item = event.get("item") if isinstance(event.get("item"), dict) else None
    if item is None:
        return None
    item_type = item.get("type")
    if item_type == "agent_message" and kind == "item.completed":
        return f"agent: {str(item.get('text') or '').strip()}"
    if item_type == "command_execution":
        if kind == "item.started":
            return f"$ {item.get('command')}"
        if kind == "item.completed":
            return f"  exit {item.get('exit_code')} ({item.get('status', 'completed')})"
    return None


def render_stream(path: Path, raw: bool = False) -> list[str]:
    if not path.exists():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw:
            out.append(line)
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            out.append("(truncated event)")
            continue
        rendered = render_event(event) if isinstance(event, dict) else None
        if rendered is not None:
            out.append(rendered)
    return out
