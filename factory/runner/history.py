"""Replay worlds: finished factory runs turned into decision points the foreman policy can be scored on.

`factory history build` reads run directories under `~/.factory/runs/` and `~/.factory/archive/` and writes
one world per finished run under `~/.factory/history/{run-id}/`: `world.json` (`factory.world/1`) and a
`points/{stage}-{n}/` directory per headless attempt that reached a decision, holding the digest the foreman
saw (or one rebuilt from `run.json`), the event message, the files the event names, and the plan the
attempt started from, with every absolute path rewritten. History never writes inside a run directory.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import final

from runner import config, foreman, records
from runner.model import (
    HEADLESS,
    PARKED,
    Run,
    SchemaError,
    atomic_write,
    hard_stop,
    utc_now,
)

DIR = "history"
LOCK = "history.lock"
# Bumped when the world layout or the rewriting rules change, so an unchanged run is rebuilt once.
BUILDER = "factory.history-builder/1"
EVENT_FILES = {"gate": "gate.json", "last_message": "last-message.md", "stderr": "stderr.log"}
TEXT_LIMIT = 8 * 1024 * 1024


class HistoryLocked(Exception):
    def __init__(self, path: Path):
        super().__init__(f"another factory history or dream command holds {path}; try again when it finishes")
        self.path = path


@final
class HistoryLock:
    """Exclusive non-blocking flock on ~/.factory/history.lock, released on process death.

    `factory history build`, `factory history label`, and `factory dream` all take it, so no world is
    rewritten and no label regenerated while a dream reads them, and two dreams never race the cache.
    """

    def __init__(self, home: Path):
        self.path = Path(home) / LOCK
        self.fd = -1

    def __enter__(self) -> HistoryLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise HistoryLocked(self.path) from None
        self.fd = fd
        return self

    def __exit__(self, *_exc_info: object) -> None:
        if self.fd >= 0:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = -1


def root(home: Path) -> Path:
    return Path(home) / DIR


# --- a run as it stood at one decision ----------------------------------------------------

def as_of(run: Run, stage: str, n: int) -> Run:
    """The run as it stood right after `stage` attempt `n` closed and before its decision."""
    attempts = run.data["attempts"]
    index = next(i for i, a in enumerate(attempts) if a["stage"] == stage and a["n"] == n)
    kept = attempts[: index + 1]
    data = json.loads(json.dumps(run.data))
    data["attempts"] = kept
    data["status"] = "running"
    data["stage"] = stage
    seen = {(a["stage"], a["n"]) for a in kept}
    data["conditions"] = [c for c in data.get("conditions") or [] if (c.get("stage"), c.get("attempt")) in seen]
    for key in ("decisions", "overrides", "foreman", "guidance", "next_action", "next_launch", "wait", "last_wait"):
        data.pop(key, None)
    for attempt in data["attempts"]:
        attempt.pop("foreman", None)
        attempt.pop("transition", None)
    return Run(run.dir, data)


def redact(text: str, run: Run) -> str:
    """Repository identity out of a committed eval case: paths, the run id, and remote URLs become placeholders."""
    replacements = [
        (str(run.worktree), "<worktree>"),
        (str(run.dir), "<run_dir>"),
        (str(run.data.get("repo") or ""), "<repo>"),
        (run.id, "<run_id>"),
        (str(Path.home()), "<home>"),
    ]
    for old, new in replacements:
        if old:
            text = text.replace(old, new)
    text = re.sub(r"https://github\.com/[^/\s\"]+/[^/\s\"]+", "https://github.com/example/project", text)
    text = re.sub(r"https://[a-z0-9.-]+\.atlassian\.net/browse/[A-Z0-9-]+", "https://example.atlassian.net/browse/KEY-1", text)
    return text


# --- repository identity --------------------------------------------------------------------

def repo_key(remote: str) -> str:
    """One key per repository: the ssh and https forms of a remote are the same key; a path becomes a hashed name.

    A path key names the directory and a short hash of the full path, so a world never carries a home path.
    """
    value = (remote or "").strip()
    match = re.match(r"^(?:[\w.+-]+@)?([\w.-]+\.[a-z]{2,}):(?!//)(.+)$", value, re.IGNORECASE) \
        or re.match(r"^(?:https?|ssh|git)://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", value, re.IGNORECASE)
    if match:
        path = re.sub(r"\.git$", "", match.group(2).strip("/"))
        return f"{match.group(1).lower()}/{path.lower()}"
    normalized = config.normalize_repo(value) if value else ""
    name = Path(normalized).name or "repo"
    return f"path:{re.sub(r'.git$', '', name)}-{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:10]}"


def repository(run: Run) -> str:
    """The run's repository key: its remote's URL when the checkout still answers, else its path."""
    repo = str(run.data.get("repo") or "")
    remote = run.data.get("remote") or "origin"
    try:
        result = subprocess.run(["git", "-C", repo, "remote", "get-url", remote], capture_output=True, text=True,
                                timeout=30, stdin=subprocess.DEVNULL, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return repo_key(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return repo_key(repo)


# --- path rewriting -------------------------------------------------------------------------

class Rewriter:
    """Absolute paths out of copied content: copied files become point-relative, everything else a placeholder.

    The run id stays readable only where a record's format carries it (see `keep_run_id`); elsewhere it is
    replaced, since operator actions and notes quote it freely.
    """

    def __init__(self, run: Run, home: Path, copied: dict[str, str], plan_copied: bool, recorded_dirs: list[str]):
        run_dirs = {d for d in (str(run.dir), os.path.realpath(run.dir), str(Path(home) / "runs" / run.id),
                                str(Path(home) / "archive" / run.id), *recorded_dirs) if d}
        pairs = list(copied.items()) + [pair for base in run_dirs for pair in _aliases(base, run_dirs, copied)]
        prefixes = [pair for base in run_dirs for pair in _run_prefixes(base, run.plan, plan_copied)]
        prefixes += _outside_prefixes(run)
        self.pairs = sorted(set(pairs), key=lambda p: -len(p[0]))
        self.prefixes = sorted(set(prefixes), key=lambda p: -len(p[0]))
        self.run_id = run.id

    def text(self, value: str) -> str:
        for old, new in self.pairs:
            value = value.replace(old, new)
        for old, new in self.prefixes:
            value = value.replace(old, new)
        value = re.sub(r"(?<![\w<])/(?:Users|home)/[^\s\"'`]*", "<home>", value)
        return value.replace(self.run_id, "<run_id>")

    def value(self, value: object) -> object:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {self.text(key) if isinstance(key, str) else key: self.value(item) for key, item in value.items()}
        return value

    def file(self, source: Path, dest: Path, *, keep_run_id: tuple[str, ...] = ()) -> None:
        """Copy one file with its content rewritten; JSON keeps the run id in the fields `keep_run_id` names."""
        text = source.read_bytes()[-TEXT_LIMIT:].decode("utf-8", errors="replace")
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = _json_or_none(text) if source.suffix == ".json" else None
        if data is None:
            dest.write_text(self.text(text), encoding="utf-8")
            return
        rewritten = self.value(data)
        if isinstance(data, dict) and isinstance(rewritten, dict):
            rewritten.update({key: self.run_id for key in keep_run_id if data.get(key) == self.run_id})
        dest.write_text(json.dumps(rewritten, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _aliases(base: str, run_dirs: set[str], copied: dict[str, str]) -> list[tuple[str, str]]:
    """Each copied file under `base`, another alias of the run directory (archive, a symlinked temp dir)."""
    return [(base + source[len(other):], target) for source, target in copied.items() for other in run_dirs
            if source.startswith(other + "/")]


def _run_prefixes(base: str, plan: str, plan_copied: bool) -> list[tuple[str, str]]:
    return [(f"{base}/worktree/.dev/{plan}", "plan" if plan_copied else "<plan_dir>"),
            (f"{base}/worktree", "<worktree>"), (base, "<run_dir>")]


def _outside_prefixes(run: Run) -> list[tuple[str, str]]:
    """The repository checkout and the home directory, each also by its resolved path."""
    repo = str(run.data.get("repo") or "")
    home_dir = str(Path.home())
    return [(value, "<repo>") for value in {repo, os.path.realpath(repo) if repo else ""} if value] \
        + [(home_dir, "<home>"), (os.path.realpath(home_dir), "<home>")]


def _json_or_none(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


# --- building one world ---------------------------------------------------------------------

@dataclass
class BuildResult:
    run_id: str
    status: str  # built, unchanged, skipped
    world: Path | None = None
    notes: list[str] = field(default_factory=list)


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _event_from_prompt(prompt: str, run_id: str) -> str | None:
    """The event message inside a turn prompt: a cold prompt opens with the skill line and the intro first."""
    marker = f"Factory run {run_id}: event "
    index = prompt.find(marker)
    return prompt[index:] if index >= 0 else None


def _runtime(run: Run) -> dict:
    for attempt in reversed(run.data.get("attempts") or []):
        data = _read_json(run.attempt_dir(attempt.get("stage", ""), attempt.get("n", 0)) / "runtime.json")
        if data:
            return data
    return {}


def _auto_entry(attempt: dict) -> dict:
    """A point no foreman turn decided: the runner's own transition, when it recorded one."""
    transition = attempt.get("transition")
    if not transition:
        return {}
    return {"decision_source": "auto", "recorded_decision": {"action": transition.get("action"),
                                                             "vocabulary": "transition"}}


def _turn_entry(record: dict, summary: dict) -> dict:
    """The world entry fields a foreman turn record gives: its number, its origin, and who decided."""
    entry: dict = {"turn": int(record.get("turn") or summary.get("turn") or 0),
                   "origin": "cold" if record.get("cold") else "warm"}
    decision = record.get("decision")
    if decision and record.get("source") == "foreman":
        entry["decision_source"] = "foreman"
        entry["recorded_decision"] = {"action": decision.get("action"), "vocabulary": "decision",
                                      "override": bool(decision.get("override"))}
    else:
        entry["decision_source"] = "fallback"
    return entry


def _turn_event_text(turn_dir: Path, run_id: str) -> str | None:
    prompt = turn_dir / "prompt.txt"
    return _event_from_prompt(prompt.read_text(encoding="utf-8", errors="replace"), run_id) if prompt.is_file() else None


def _read_turn(run: Run, attempt: dict) -> tuple[dict, dict | None, bytes | None, str | None]:
    """The foreman turn that decided the attempt: its entry fields, event, digest bytes, and event message."""
    summary = attempt.get("foreman") or {}
    turn_dir = run.dir / summary["dir"] if summary.get("dir") else None
    record = _read_json(turn_dir / "decision.json") if turn_dir else None
    if turn_dir is None or record is None:
        return _auto_entry(attempt), None, None, None
    event = record.get("event") if isinstance(record.get("event"), dict) else None
    digest = turn_dir / "digest.json"
    return (_turn_entry(record, summary), event, digest.read_bytes() if digest.is_file() else None,
            _turn_event_text(turn_dir, run.id))


def _event(snapshot: Run, stage: str, cfg: config.Config, event: dict | None,
           event_text: str | None) -> tuple[dict, str]:
    """The event the foreman saw, rebuilt from the attempt wherever the turn record does not carry it."""
    if event is not None and event_text is not None:
        return event, event_text
    from runner import gates
    last = snapshot.data["attempts"][-1]
    outcome = gates.Outcome(last["outcome"], last.get("reason"), bool(last.get("retryable")),
                            last.get("source") or "runner", warning=last.get("warning"), code=last.get("code"),
                            conditions=list(last.get("conditions") or []))
    event = event or foreman.attempt_event(snapshot, stage, last, outcome)
    return event, event_text or foreman.event_message(snapshot, cfg, event)


def _judgement(run: Run, attempt: dict, event: dict) -> dict:
    """Whether the point is scorable, and the event it replays."""
    stop = hard_stop(run, attempt)
    entry: dict = {"scorable": stop is None}
    if stop:
        entry["unscorable_reason"] = f"hard stop {stop}: the runner parks whatever the foreman decides"
    entry["event"] = {"kind": event.get("kind"), "outcome": event.get("outcome"), "code": event.get("code"),
                      "reason": records.bounded(event.get("reason") or "", 300) or None,
                      "same_failure_streak": event.get("same_failure_streak")}
    return entry


def _event_files(run: Run, stage: str, n: int, files: dict) -> tuple[list[tuple[Path, str]], dict[str, str]]:
    """The attempt files the event names, as (source, point-relative name), and every path that names them."""
    sources: list[tuple[Path, str]] = []
    copied: dict[str, str] = {}
    attempt_dir = run.attempt_dir(stage, n)
    for name, filename in EVENT_FILES.items():
        candidate = attempt_dir / filename
        if not candidate.is_file():
            continue
        sources.append((candidate, filename))
        copied[str(candidate)] = filename
        if files.get(name):
            copied[str(files[name])] = filename
    result = files.get("result") or str(run.plan_dir / f"{stage}-result.json")
    if Path(result).is_file():
        sources.append((Path(result), f"{stage}-result.json"))
        copied[str(result)] = f"{stage}-result.json"
    return sources, copied


def _plan_files(run: Run, inputs: Path, sources: list[tuple[Path, str]], copied: dict[str, str]) -> bool:
    """Add the plan the attempt started from to `sources` and `copied`; False when the attempt kept no inputs/."""
    if not inputs.is_dir():
        return False
    for source in sorted(p for p in inputs.rglob("*") if p.is_file() and not p.is_symlink()):
        relative = source.relative_to(inputs).as_posix()
        sources.append((source, f"plan/{relative}"))
        copied[str(run.plan_dir / relative)] = f"plan/{relative}"
    return True


def _recorded_run_dirs(digest: object) -> list[str]:
    """The run directory the digest recorded, which may be an alias the run no longer answers to."""
    recorded = ((digest.get("paths") or {}).get("run_dir") or "") if isinstance(digest, dict) else ""
    return [recorded] if recorded else []


def _write_point(dest: Path, rewriter: Rewriter, digest: object, event_text: str,
                 sources: list[tuple[Path, str]], run_id: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    rewritten = rewriter.value(digest)
    if isinstance(rewritten, dict) and isinstance(rewritten.get("run"), dict):
        rewritten["run"]["id"] = run_id
    (dest / "digest.json").write_text(json.dumps(rewritten, indent=2, default=str) + "\n", encoding="utf-8")
    opening, _, rest = event_text.partition("\n")
    (dest / "event.md").write_text(opening + "\n" + rewriter.text(rest), encoding="utf-8")
    for source, relative in sources:
        rewriter.file(source, dest / relative, keep_run_id=("run_id",))


def _missing_files(files: dict) -> list[str]:
    return sorted(name for name, path in files.items() if path and not Path(path).is_file())


def _point(run: Run, attempt: dict, home: Path, dest: Path, cfg: config.Config) -> tuple[dict, list[str]]:
    """Write one decision point's directory and return its world entry and any build notes."""
    stage, n = attempt["stage"], int(attempt["n"])
    turn, event, digest_bytes, event_text = _read_turn(run, attempt)
    entry: dict = {"id": f"{stage}-{n}", "stage": stage, "attempt": n, **turn}
    snapshot = as_of(run, stage, n)
    event, event_text = _event(snapshot, stage, cfg, event, event_text)
    entry["snapshot"] = "recorded" if digest_bytes is not None else "reconstructed"
    if digest_bytes is None:
        digest_bytes = (json.dumps(foreman.digest(snapshot, cfg), indent=2, default=str) + "\n").encode("utf-8")
    digest = json.loads(digest_bytes.decode("utf-8", errors="replace"))
    entry.update(_judgement(run, attempt, event))
    # The files the event names, copied beside the digest; everything else the digest names becomes a placeholder.
    files = dict(event.get("files") or {})
    sources, copied = _event_files(run, stage, n, files)
    entry["plan"] = _plan_files(run, run.attempt_dir(stage, n) / "inputs", sources, copied)
    rewriter = Rewriter(run, home, copied, entry["plan"], _recorded_run_dirs(digest))
    entry["event"]["reason"] = rewriter.text(entry["event"]["reason"]) if entry["event"]["reason"] else None
    _write_point(dest, rewriter, digest, event_text, sources, run.id)
    missing = _missing_files(files)
    if missing:
        entry["event"]["missing_files"] = missing
    notes = [] if entry["plan"] else [f"{run.id} {entry['id']}: no inputs/ directory; the point is built without plan/"]
    return entry, notes


def _attempt_label(attempt: object, index: int) -> str:
    return f"{attempt.get('stage')}-{attempt.get('n')}" if isinstance(attempt, dict) else f"#{index}"


def _readable(run: Run) -> tuple[Run, list[str]]:
    """The run with only the attempts that carry a stage and an attempt number, and a note per dropped one."""
    readable, notes = [], []
    for index, attempt in enumerate(run.data.get("attempts") or []):
        if isinstance(attempt, dict) and isinstance(attempt.get("stage"), str) and isinstance(attempt.get("n"), int):
            readable.append(attempt)
        else:
            notes.append(f"{run.id} {_attempt_label(attempt, index)}: attempt skipped, unreadable "
                         "(no stage and attempt number)")
    return Run(run.dir, {**run.data, "attempts": readable}), notes


def _points(run: Run, home: Path, staging: Path, cfg: config.Config, notes: list[str]) -> list[dict]:
    """One point per decided headless attempt, each written under `staging`; a failure notes and skips it."""
    points: list[dict] = []
    for index, attempt in enumerate(run.data["attempts"]):
        try:
            if attempt.get("stage") not in HEADLESS or attempt.get("outcome") in (None, "cancelled"):
                continue
            entry, point_notes = _point(run, attempt, home, staging / "points" / f"{attempt['stage']}-{attempt['n']}",
                                        cfg)
        except Exception as error:  # noqa: BLE001 - one unreadable attempt never costs the rest of the world
            notes.append(f"{run.id} {_attempt_label(attempt, index)}: attempt skipped, unreadable "
                         f"({type(error).__name__}: {error})")
            continue
        points.append(entry)
        notes += point_notes
    return points


def _skill_hash(run: Run, points: list[dict]) -> str | None:
    """The policy hash of the last foreman turn that recorded one."""
    turns = (_read_json(run.dir / foreman.DIR / "turns" / str(e["turn"]) / "decision.json") or {}
                for e in points if e.get("turn"))
    policies = [p.get("sha256") for p in turns if isinstance(p, dict)]
    return next((sha for sha in reversed(policies) if sha), None)


def _world(run: Run, points: list[dict], source_sha: str) -> dict:
    runtime = _runtime(run)
    return {
        "schema": records.WORLD_SCHEMA,
        "run": {"id": run.id, "plan": run.plan, "created_at": run.data.get("created_at"),
                "attempts": [{"stage": a.get("stage"), "n": a.get("n"), "kind": a.get("kind", "stage"),
                              "outcome": a.get("outcome"), "code": a.get("code"), "tokens": a.get("tokens") or 0}
                             for a in run.data["attempts"]]},
        "repository": repository(run),
        "terminal_status": run.status,
        "runner_revision": (runtime.get("runner") or {}).get("revision"),
        "decision_schema": records.DECISION_SCHEMA,
        "skill_hash": _skill_hash(run, points),
        "skills_id": (runtime.get("skills") or {}).get("skills_id"),
        "source_sha256": source_sha,
        "builder": BUILDER,
        "built_at": utc_now(),
        "points": points,
    }


def _replace_world(world_dir: Path, staging: Path) -> None:
    # Labels and faults outlive a rebuild: a human label is never lost to a world refresh.
    for keep in ("labels.json", "faults.json"):
        if (world_dir / keep).is_file():
            shutil.copyfile(world_dir / keep, staging / keep)
    shutil.rmtree(world_dir, ignore_errors=True)
    staging.rename(world_dir)


def build(run_dir: Path, home: Path, *, force: bool = False) -> BuildResult:
    """Build or refresh the world for one run directory; never writes inside it."""
    run_dir = Path(run_dir)
    try:
        run = Run.load(run_dir)
    except (OSError, SchemaError, json.JSONDecodeError, KeyError) as error:
        return BuildResult(run_dir.name, "skipped", notes=[f"{run_dir.name}: skipped, run.json unreadable ({error})"])
    if run.status not in PARKED:
        return BuildResult(run.id, "skipped", notes=[f"{run.id}: skipped, status {run.status} is not terminal"])
    source_sha = hashlib.sha256((run_dir / "run.json").read_bytes()).hexdigest()
    world_dir = root(home) / run.id
    existing = _read_json(world_dir / "world.json")
    if not force and existing and existing.get("source_sha256") == source_sha and existing.get("builder") == BUILDER:
        return BuildResult(run.id, "unchanged", world_dir, [f"{run.id}: unchanged since its world was built"])
    staging = world_dir.with_name(world_dir.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    # Every later point is rebuilt from this copy, so one malformed entry never breaks the ones after it.
    run, notes = _readable(run)
    points = _points(run, home, staging, config.parse({"foreman": "codex"}), notes)
    rewriter = Rewriter(run, home, {}, False, [])
    for name in ("outcomes.jsonl", "note"):
        if (run_dir / name).is_file():
            rewriter.file(run_dir / name, staging / name)
    world = _world(run, points, source_sha)
    records.validate_world(world)
    atomic_write(staging / "world.json", json.dumps(world, indent=2) + "\n")
    _replace_world(world_dir, staging)
    return BuildResult(run.id, "built", world_dir, notes)


def run_dirs(home: Path) -> list[Path]:
    return [p for base in (Path(home) / "runs", Path(home) / "archive") if base.is_dir()
            for p in sorted(base.iterdir()) if p.is_dir()]


def worlds(home: Path) -> list[Path]:
    """Built worlds, sorted by run id."""
    base = root(home)
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and not p.name.endswith(".partial")
                  and (p / "world.json").is_file())


def load_world(world_dir: Path) -> dict:
    return records.validate_world(json.loads((Path(world_dir) / "world.json").read_text(encoding="utf-8")),
                                  name=f"{Path(world_dir).name}/world.json")


def load_labels(world_dir: Path) -> dict | None:
    path = Path(world_dir) / "labels.json"
    if not path.is_file():
        return None
    try:
        return records.validate_labels(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, records.RecordError):
        return None


# --- hindsight labels --------------------------------------------------------------------------

GENERATED_SKILLS = Path(__file__).resolve().parents[2] / "plugins" / "factory" / "skills"
HAND_CASES = Path(__file__).resolve().parents[1] / "evals" / "foreman" / "cases"
LABEL_TRIES = 2


@dataclass
class LabelResult:
    run_id: str
    status: str  # labelled, unchanged, failed
    problems: list[str] = field(default_factory=list)
    kept_human: int = 0


def scorable_points(world: dict) -> list[str]:
    return [p["id"] for p in world["points"] if p["scorable"]]


def label_prompt(world: dict, points: list[str]) -> str:
    skill = GENERATED_SKILLS / "hindsight" / "SKILL.md"
    return "\n".join([
        f"Follow the skill at {skill}.",
        "Read it and the foreman skill and protocol reference it links; do not use an installed factory plugin.",
        "",
        f"You label the finished factory run {world['run']['id']}, replayed as the world in the current directory.",
        f"Label exactly these scorable points: {', '.join(points) or 'none'}.",
        f"Reply with exactly one {records.HINDSIGHT_SCHEMA} JSON object.",
    ]) + "\n"


def _ask_labeller(world_dir: Path, world: dict, points: list[str], cfg: config.Config, attempt: int) -> tuple[dict | None, str | None]:
    from runner import hosts
    work = Path(world_dir) / "labelling" / str(attempt)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    schema = work / "hindsight.schema.json"
    schema.write_text(json.dumps(records.hindsight_json_schema(), indent=2) + "\n", encoding="utf-8")
    last = work / "last-message.md"
    argv = hosts.codex_foreman_argv(prompt=label_prompt(world, points), model=cfg.hindsight_model,
                                    effort=cfg.hindsight_effort, sandbox="read-only", worktree=Path(world_dir),
                                    writable=[], last_message=last, schema=schema)
    env = {**os.environ, "FACTORY_ROLE": "hindsight", "FACTORY_WORLD": world["run"]["id"]}
    try:
        with open(work / "stdout.jsonl", "wb") as stdout, open(work / "stderr.log", "wb") as stderr:
            code = subprocess.run(argv, cwd=world_dir, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                                  env=env, timeout=cfg.foreman_turn_timeout_s, check=False).returncode
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, f"labeller did not finish: {error}"
    if code != 0:
        return None, f"labeller exited {code}"
    try:
        text = last.read_text(encoding="utf-8").strip()
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1] if start >= 0 and end > start else text)
        return records.validate_hindsight(data, points=points), None
    except (OSError, ValueError, records.RecordError) as error:
        return None, f"{type(error).__name__}: {error}"


def label(world_dir: Path, cfg: config.Config, *, relabel: bool = False) -> LabelResult:
    """Label one world through the hindsight skill: one Codex session answers every scorable point.

    Human labels always win the merge; model labels are replaced. A world whose labeller answers outside
    `factory.hindsight/1` twice stays unlabelled and is excluded from scoring.
    """
    world_dir = Path(world_dir)
    world = load_world(world_dir)
    run_id = world["run"]["id"]
    points = scorable_points(world)
    existing = load_labels(world_dir)
    entries = dict((existing or {}).get("labels") or {})
    human = {k: v for k, v in entries.items() if v.get("label_source") == "human"}
    if not relabel and existing and _labels_current(world_dir, entries, points):
        return LabelResult(run_id, "unchanged", kept_human=len(human))
    answer, problems = _ask_until_valid(world_dir, world, points, cfg)
    if answer is None:
        return LabelResult(run_id, "failed", problems, kept_human=len(human))
    _write_labels(world_dir, run_id, answer, human)
    return LabelResult(run_id, "labelled", kept_human=len(human))


def _labels_current(world_dir: Path, entries: dict, points: list[str]) -> bool:
    return all(p in entries for p in points) and (world_dir / "faults.json").is_file()


def _ask_until_valid(world_dir: Path, world: dict, points: list[str],
                     cfg: config.Config) -> tuple[dict | None, list[str]]:
    """The labeller's first valid answer within `LABEL_TRIES`, and what was wrong with each try before it."""
    problems: list[str] = []
    for attempt in range(1, LABEL_TRIES + 1):
        answer, problem = _ask_labeller(world_dir, world, points, cfg, attempt)
        if answer is not None:
            return answer, problems
        problems.append(f"try {attempt}: {problem}")
    return None, problems


def _write_labels(world_dir: Path, run_id: str, answer: dict, human: dict) -> None:
    """Model labels from the answer with every human label merged over them, and the answer's faults."""
    now = utc_now()
    merged = {entry["point"]: {"accept": entry["accept"], "reject": entry["reject"],
                               "allow_override": entry["allow_override"], "note": entry["note"],
                               "label_source": "model", "labelled_at": now} for entry in answer["labels"]}
    merged.update(human)
    labels = records.validate_labels({"schema": records.LABELS_SCHEMA, "run": run_id,
                                      "decision_schema": records.DECISION_SCHEMA, "labelled_at": now,
                                      "labels": dict(sorted(merged.items()))})
    faults = records.validate_faults({"schema": records.FAULTS_SCHEMA, "run": run_id, "labelled_at": now,
                                      "faults": answer["faults"]})
    atomic_write(world_dir / "labels.json", json.dumps(labels, indent=2) + "\n")
    atomic_write(world_dir / "faults.json", json.dumps(faults, indent=2) + "\n")


class LabelError(Exception):
    pass


def set_label(world_dir: Path, point: str, accept: list[str], reject: list[str] | None = None,
              allow_override: bool = False, note: str | None = None) -> dict:
    """Record a person's label for one point; it wins every later model relabel."""
    world_dir = Path(world_dir)
    world = load_world(world_dir)
    if point not in {p["id"] for p in world["points"]}:
        raise LabelError(f"{world['run']['id']} has no point {point!r}; points: "
                         + ", ".join(p["id"] for p in world["points"]))
    existing = load_labels(world_dir) or {"schema": records.LABELS_SCHEMA, "run": world["run"]["id"], "labels": {}}
    entries = dict(existing["labels"])
    entries[point] = {"accept": list(accept), "reject": list(reject or []), "allow_override": bool(allow_override),
                      "note": note or "labelled by hand", "label_source": "human", "labelled_at": utc_now()}
    document = {**existing, "labels": dict(sorted(entries.items()))}
    try:
        records.validate_labels(document)
    except records.RecordError as error:
        raise LabelError(str(error)) from error
    atomic_write(world_dir / "labels.json", json.dumps(document, indent=2) + "\n")
    return entries[point]


def check_hand_cases(home: Path, cases: Path = HAND_CASES) -> dict:
    """Compare world labels with the hand-labelled eval cases; the hand label wins every disagreement.

    A case matches the world point whose run was created at the case digest's `run.created_at`, at the
    case's stage and attempt. Every case is accounted for: matched, or reported unmatched.
    """
    index = _point_index(home)
    report = {"cases": 0, "matched": [], "unmatched": [], "disagreements": [], "unlabelled": [], "sizes": []}
    for case in sorted(p for p in Path(cases).iterdir() if p.is_dir()):
        report["cases"] += 1
        _check_case(case, index, report)
    report["mean_accept_size"] = round(sum(report["sizes"]) / len(report["sizes"]), 2) if report["sizes"] else None
    return report


def _point_index(home: Path) -> dict[tuple, tuple[Path, dict]]:
    """Every readable world point by its run's `created_at` and its point id."""
    index: dict[tuple, tuple[Path, dict]] = {}
    for world_dir in worlds(home):
        try:
            world = load_world(world_dir)
        except (OSError, ValueError, records.RecordError):
            continue
        for point in world["points"]:
            index[(world["run"].get("created_at"), point["id"])] = (world_dir, point)
    return index


def _check_case(case: Path, index: dict[tuple, tuple[Path, dict]], report: dict) -> None:
    """Account for one hand case in `report`: unmatched, unlabelled, or matched with its disagreements."""
    expected = json.loads((case / "expected.json").read_text(encoding="utf-8"))
    digest = json.loads((case / "digest.json").read_text(encoding="utf-8"))
    key = ((digest.get("run") or {}).get("created_at"), f"{expected['stage']}-{expected['attempt']}")
    if key not in index:
        report["unmatched"].append(case.name)
        return
    world_dir, point = index[key]
    report["matched"].append(case.name)
    entry = ((load_labels(world_dir) or {}).get("labels") or {}).get(point["id"])
    if entry is None:
        report["unlabelled"].append(case.name)
        return
    report["sizes"].append(len(entry["accept"]))
    report["disagreements"] += _disagreements(case.name, expected, entry["accept"])


def _disagreements(name: str, expected: dict, accepted: list[str]) -> list[str]:
    hand, model = set(expected["accept"]), set(accepted)
    rejected = set(expected.get("reject") or [])
    return [f"{name}: {action} (" + ("hand accepts, label does not" if action in hand
                                    else "label accepts, hand rejects" if action in rejected
                                    else "label accepts, hand does not") + ")"
            for action in sorted(hand ^ model)]
