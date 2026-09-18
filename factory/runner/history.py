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

from runner import config, foreman, records
from runner.model import HEADLESS, PARKED, Run, SchemaError, atomic_write, hard_stop, utc_now

DIR = "history"
LOCK = "history.lock"
# Bumped when the world layout or the rewriting rules change, so an unchanged run is rebuilt once.
BUILDER = "factory.history-builder/1"
PLACEHOLDERS = ("<plan_dir>", "<worktree>", "<run_dir>", "<repo>", "<home>")
EVENT_FILES = {"gate": "gate.json", "last_message": "last-message.md", "stderr": "stderr.log"}
TEXT_LIMIT = 8 * 1024 * 1024


class HistoryLocked(Exception):
    def __init__(self, path: Path):
        super().__init__(f"another factory history or dream command holds {path}; try again when it finishes")
        self.path = path


class HistoryLock:
    """Exclusive non-blocking flock on ~/.factory/history.lock, released on process death.

    `factory history build`, `factory history label`, and `factory dream` all take it, so no world is
    rewritten and no label regenerated while a dream reads them, and two dreams never race the cache.
    """

    def __init__(self, home: Path):
        self.path = Path(home) / LOCK
        self.fd = -1

    def __enter__(self) -> "HistoryLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise HistoryLocked(self.path) from None
        self.fd = fd
        return self

    def __exit__(self, *exc: object) -> None:
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
    match = re.match(r"^(?:[\w.+-]+@)?([\w.-]+\.[a-z]{2,}):(?!//)(.+)$", value, re.I) \
        or re.match(r"^(?:https?|ssh|git)://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)$", value, re.I)
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
                                timeout=30, stdin=subprocess.DEVNULL)
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
        run_dirs = {str(run.dir), os.path.realpath(run.dir), str(Path(home) / "runs" / run.id),
                    str(Path(home) / "archive" / run.id), *recorded_dirs}
        pairs: list[tuple[str, str]] = []
        for source, target in copied.items():
            pairs.append((source, target))
        prefixes: list[tuple[str, str]] = []
        for base in run_dirs:
            if not base:
                continue
            plan = f"{base}/worktree/.dev/{run.plan}"
            prefixes.append((plan, "plan" if plan_copied else "<plan_dir>"))
            prefixes.append((f"{base}/worktree", "<worktree>"))
            prefixes.append((base, "<run_dir>"))
            for source, target in copied.items():
                # The same file under another alias of the run directory (archive, a symlinked temp dir).
                for other in run_dirs:
                    if other and source.startswith(other + "/"):
                        pairs.append((base + source[len(other):], target))
        repo = str(run.data.get("repo") or "")
        for value in {repo, os.path.realpath(repo) if repo else ""}:
            if value:
                prefixes.append((value, "<repo>"))
        home_dir = str(Path.home())
        prefixes += [(home_dir, "<home>"), (os.path.realpath(home_dir), "<home>")]
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
        raw = source.read_bytes()[-TEXT_LIMIT:]
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = raw.decode("utf-8", errors="replace")
        if source.suffix == ".json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
            if data is not None:
                rewritten = self.value(data)
                if isinstance(data, dict) and isinstance(rewritten, dict):
                    for key in keep_run_id:
                        if data.get(key) == self.run_id:
                            rewritten[key] = self.run_id
                dest.write_text(json.dumps(rewritten, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                return
        dest.write_text(self.text(text), encoding="utf-8")


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


def _point(run: Run, attempt: dict, home: Path, dest: Path, cfg: config.Config) -> tuple[dict, list[str]]:
    """Write one decision point's directory and return its world entry and any build notes."""
    stage, n = attempt["stage"], int(attempt["n"])
    point_id = f"{stage}-{n}"
    notes: list[str] = []
    summary = attempt.get("foreman") or {}
    turn_dir = run.dir / summary["dir"] if summary.get("dir") else None
    turn_record = _read_json(turn_dir / "decision.json") if turn_dir else None
    entry: dict = {"id": point_id, "stage": stage, "attempt": n}
    digest_bytes: bytes | None = None
    event_text: str | None = None
    event: dict | None = None
    if turn_record is not None:
        entry["turn"] = int(turn_record.get("turn") or summary.get("turn") or 0)
        entry["origin"] = "cold" if turn_record.get("cold") else "warm"
        decision = turn_record.get("decision")
        if decision and turn_record.get("source") == "foreman":
            entry["decision_source"] = "foreman"
            entry["recorded_decision"] = {"action": decision.get("action"), "vocabulary": "decision",
                                          "override": bool(decision.get("override"))}
        else:
            entry["decision_source"] = "fallback"
        event = turn_record.get("event") if isinstance(turn_record.get("event"), dict) else None
        if (turn_dir / "digest.json").is_file():
            digest_bytes = (turn_dir / "digest.json").read_bytes()
        if (turn_dir / "prompt.txt").is_file():
            event_text = _event_from_prompt((turn_dir / "prompt.txt").read_text(encoding="utf-8", errors="replace"),
                                            run.id)
    elif attempt.get("transition"):
        entry["decision_source"] = "auto"
        entry["recorded_decision"] = {"action": attempt["transition"].get("action"), "vocabulary": "transition"}
    snapshot = as_of(run, stage, n)
    last = snapshot.data["attempts"][-1]
    if event is None or event_text is None:
        from runner import gates
        outcome = gates.Outcome(last["outcome"], last.get("reason"), bool(last.get("retryable")),
                                last.get("source") or "runner", warning=last.get("warning"), code=last.get("code"),
                                conditions=list(last.get("conditions") or []))
        event = event or foreman.attempt_event(snapshot, stage, last, outcome)
        event_text = event_text or foreman.event_message(snapshot, cfg, event)
    entry["snapshot"] = "recorded" if digest_bytes is not None else "reconstructed"
    if digest_bytes is None:
        digest_bytes = (json.dumps(foreman.digest(snapshot, cfg), indent=2, default=str) + "\n").encode("utf-8")
    digest = json.loads(digest_bytes.decode("utf-8", errors="replace"))
    stop = hard_stop(run, attempt)
    entry["scorable"] = stop is None
    if stop:
        entry["unscorable_reason"] = f"hard stop {stop}: the runner parks whatever the foreman decides"
    entry["event"] = {"kind": event.get("kind"), "outcome": event.get("outcome"), "code": event.get("code"),
                      "reason": records.bounded(event.get("reason") or "", 300) or None,
                      "same_failure_streak": event.get("same_failure_streak")}

    # The files the event names, copied beside the digest; everything else the digest names becomes a placeholder.
    copied: dict[str, str] = {}
    sources: list[tuple[Path, str]] = []
    files = dict(event.get("files") or {})
    attempt_dir = run.attempt_dir(stage, n)
    for name, filename in EVENT_FILES.items():
        candidate = attempt_dir / filename
        if candidate.is_file():
            sources.append((candidate, filename))
            copied[str(candidate)] = filename
        recorded = files.get(name)
        if recorded and candidate.is_file():
            copied[str(recorded)] = filename
    result = files.get("result") or str(run.plan_dir / f"{stage}-result.json")
    if Path(result).is_file():
        sources.append((Path(result), f"{stage}-result.json"))
        copied[str(result)] = f"{stage}-result.json"
    missing = [name for name, path in files.items() if path and not Path(path).is_file()]
    inputs = attempt_dir / "inputs"
    plan = inputs.is_dir()
    if plan:
        for source in sorted(p for p in inputs.rglob("*") if p.is_file() and not p.is_symlink()):
            relative = source.relative_to(inputs).as_posix()
            sources.append((source, f"plan/{relative}"))
            copied[str(run.plan_dir / relative)] = f"plan/{relative}"
    else:
        notes.append(f"{run.id} {point_id}: no inputs/ directory; the point is built without plan/")
    entry["plan"] = plan
    recorded_run_dir = ((digest.get("paths") or {}).get("run_dir") or "") if isinstance(digest, dict) else ""
    rewriter = Rewriter(run, home, copied, plan, [recorded_run_dir] if recorded_run_dir else [])
    entry["event"]["reason"] = rewriter.text(entry["event"]["reason"]) if entry["event"]["reason"] else None
    dest.mkdir(parents=True, exist_ok=True)
    rewritten = rewriter.value(digest)
    if isinstance(rewritten, dict) and isinstance(rewritten.get("run"), dict):
        rewritten["run"]["id"] = run.id
    (dest / "digest.json").write_text(json.dumps(rewritten, indent=2, default=str) + "\n", encoding="utf-8")
    opening, _, rest = event_text.partition("\n")
    (dest / "event.md").write_text(opening + "\n" + rewriter.text(rest), encoding="utf-8")
    for source, relative in sources:
        rewriter.file(source, dest / relative, keep_run_id=("run_id",))
    if missing:
        entry.setdefault("event", {})["missing_files"] = sorted(missing)
    return entry, notes


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
    cfg = config.parse({"foreman": "codex"})
    staging = world_dir.with_name(world_dir.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    notes: list[str] = []
    points: list[dict] = []
    readable = []
    for index, attempt in enumerate(run.data.get("attempts") or []):
        if isinstance(attempt, dict) and isinstance(attempt.get("stage"), str) and isinstance(attempt.get("n"), int):
            readable.append(attempt)
        else:
            label = f"{attempt.get('stage')}-{attempt.get('n')}" if isinstance(attempt, dict) else f"#{index}"
            notes.append(f"{run.id} {label}: attempt skipped, unreadable (no stage and attempt number)")
    # Every later point is rebuilt from this copy, so one malformed entry never breaks the ones after it.
    run = Run(run.dir, {**run.data, "attempts": readable})
    for index, attempt in enumerate(readable):
        try:
            if attempt.get("stage") not in HEADLESS or attempt.get("outcome") in (None, "cancelled"):
                continue
            entry, point_notes = _point(run, attempt, home, staging / "points" / f"{attempt['stage']}-{attempt['n']}",
                                        cfg)
        except Exception as error:  # noqa: BLE001 - one unreadable attempt never costs the rest of the world
            label = f"{attempt.get('stage')}-{attempt.get('n')}" if isinstance(attempt, dict) else f"#{index}"
            notes.append(f"{run.id} {label}: attempt skipped, unreadable ({type(error).__name__}: {error})")
            continue
        points.append(entry)
        notes += point_notes
    rewriter = Rewriter(run, home, {}, False, [])
    for name in ("outcomes.jsonl", "note"):
        if (run_dir / name).is_file():
            rewriter.file(run_dir / name, staging / name)
    runtime = _runtime(run)
    policies = [p.get("sha256") for p in (_read_json(run.dir / foreman.DIR / "turns" / str(e["turn"]) / "decision.json")
                                          or {} for e in points if e.get("turn")) if isinstance(p, dict)]
    decision_schema = records.DECISION_SCHEMA
    world = {
        "schema": records.WORLD_SCHEMA,
        "run": {"id": run.id, "plan": run.plan, "created_at": run.data.get("created_at"),
                "attempts": [{"stage": a.get("stage"), "n": a.get("n"), "kind": a.get("kind", "stage"),
                              "outcome": a.get("outcome"), "code": a.get("code"), "tokens": a.get("tokens") or 0}
                             for a in run.data.get("attempts") or [] if isinstance(a, dict)]},
        "repository": repository(run),
        "terminal_status": run.status,
        "runner_revision": (runtime.get("runner") or {}).get("revision"),
        "decision_schema": decision_schema,
        "skill_hash": next((sha for sha in reversed(policies) if sha), None),
        "skills_id": (runtime.get("skills") or {}).get("skills_id"),
        "source_sha256": source_sha,
        "builder": BUILDER,
        "built_at": utc_now(),
        "points": points,
    }
    records.validate_world(world)
    atomic_write(staging / "world.json", json.dumps(world, indent=2) + "\n")
    # Labels and faults outlive a rebuild: a human label is never lost to a world refresh.
    for keep in ("labels.json", "faults.json"):
        if (world_dir / keep).is_file():
            shutil.copyfile(world_dir / keep, staging / keep)
    shutil.rmtree(world_dir, ignore_errors=True)
    staging.rename(world_dir)
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

