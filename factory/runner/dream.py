"""The dream loop: replay the foreman skill over history worlds, score it against hindsight labels, revise it.

A replay is one cold Codex turn per decision point, reading the point's digest snapshot and event under a
staged plugin tree; its decision is scored against the point's hindsight label by the action ladder below.
`factory dream` scores the incumbent skill, asks the dream skill for revisions, keeps the best on the
selection worlds, and deploys it only when it beats the incumbent on confirmation worlds it never saw.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from runner import config, hosts, provenance, records

# Cost of an action above the cheapest accepted one, from the foreman skill's "cheapest sufficient step".
LADDER = {"regate": 0, "publish": 0, "advance": 0, "wait": 1, "repair": 1, "launch": 2, "park": 3, "rescope": 3,
          "cancel": 3}
STEP_PENALTY = 0.15
GENERATED = provenance.GENERATED
NO_WORKTREE = ("You are the foreman of a factory run replayed for evaluation: the worktree is not available, so decide\n"
               "from the digest at {digest} and the event below alone. Then answer the event.")


# --- scoring ---------------------------------------------------------------------------------

def score_point(decision: dict | None, label: dict) -> float:
    """1 minus STEP_PENALTY per ladder step above the cheapest accepted action; 0 when not accepted.

    A malformed decision, or an override the label does not allow, scores 0.
    """
    if not decision or decision.get("action") not in label.get("accept", []):
        return 0.0
    if decision.get("override") and not label.get("allow_override"):
        return 0.0
    cheapest = min(LADDER[action] for action in label["accept"])
    return round(max(0.0, 1 - STEP_PENALTY * (LADDER[decision["action"]] - cheapest)), 6)


def score_world(points: list[dict]) -> float | None:
    """The mean over a world's scorable points; None when it has none, so the world is excluded, not zero."""
    scores = [p["score"] for p in points if p.get("scorable", True)]
    return round(sum(scores) / len(scores), 6) if scores else None


def score_policy(worlds: list[float | None]) -> float | None:
    """Equal weight per world: the mean over worlds that have a score."""
    scores = [s for s in worlds if s is not None]
    return round(sum(scores) / len(scores), 6) if scores else None


# --- staged plugin trees -------------------------------------------------------------------

def _plugin_builder():
    path = provenance.REPO_ROOT / "scripts" / "build_codex_plugin.py"
    spec = importlib.util.spec_from_file_location("build_codex_plugin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def built_skill(source_text: str) -> str:
    """The generated form of a foreman skill source: the same frontmatter strip the plugin build applies."""
    return _plugin_builder().codex_skill(source_text, "foreman")


def stage_plugin(skill_text: str, dest: Path, source_tree: Path | None = None) -> Path:
    """A full copy of the generated plugin tree with a candidate skill swapped in, so its links resolve."""
    dest = Path(dest)
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(source_tree or GENERATED, dest, ignore=shutil.ignore_patterns("__pycache__"))
    (dest / provenance.FOREMAN_SKILL).write_text(built_skill(skill_text), encoding="utf-8")
    return dest


# --- one replay ------------------------------------------------------------------------------

def cache_key(*, policy: str, world: str, point: str, digest: str, schema: str, model: str, effort: str,
              repeat: int) -> str:
    return hashlib.sha256(json.dumps([policy, world, point, digest, schema, model, effort, repeat]).encode()).hexdigest()


class Cache:
    """Replay answers under ~/.factory/dreams/cache/, shared across dreams. Only first passes are stored."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def get(self, key: str) -> dict | None:
        path = self.root / key[:2] / f"{key}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def put(self, key: str, value: dict) -> None:
        path = self.root / key[:2] / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".partial")
        tmp.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)


def missing_event_files(point_dir: Path) -> list[str]:
    """Files the event message names that the point directory does not hold."""
    event = (Path(point_dir) / "event.md").read_text(encoding="utf-8", errors="replace")
    missing = []
    for line in event.splitlines():
        if not line.startswith("Files: "):
            continue
        for entry in line[len("Files: "):].split("; "):
            name, _, path = entry.strip().partition(" ")
            if path and not (Path(point_dir) / path).is_file():
                missing.append(f"{name} {path}")
    return missing


def replay_prompt(point_dir: Path, plugin_tree: Path) -> str:
    point_dir = Path(point_dir)
    lines = [f"Follow the skill at {Path(plugin_tree) / provenance.FOREMAN_SKILL}.",
             "Read it and the references it links from that directory; do not use an installed factory plugin.", "",
             NO_WORKTREE.format(digest=point_dir / "digest.json"),
             "The digest is a snapshot taken when the decision was due and may predate the current digest format; "
             "its paths are relative to the current directory, and placeholders such as <run_dir> stand for files "
             "that are not available."]
    if not (point_dir / "plan").is_dir():
        lines.append("The plan files are not available for this point: decide from the digest, the event, and the "
                     "files beside them.")
    missing = missing_event_files(point_dir)
    if missing:
        lines.append("The event names files that are not available here: " + "; ".join(missing) + ".")
    lines.append("")
    return "\n".join(lines) + (point_dir / "event.md").read_text(encoding="utf-8")


def parse_decision(path: Path) -> tuple[dict | None, str | None]:
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None, "the replay wrote no final message"
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start:end + 1] if start >= 0 and end > start else text)
        return records.validate_decision(data), None
    except (ValueError, records.RecordError) as error:
        return None, f"{type(error).__name__}: {error}"


@dataclass
class Replay:
    decision: dict | None
    problem: str | None
    fresh: bool
    key: str


def replay(point_dir: Path, plugin_tree: Path, cfg: config.Config, cache: Cache | None, *, repeat: int = 1,
           world: str = "", work: Path | None = None) -> Replay:
    """One cold foreman turn on a recorded point under `plugin_tree`'s skill; first passes come from the cache.

    Repeats 2..K are always fresh calls: a repeat is a draw, not a lookup.
    """
    point_dir = Path(point_dir)
    policy = provenance.tree_policy(plugin_tree)["sha256"]
    digest = hashlib.sha256((point_dir / "digest.json").read_bytes()).hexdigest()
    key = cache_key(policy=policy, world=world, point=point_dir.name, digest=digest, schema=records.DECISION_SCHEMA,
                    model=cfg.foreman_model, effort=cfg.foreman_effort, repeat=repeat)
    if cache is not None and repeat == 1:
        hit = cache.get(key)
        if hit is not None:
            return Replay(hit.get("decision"), hit.get("problem"), False, key)
    own = work is None
    work = Path(tempfile.mkdtemp(prefix="factory-replay-")) if own else Path(work)
    work.mkdir(parents=True, exist_ok=True)
    try:
        schema = work / "decision.schema.json"
        schema.write_text(json.dumps(records.decision_json_schema(), indent=2) + "\n", encoding="utf-8")
        last = work / "last-message.md"
        last.unlink(missing_ok=True)
        argv = hosts.codex_foreman_argv(prompt=replay_prompt(point_dir, plugin_tree), model=cfg.foreman_model,
                                        effort=cfg.foreman_effort, sandbox="read-only", worktree=point_dir,
                                        writable=[], last_message=last, schema=schema)
        env = {**os.environ, "FACTORY_ROLE": "replay", "FACTORY_REPLAY_POINT": point_dir.name}
        try:
            with open(work / "stdout.jsonl", "wb") as stdout, open(work / "stderr.log", "wb") as stderr:
                code = subprocess.run(argv, cwd=point_dir, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                                      env=env, timeout=cfg.foreman_turn_timeout_s).returncode
        except (OSError, subprocess.TimeoutExpired) as error:
            return Replay(None, f"replay did not finish: {error}", True, key)
        if code != 0:
            return Replay(None, f"replay exited {code}", True, key)
        decision, problem = parse_decision(last)
        if cache is not None and repeat == 1:
            cache.put(key, {"decision": decision, "problem": problem})
        return Replay(decision, problem, True, key)
    finally:
        if own:
            shutil.rmtree(work, ignore_errors=True)
