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
BROWSER_MODES = ("auto", "off")
FOREMAN_MODES = ("off", "shadow", "codex")
EFFORTS = ("low", "medium", "high", "xhigh")

DEFAULTS = {
    "schema": SCHEMA,
    "max_concurrent_stages": 4,
    "max_retries": 5,
    "notify": True,
    "stop_on_repeated_reason": False,
    "stage_poll_seconds": 10,
    "heartbeat_seconds": 30,
    "max_child_agents": 6,
    "max_agent_depth": 1,
    "max_heavy_commands": 2,
    "port_range": [20000, 29999],
    # "auto" hosts one headless Chrome per build and ship attempt and per e2e gate, reached over CDP; see runner/browser.py.
    "browser": "auto",
    # The foreman: one long-lived Codex session per run that decides what the worker does next.
    # "codex" applies its decisions; "shadow" consults it and records the decision without applying it;
    # "off" keeps the fixed pipeline and model.decide(). Caps below bind whoever decides.
    "foreman": "codex",
    "foreman_model": "openai.gpt-5.6-luna",
    "foreman_effort": "medium",
    "foreman_turn_timeout_s": 900,
    "foreman_turns_per_event": 2,
    "foreman_context_tokens": 400000,
    "max_stage_attempts": 6,
    "max_repairs_per_stage": 3,
    "max_wait_minutes": 120,
    "max_run_hours": 24,
    "max_overrides_per_run": 2,
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
    max_retries: int = 5
    notify: bool = True
    stop_on_repeated_reason: bool = False
    stage_poll_seconds: float = 10
    heartbeat_seconds: float = 30
    max_child_agents: int = 6
    max_agent_depth: int = 1
    max_heavy_commands: int = 2
    port_range: tuple = (20000, 29999)
    browser: str = "auto"
    foreman: str = "codex"
    foreman_model: str = "openai.gpt-5.6-luna"
    foreman_effort: str = "medium"
    foreman_turn_timeout_s: float = 900
    foreman_turns_per_event: int = 2
    foreman_context_tokens: int = 400000
    max_stage_attempts: int = 6
    max_repairs_per_stage: int = 3
    max_wait_minutes: float = 120
    max_run_hours: float = 24
    max_overrides_per_run: int = 2
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
    _positive_number(merged, "max_retries", integer=True)
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
    if merged["browser"] not in BROWSER_MODES:
        raise ConfigError(f"config.json: 'browser' must be one of {', '.join(BROWSER_MODES)}, got {merged['browser']!r}")
    if merged["foreman"] not in FOREMAN_MODES:
        raise ConfigError(f"config.json: 'foreman' must be one of {', '.join(FOREMAN_MODES)}, got {merged['foreman']!r}")
    if not isinstance(merged["foreman_model"], str) or not merged["foreman_model"].strip():
        raise ConfigError(f"config.json: 'foreman_model' must be a model name, got {merged['foreman_model']!r}")
    if merged["foreman_effort"] not in EFFORTS:
        raise ConfigError(f"config.json: 'foreman_effort' must be one of {', '.join(EFFORTS)}, got {merged['foreman_effort']!r}")
    for key in ("foreman_turns_per_event", "foreman_context_tokens", "max_stage_attempts", "max_repairs_per_stage"):
        _positive_number(merged, key, integer=True)
    for key in ("foreman_turn_timeout_s", "max_wait_minutes", "max_run_hours"):
        _positive_number(merged, key, integer=False)
    overrides = merged["max_overrides_per_run"]
    if isinstance(overrides, bool) or not isinstance(overrides, int) or overrides < 0:
        raise ConfigError(f"config.json: 'max_overrides_per_run' must be a non-negative integer, got {overrides!r}")
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
        max_retries=merged["max_retries"],
        notify=merged["notify"],
        stop_on_repeated_reason=merged["stop_on_repeated_reason"],
        stage_poll_seconds=merged["stage_poll_seconds"],
        heartbeat_seconds=merged["heartbeat_seconds"],
        max_child_agents=merged["max_child_agents"],
        max_agent_depth=merged["max_agent_depth"],
        max_heavy_commands=merged["max_heavy_commands"],
        port_range=tuple(merged["port_range"]),
        browser=merged["browser"],
        foreman=merged["foreman"],
        foreman_model=merged["foreman_model"],
        foreman_effort=merged["foreman_effort"],
        foreman_turn_timeout_s=merged["foreman_turn_timeout_s"],
        foreman_turns_per_event=merged["foreman_turns_per_event"],
        foreman_context_tokens=merged["foreman_context_tokens"],
        max_stage_attempts=merged["max_stage_attempts"],
        max_repairs_per_stage=merged["max_repairs_per_stage"],
        max_wait_minutes=merged["max_wait_minutes"],
        max_run_hours=merged["max_run_hours"],
        max_overrides_per_run=merged["max_overrides_per_run"],
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
