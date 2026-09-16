"""The fixed four-stage pipeline: hosts, models, efforts, timeouts, prompts, and gates.

Model, effort, and timeout defaults live here rather than in user config so the
tested pipeline stays reproducible.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from runner import FACTORY_ROOT, conditions, gates
from runner.model import Run, atomic_write

GENERATED_SKILLS = FACTORY_ROOT.parent / "plugins" / "factory" / "skills"


@dataclass(frozen=True)
class Stage:
    name: str
    host: str
    model: str
    effort: str
    timeout_s: int | None
    slot_limited: bool
    build_prompt: Callable[[Run, int], str]
    gate: Callable[[gates.GateContext], gates.GateResult]

    @property
    def interactive(self) -> bool:
        return self.host == "claude"


def gate_reserve_s(timeout_s: int | None) -> float:
    """Part of a stage's one attempt deadline kept for its gate after the agent's own allowance ends."""
    return 0.0 if not timeout_s else min(30 * 60, timeout_s * 0.25)


def direct_skill_path() -> bool:
    """Explicit fallback for hosts where `$factory:{stage}` does not resolve."""
    return os.environ.get("FACTORY_DIRECT_SKILL_PATH", "") not in ("", "0", "false")


def headless_prompt(stage: str) -> Callable[[Run, int], str]:
    def build(run: Run, n: int) -> str:
        lines = []
        if direct_skill_path():
            lines += [f"Follow the skill at {GENERATED_SKILLS / stage / 'SKILL.md'}.", ""]
        lines += [
            f"$factory:{stage}",
            "",
            f"Factory run {run.id}. Read .dev/factory-run.json first. It names the plan",
            "directory, report directory, evidence directory, and scratch directory.",
            "No human is available in this session. This is attempt "
            f"{n} of the {stage} stage.",
        ]
        finished = [a for a in run.stage_attempts(stage) if a["n"] < n and a.get("outcome")]
        prior = finished[-1] if finished else None
        extra = []
        if prior is not None:
            extra.append(f"Attempt {prior['n']} ended {prior['outcome']}: {prior['reason']}.")
        note = run.data["human"].get("note")
        if note:
            extra.append(f"Operator note: {note}.")
        for condition in conditions.open_for(run, stage):
            extra.append(f"Open condition {condition['id']} {condition['code']}: {condition['summary']}. Re-check it; "
                         "report it resolved with evidence, or report it again.")
        if extra:
            lines += [""] + extra
        lines += [
            "",
            "Start from the artifacts on disk. Do not redo finished work.",
            f"Write .dev/{run.plan}/{stage}-result.json as your last action.",
        ]
        return "\n".join(lines) + "\n"
    return build


def scope_prompt(run: Run, n: int) -> str:
    """Slash-command prompt for interactive scope; long or external requests stay on disk."""
    if run.data.get("source", {}).get("kind", "text") == "text" or not run.data.get("request_file"):
        return f"/factory:scope {run.data['request']}"
    return f"/factory:scope {run.data['request']} (full request: {run.data['request_file']})"


PIPELINE: dict[str, Stage] = {
    "scope": Stage("scope", "claude", "fable", "medium", None, False, scope_prompt, gates.scope_gate),
    "scope-review": Stage("scope-review", "codex", "openai.gpt-5.6-sol", "low", 45 * 60, True,
                          headless_prompt("scope-review"), gates.scope_review_gate),
    "build": Stage("build", "codex", "openai.gpt-5.6-luna", "medium", 3 * 60 * 60, True,
                   headless_prompt("build"), gates.build_gate),
    "ship": Stage("ship", "codex", "openai.gpt-5.6-luna", "high", 3 * 60 * 60, True,
                  headless_prompt("ship"), gates.ship_gate),
}


def run_context(run: Run, stage: str, n: int, *, interactive: bool) -> dict:
    previous = None
    prior = [a for a in run.stage_attempts(stage) if a["n"] < n and a.get("outcome")]
    if prior:
        previous = {"outcome": prior[-1]["outcome"], "reason": prior[-1]["reason"]}
    return {
        "schema": 2,
        "run_id": run.id,
        "run_dir": str(run.dir),
        "plan": run.plan,
        "stage": stage,
        "attempt": n,
        "previous": previous,
        "operator_note": run.data["human"].get("note"),
        "conditions": conditions.context(run, stage),
        "intent_file": str(run.dir / "intent" / "approved.json") if run.data.get("intent") else None,
        "scenarios_file": str(run.dir / "intent" / "scenarios.json") if run.data.get("intent") else None,
        "request_file": run.data.get("request_file"),
        "source": run.data.get("source") or {"kind": "text"},
        "report_dir": str(run.report_dir),
        "evidence_dir": str(run.evidence_dir),
        "scratch_dir": str(run.scratch_dir(stage, n)),
        "branch": run.data["branch"],
        "base": run.data["base"],
        "base_sha": run.data["base_sha"],
        "interactive": interactive,
    }


def write_run_context(run: Run, stage: str, n: int, *, interactive: bool) -> Path:
    run.scratch_dir(stage, n).mkdir(parents=True, exist_ok=True)
    path = run.worktree / ".dev" / "factory-run.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(run_context(run, stage, n, interactive=interactive), indent=2) + "\n")
    return path


def gate_context(run: Run, stage: str, n: int, **overrides: object) -> gates.GateContext:
    attempt = next((a for a in run.stage_attempts(stage) if a["n"] == n), {})
    baseline = dict(attempt.get("baseline") or {})
    build_start = run.stage_record("build").get("start_sha")
    if build_start:
        baseline.setdefault("build_start_sha", build_start)
    values = dict(
        worktree=run.worktree,
        plan=run.plan,
        stage=stage,
        report_dir=run.report_dir,
        attempt_dir=run.attempt_dir(stage, n),
        attempt=n,
        remote=run.data["remote"],
        branch=run.data["branch"],
        base_sha=run.data["base_sha"],
        base=run.data.get("base"),
        baseline=baseline,
        run_dir=run.dir,
        cancel_file=run.dir / "cancel",
        intent_sha256=(run.data.get("intent") or {}).get("sha256"),
        require_intent=stage != "scope",
    )
    values.update(overrides)
    return gates.GateContext(**values)
