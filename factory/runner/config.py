"""Runtime root and optional user configuration.

`~/.factory/config.json` is optional; missing keys use defaults. Invalid JSON or
invalid known values raise ConfigError, and the runner never resets the file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = 1
SANDBOX_MODES = ("workspace-write", "bypass")

DEFAULTS = {
    "schema": SCHEMA,
    "max_concurrent_stages": 4,
    "notify": True,
    # One real first-try run used 31.7M tokens (ship alone 20.9M); see factory/README.md.
    "max_tokens_per_run": 60_000_000,
    "stop_on_repeated_reason": False,
    "stage_poll_seconds": 10,
    "heartbeat_seconds": 30,
    "max_child_agents": 6,
    "max_agent_depth": 1,
    "max_heavy_commands": 2,
    "port_range": [20000, 29999],
    "repos": {},
}


class ConfigError(Exception):
    pass


def factory_home() -> Path:
    return Path(os.environ.get("FACTORY_HOME") or Path.home() / ".factory").expanduser().resolve()


def binary(name: str) -> str:
    """Host binary, honoring FACTORY_{NAME}_BIN test overrides."""
    return os.environ.get(f"FACTORY_{name.upper()}_BIN") or name


def normalize_repo(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


@dataclass
class Config:
    max_concurrent_stages: int = 4
    notify: bool = True
    max_tokens_per_run: int = 60_000_000
    stop_on_repeated_reason: bool = False
    stage_poll_seconds: float = 10
    heartbeat_seconds: float = 30
    max_child_agents: int = 6
    max_agent_depth: int = 1
    max_heavy_commands: int = 2
    port_range: tuple = (20000, 29999)
    repos: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def sandbox_for(self, repo: str | Path) -> str:
        entry = self.repos.get(normalize_repo(repo), {})
        return entry.get("codex_sandbox", "workspace-write")


def _positive_number(data: dict, key: str, integer: bool) -> None:
    value = data[key]
    kinds = (int,) if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kinds) or value <= 0:
        raise ConfigError(f"config.json: '{key}' must be a positive {'integer' if integer else 'number'}, got {value!r}")


def parse(data: object) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config.json: top level must be a JSON object")
    merged = {**DEFAULTS, **data}
    if merged["schema"] != SCHEMA:
        raise ConfigError(f"config.json: schema {merged['schema']!r} is not supported (expected {SCHEMA})")
    _positive_number(merged, "max_concurrent_stages", integer=True)
    _positive_number(merged, "max_tokens_per_run", integer=True)
    _positive_number(merged, "stage_poll_seconds", integer=False)
    _positive_number(merged, "heartbeat_seconds", integer=False)
    for key in ("max_child_agents", "max_agent_depth", "max_heavy_commands"):
        _positive_number(merged, key, integer=True)
    ports = merged["port_range"]
    if (not isinstance(ports, list) or len(ports) != 2 or not all(isinstance(p, int) and not isinstance(p, bool)
                                                                   for p in ports) or not 1024 <= ports[0] < ports[1] <= 65535):
        raise ConfigError(f"config.json: 'port_range' must be [low, high] with 1024 <= low < high <= 65535, got {ports!r}")
    for key in ("notify", "stop_on_repeated_reason"):
        if not isinstance(merged[key], bool):
            raise ConfigError(f"config.json: '{key}' must be true or false, got {merged[key]!r}")
    if not isinstance(merged["repos"], dict):
        raise ConfigError("config.json: 'repos' must be an object keyed by absolute repository path")
    repos: dict = {}
    for key, entry in merged["repos"].items():
        if not os.path.isabs(os.path.expanduser(key)):
            raise ConfigError(f"config.json: repository key {key!r} must be an absolute path")
        if not isinstance(entry, dict):
            raise ConfigError(f"config.json: repos[{key!r}] must be an object")
        sandbox = entry.get("codex_sandbox", "workspace-write")
        if sandbox not in SANDBOX_MODES:
            raise ConfigError(
                f"config.json: repos[{key!r}].codex_sandbox must be one of {', '.join(SANDBOX_MODES)}, got {sandbox!r}"
            )
        repos[normalize_repo(key)] = entry
    return Config(
        max_concurrent_stages=merged["max_concurrent_stages"],
        notify=merged["notify"],
        max_tokens_per_run=merged["max_tokens_per_run"],
        stop_on_repeated_reason=merged["stop_on_repeated_reason"],
        stage_poll_seconds=merged["stage_poll_seconds"],
        heartbeat_seconds=merged["heartbeat_seconds"],
        max_child_agents=merged["max_child_agents"],
        max_agent_depth=merged["max_agent_depth"],
        max_heavy_commands=merged["max_heavy_commands"],
        port_range=tuple(merged["port_range"]),
        repos=repos,
        raw=data,
    )


def load(home: Path | None = None) -> Config:
    path = (home or factory_home()) / "config.json"
    if not path.exists():
        return parse({})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError(f"{path}: invalid JSON: {error}") from error
    return parse(data)


def ensure_home(home: Path | None = None) -> Path:
    root = home or factory_home()
    for sub in ("runs", "slots", "archive"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root
