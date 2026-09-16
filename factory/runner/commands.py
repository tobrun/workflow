"""Repository command records resolved into guarded executions.

A command record (see `validate_command` in factory/scripts/factory_records.py) is an
argv or one repository script, with a working directory, declared environment inputs,
a timeout cap, and an execution boundary. This module turns one into the argv, the
environment, and the sandbox wrapper the executor runs; it never builds a shell line.

Boundaries:
  host             the runner's own privileges: whole filesystem, network.
  workspace-write  writes limited to the worktree, the execution directory, and the
                   system temporary directories; reads and network stay open. This
                   mirrors Codex's workspace-write sandbox with network access, the
                   configuration the factory launches agents with. macOS enforces it
                   with sandbox-exec, Linux with bubblewrap; a host without either
                   fails the command instead of silently running it unconfined.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from runner import records

BASELINE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM", "TZ")
DEFAULT_TIMEOUT_S = 20 * 60


class CommandError(Exception):
    """A command record that cannot run as declared; `repair` names the fix."""

    def __init__(self, message: str, repair: str | None = None):
        super().__init__(message)
        self.repair = repair


def argv(command: dict, worktree: Path, placeholders: dict[str, list[str]] | None = None) -> list[str]:
    """The argv a record declares, with list placeholders such as {ids} expanded in place."""
    placeholders = placeholders or {}
    if "script" in command:
        script = resolve_inside(worktree, command["script"], "script")
        if not script.is_file():
            raise CommandError(f"command {command['id']}: script {command['script']} does not exist",
                               f"commit {command['script']} or fix .factory/contract.json")
        parts = [str(script), *command.get("args", [])]
        if "shell" in command or not os.access(script, os.X_OK):
            parts = [command.get("shell", "sh"), *parts]
    else:
        parts = list(command["run"])
    expanded: list[str] = []
    for part in parts:
        token = part[1:-1] if part.startswith("{") and part.endswith("}") else None
        if token is not None and token in placeholders:
            expanded.extend(placeholders[token])
        else:
            for name, values in placeholders.items():
                part = part.replace("{" + name + "}", " ".join(values))
            expanded.append(part)
    return expanded


def resolve_inside(worktree: Path, relative: str, what: str) -> Path:
    root = Path(os.path.realpath(worktree))
    target = Path(os.path.realpath(root / relative))
    try:
        target.relative_to(root)
    except ValueError as error:
        raise CommandError(f"{what} {relative} resolves outside the worktree") from error
    return target


def missing_environment(command: dict, contract: dict, base: dict) -> list[str]:
    required = list(contract.get("environment", {}).get("required", [])) + list(command.get("env", []))
    return sorted({name for name in required if not base.get(name)})


def environment(command: dict, contract: dict, base: dict) -> dict:
    """Baseline variables plus exactly the declared inputs; never the whole runner environment."""
    missing = missing_environment(command, contract, base)
    if missing:
        raise CommandError(f"command {command['id']} needs environment variable(s) {', '.join(missing)}",
                           f"export {' '.join(missing)} before `factory resume`, or declare them optional in "
                           ".factory/contract.json")
    names = set(BASELINE_ENV) | set(contract.get("environment", {}).get("required", []))
    names |= set(contract.get("environment", {}).get("passthrough", [])) | set(command.get("env", []))
    env = {name: base[name] for name in names if name in base}
    env.update(command.get("set", {}))
    return env


def timeout_s(command: dict) -> int:
    return int(command.get("timeout_s", DEFAULT_TIMEOUT_S))


def boundary(command: dict, contract: dict) -> str:
    return command.get("boundary") or contract.get("boundary") or "host"


def temp_roots() -> list[Path]:
    roots = {Path(os.path.realpath(tempfile.gettempdir())), Path(os.path.realpath("/tmp"))}
    return sorted(roots)


def sandbox_wrap(parts: list[str], mode: str, *, writable: list[Path]) -> list[str]:
    """Wrap an argv in the boundary's enforcement; host mode returns it unchanged."""
    if mode == "host":
        return parts
    if mode != "workspace-write":
        raise CommandError(f"unknown execution boundary {mode!r}")
    paths = sorted({Path(os.path.realpath(p)) for p in [*writable, *temp_roots()]})
    if sys.platform == "darwin":
        tool = shutil.which("sandbox-exec")
        if not tool:
            raise CommandError("the workspace-write boundary needs sandbox-exec, which this macOS host lacks",
                               "set \"boundary\": \"host\" in .factory/contract.json")
        allowed = " ".join(f'(subpath "{_escape(str(p))}")' for p in paths)
        profile = ("(version 1)\n(allow default)\n(deny file-write*)\n"
                   f"(allow file-write* {allowed} (subpath \"/dev\"))\n")
        return [tool, "-p", profile, *parts]
    tool = shutil.which("bwrap")
    if not tool:
        raise CommandError("the workspace-write boundary needs bubblewrap (bwrap) on this host",
                           "install bubblewrap or set \"boundary\": \"host\" in .factory/contract.json")
    wrapped = [tool, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for path in paths:
        wrapped += ["--bind", str(path), str(path)]
    return [*wrapped, "--", *parts]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def load_contract(worktree: Path) -> dict:
    """The repository contract at .factory/contract.json; raises records.RecordError."""
    return records.load_contract(Path(worktree) / records.CONTRACT_PATH)
