"""The dream loop: replay the foreman skill over history worlds, score it against hindsight labels, revise it.

A replay is one cold Codex turn per decision point, reading the point's digest snapshot and event under a
staged plugin tree; its decision is scored against the point's hindsight label by the action ladder below.
`factory dream` scores the incumbent skill, asks the dream skill for revisions, keeps the best on the
selection worlds, and deploys it only when it beats the incumbent on confirmation worlds it never saw.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from runner import config, hosts, provenance, records
from runner.model import utc_now

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
    existing = sys.modules.get("build_codex_plugin")
    if existing is not None and getattr(existing, "__file__", None) == str(path):
        return existing
    spec = importlib.util.spec_from_file_location("build_codex_plugin", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_codex_plugin"] = module
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
    skipped: bool = False  # the call cap was reached before this fresh call


def replay(point_dir: Path, plugin_tree: Path, cfg: config.Config, cache: Cache | None, *, repeat: int = 1,
           world: str = "", work: Path | None = None, budget: "Budget | None" = None) -> Replay:
    """One cold foreman turn on a recorded point under `plugin_tree`'s skill; first passes come from the cache.

    Repeats 2..K are always fresh calls: a repeat is a draw, not a lookup. A fresh call needs `budget`.
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
    if budget is not None and not budget.take():
        return Replay(None, "the call cap was reached", False, key, skipped=True)
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


# --- selection and confirmation --------------------------------------------------------------

CONFIRMATION_TARGET_WORLDS = 6
CONFIRMATION_TARGET_REPOS = 2


@dataclass
class Split:
    selection: list[str]
    confirmation: list[str]
    rule: str  # whole-repo, split-by-run, too-small
    notes: list[str]

    @property
    def confirmation_hash(self) -> str:
        return hashlib.sha256("\n".join(sorted(self.confirmation)).encode("utf-8")).hexdigest()


def split(worlds: list[dict]) -> Split:
    """Split worlds by run into selection and confirmation; confirmation prefers whole unseen repositories.

    `worlds` are `{"id", "repository"}` entries. Input order never matters: worlds are sorted by run id first.
    """
    ordered = sorted(worlds, key=lambda w: w["id"])
    if len(ordered) < 2:
        return Split([w["id"] for w in ordered], [], "too-small",
                     [f"{len(ordered)} world(s): too few to hold any back for confirmation"])
    by_repo: dict[str, list[str]] = {}
    for world in ordered:
        by_repo.setdefault(world["repository"], []).append(world["id"])
    repos = sorted(by_repo)
    if len(repos) >= 3:
        confirmation: list[str] = []
        taken: list[str] = []
        for repo in repos[:-1]:
            if len(confirmation) >= CONFIRMATION_TARGET_WORLDS and len(taken) >= CONFIRMATION_TARGET_REPOS:
                break
            confirmation += by_repo[repo]
            taken.append(repo)
        selection = [w["id"] for w in ordered if w["id"] not in confirmation]
        return Split(selection, sorted(confirmation), "whole-repo",
                     [f"confirmation holds whole repositories never seen in selection: {', '.join(taken)}"])
    notes: list[str] = []
    short = [repo for repo in repos if len(by_repo[repo]) < 2]
    if len(repos) == 2 and not short:
        confirmation = [run for repo in repos for run in by_repo[repo][1::2]]
        notes.append("two repositories: each one feeds both sets, split by run")
    else:
        if len(repos) == 1:
            notes.append(f"one repository ({repos[0]}): the repository rule could not apply")
        for repo in short:
            notes.append(f"repository {repo} holds fewer than two worlds and could not span both sets")
        confirmation = [w["id"] for w in ordered][1::2]
    selection = [w["id"] for w in ordered if w["id"] not in confirmation]
    return Split(selection, sorted(confirmation), "split-by-run", notes)


# --- candidate checks ------------------------------------------------------------------------

MAX_SKILL_LINES = 150
SHINGLE = 40
RUN_ID = re.compile(r"\b20\d{6}-\d{4}-[a-z0-9]+(?:-[a-z0-9]+)*\b")
# Credential shapes the recorded traces actually carry; a revision is pushed, so none may reach it.
SECRET_PATTERNS = (
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abp]-[0-9A-Za-z-]{10,}"),
    re.compile(r"\b(?:gh[pousr]_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{30,})"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
DECISION_ROW = re.compile(r"^\|\s*`([a-z]+)`\s*\|", re.M)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _windows(text: str) -> set[int]:
    text = _normalize(text)
    return {hash(text[i:i + SHINGLE]) for i in range(0, max(0, len(text) - SHINGLE + 1))}


def f03_pattern() -> re.Pattern:
    """The phrase check scripts/validate.sh F03 runs over unattended factory material."""
    return re.compile(
        r"\b(ask(s|ed|ing)?|confirm(s|ed|ing)?\s+with|wait(s|ing)?\s+for|check(s|ing)?\s+with)\s+(the\s+)?(user|operator|human)s?\b"
        r"|\b(user|operator|human)\s+(decides|approves|authorizes|chooses|confirms|answers)\b"
        r"|structured user-input tool|\bhuman call\b|\bconfirm (with|before)\b",
        re.I,
    )


@dataclass
class Denylist:
    literals: set[str]
    shingles: set[int]


def denylist(world_dirs: list[Path]) -> Denylist:
    """What a revision must not copy from the selection worlds: identities and 40-character runs of recorded text."""
    literals: set[str] = set()
    shingles: set[int] = set()
    for world_dir in world_dirs:
        world = json.loads((Path(world_dir) / "world.json").read_text(encoding="utf-8"))
        literals |= {world["run"]["id"], world["repository"], world["run"].get("plan") or ""}
        for point in world["points"]:
            point_dir = Path(world_dir) / "points" / point["id"]
            try:
                digest = json.loads((point_dir / "digest.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                digest = {}
            run = digest.get("run") or {}
            literals |= {str(run.get("branch") or ""), str(run.get("plan") or "")}
            number = (digest.get("pr") or {}).get("number")
            if number:
                literals |= {f"#{number}", f"pull/{number}"}
            for name in ("gate.json", "last-message.md"):
                path = point_dir / name
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                if name == "gate.json":
                    try:
                        text = str(json.loads(text).get("reason") or "")
                    except (json.JSONDecodeError, AttributeError):
                        pass
                shingles |= _windows(text)
            if point["event"].get("reason"):
                shingles |= _windows(point["event"]["reason"])
    return Denylist({literal for literal in literals if len(literal) >= 4}, shingles)


def check_candidate(text: str, deny: Denylist, incumbent: str) -> tuple[str, str] | None:
    """The first deterministic check a candidate skill fails, as (check, detail); None when it passes them all."""
    def frontmatter(value: str) -> str:
        match = re.match(r"^---\n.*?\n---\n", value, re.S)
        return match.group(0) if match else ""

    if not frontmatter(text) or frontmatter(text) != frontmatter(incumbent):
        return "frontmatter", "the frontmatter must stay exactly as it is"
    named = set(DECISION_ROW.findall(text))
    missing = [action for action in records.DECISION_ACTIONS if action not in named]
    if missing:
        return "actions", f"the decision table no longer names {', '.join(missing)}"
    lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    if lines > MAX_SKILL_LINES:
        return "length", f"{lines} lines, the limit is {MAX_SKILL_LINES}"
    if "—" in text:
        return "emdash", "an em dash character"
    found = f03_pattern().search(text)
    if found:
        return "F03", f"routes a decision to a person: {found.group(0)!r}"
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return "secret", f"a credential-shaped literal matching {pattern.pattern[:30]}"
    for literal in sorted(deny.literals):
        if re.search(rf"(?<![\w-]){re.escape(literal)}(?![\w-])", text, re.I):
            return "denylist", f"names {literal!r} from the selection runs"
    found = RUN_ID.search(text)
    if found:
        return "denylist", f"carries a run id shape {found.group(0)!r}"
    # Only new text counts: a phrase the incumbent already holds is not memorized from the traces.
    copied = (_windows(text) - _windows(incumbent)) & deny.shingles
    if copied:
        return "shingle", f"{len(copied)} 40-character run(s) of recorded gate reasons or agent messages"
    return None


# --- scoring a policy over worlds ------------------------------------------------------------

@dataclass
class Budget:
    """`--max-calls`: fresh replay calls only; cache hits, labeller, and dream sessions are free here."""
    limit: int
    spent: int = 0
    exhausted: bool = False

    def take(self) -> bool:
        if self.spent >= self.limit:
            self.exhausted = True
            return False
        self.spent += 1
        return True


@dataclass
class WorldView:
    id: str
    dir: Path
    world: dict
    labels: dict

    @property
    def repository(self) -> str:
        return self.world["repository"]

    def points(self) -> list[dict]:
        """Scorable points that carry a label."""
        return [p for p in self.world["points"] if p["scorable"] and p["id"] in self.labels]


@dataclass
class PolicyScore:
    name: str
    worlds: dict = field(default_factory=dict)  # world id -> score or None
    points: list = field(default_factory=list)
    partial: bool = False

    @property
    def mean(self) -> float | None:
        return score_policy(list(self.worlds.values()))


def score_on(name: str, views: list[WorldView], tree: Path, cfg: config.Config, cache: Cache, budget: Budget,
             repeats: int, work: Path) -> PolicyScore:
    """Replay a policy over every labelled scorable point of `views`; stops as soon as the call cap is reached."""
    result = PolicyScore(name)
    for view in views:
        entries = []
        for point in view.points():
            label = view.labels[point["id"]]
            scores, last = [], None
            for repeat in range(1, repeats + 1):
                answer = replay(view.dir / "points" / point["id"], tree, cfg, cache, repeat=repeat, world=view.id,
                                work=work / view.id / point["id"] / str(repeat), budget=budget)
                if answer.skipped:
                    result.partial = True
                    break
                scores.append(score_point(answer.decision, label))
                last = answer
            if result.partial:
                break
            score = round(sum(scores) / len(scores), 6)
            entry = {"world": view.id, "point": point["id"], "score": score, "scores": scores, "scorable": True,
                     "origin": point.get("origin") or "auto", "decision": last.decision, "problem": last.problem,
                     "label": {k: label[k] for k in ("accept", "reject", "allow_override")},
                     "note": label.get("note")}
            entries.append(entry)
            result.points.append(entry)
        if result.partial:
            break
        result.worlds[view.id] = score_world(entries)
    return result


# --- the round -------------------------------------------------------------------------------

def repo_root() -> Path:
    """The runner's own checkout; `FACTORY_REPO_ROOT` points tests at a fixture repository."""
    return Path(os.environ.get("FACTORY_REPO_ROOT") or provenance.REPO_ROOT)


def mint_id(dreams: Path, now: datetime | None = None) -> Path:
    """`{stamp}-dream`, with a numeric suffix on collision, the way run ids are minted."""
    dreams.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now().astimezone()).strftime("%Y%m%d-%H%M")
    suffix = 1
    while True:
        name = f"{stamp}-dream" if suffix == 1 else f"{stamp}-dream-{suffix}"
        try:
            (dreams / name).mkdir()
            return dreams / name
        except FileExistsError:
            suffix += 1


def load_views(home: Path, world_ids: list[str] | None) -> tuple[list[WorldView], list[str]]:
    """Labelled worlds to dream over, and the names of those excluded for having no labels."""
    from runner import history
    views, unlabelled = [], []
    for world_dir in history.worlds(home):
        if world_ids and world_dir.name not in world_ids:
            continue
        world = history.load_world(world_dir)
        labels = history.load_labels(world_dir)
        if labels is None:
            unlabelled.append(world_dir.name)
            continue
        views.append(WorldView(world_dir.name, world_dir, world, labels["labels"]))
    return views, unlabelled


def dream_prompt(round_dir: Path, tree: Path) -> str:
    skill = tree / "skills" / "dream" / "SKILL.md"
    return "\n".join([
        f"Follow the skill at {skill}.",
        "Read it from that directory; do not use an installed factory plugin.", "",
        f"The current skill is at {round_dir / 'input' / 'SKILL.md'}.",
        f"The traces are at {round_dir / 'traces.jsonl'} and the prior scores at {round_dir / 'scores.json'}.",
        f"The protocol reference is at {tree / provenance.PROTOCOL}.",
        f"Write the revised skill to {round_dir / 'output' / 'SKILL.md'}.",
    ]) + "\n"


def ask_dream(round_dir: Path, round_n: int, tree: Path, cfg: config.Config) -> tuple[str | None, str | None]:
    """One dream-skill session: reads the round's inputs, writes one revised skill into its output directory."""
    output = round_dir / "output"
    output.mkdir(parents=True, exist_ok=True)
    schema = round_dir / "summary.schema.json"
    schema.write_text(json.dumps({"type": "object", "properties": {"summary": {"type": "string"}},
                                  "required": ["summary"], "additionalProperties": False}) + "\n", encoding="utf-8")
    argv = hosts.codex_foreman_argv(prompt=dream_prompt(round_dir, tree), model=cfg.dream_model,
                                    effort=cfg.dream_effort, sandbox="workspace-write", worktree=output, writable=[],
                                    last_message=round_dir / "last-message.md", schema=schema)
    env = {**os.environ, "FACTORY_ROLE": "dream", "FACTORY_DREAM_ROUND": str(round_n)}
    try:
        with open(round_dir / "stdout.jsonl", "wb") as stdout, open(round_dir / "stderr.log", "wb") as stderr:
            code = subprocess.run(argv, cwd=output, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL, env=env,
                                  timeout=max(cfg.foreman_turn_timeout_s, 1800)).returncode
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, f"dream session did not finish: {error}"
    if code != 0:
        return None, f"dream session exited {code}"
    path = output / "SKILL.md"
    if not path.is_file():
        return None, "the dream session wrote no SKILL.md"
    return path.read_text(encoding="utf-8"), None


def traces(score: PolicyScore, views: dict[str, WorldView]) -> list[dict]:
    rows = []
    for entry in score.points:
        event = (views[entry["world"]].dir / "points" / entry["point"] / "event.md").read_text(encoding="utf-8")
        rows.append({"policy": score.name, "point": f"{entry['world']}/{entry['point']}",
                     "event": records.bounded(event, 2000), "decision": entry["decision"],
                     "problem": entry["problem"], "label": entry["label"], "score": entry["score"],
                     "note": entry["note"]})
    return rows


def rounds_served(dreams: Path, confirmation_hash: str, current: Path) -> int:
    count = 0
    for path in dreams.glob("*/decision.json"):
        if path.parent == current:
            continue
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("split", {}).get("confirmation_hash") == confirmation_hash:
                count += 1
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    return count


def run(home: Path, cfg: config.Config, *, rounds: int = 3, repeats: int | None = None, max_calls: int = 1000,
        deploy_enabled: bool = True, world_ids: list[str] | None = None, echo=print) -> tuple[Path, dict]:
    """One dream: score the incumbent, ask for `rounds` revisions, keep the best, confirm it, maybe deploy it."""
    root = repo_root()
    tree = root / "plugins" / "factory"
    incumbent_text = (root / "factory" / provenance.FOREMAN_SKILL).read_text(encoding="utf-8")
    views, unlabelled = load_views(home, world_ids)
    by_id = {v.id: v for v in views}
    chosen = split([{"id": v.id, "repository": v.repository} for v in views])
    selection = [by_id[w] for w in chosen.selection]
    confirmation = [by_id[w] for w in chosen.confirmation]
    dreams = Path(home) / "dreams"
    dream_dir = mint_id(dreams)
    cache = Cache(dreams / "cache")
    budget = Budget(max_calls)
    repeats_selection = repeats or 1
    repeats_confirmation = repeats or 2
    work = dream_dir / "replays"
    echo(f"Dream {dream_dir.name}: {len(selection)} selection and {len(confirmation)} confirmation world(s), "
         f"rule {chosen.rule}.")
    incumbent = score_on("incumbent", selection, tree, cfg, cache, budget, repeats_selection, work / "incumbent")
    pool = [(incumbent, incumbent_text, tree)]
    candidates: list[dict] = []
    trace_rows = traces(incumbent, by_id)
    best = pool[0]
    deny = denylist([v.dir for v in selection])
    for round_n in range(1, rounds + 1):
        if best[0].partial:
            break
        round_dir = dream_dir / "rounds" / str(round_n)
        (round_dir / "input").mkdir(parents=True, exist_ok=True)
        (round_dir / "input" / "SKILL.md").write_text(best[1], encoding="utf-8")
        with open(round_dir / "traces.jsonl", "w", encoding="utf-8") as handle:
            for row in traces(best[0], by_id):
                handle.write(json.dumps(row) + "\n")
        (round_dir / "scores.json").write_text(json.dumps({"incumbent": incumbent.mean, "rounds": [
            {k: c[k] for k in ("round", "score", "rejected", "diff") if k in c} for c in candidates]}, indent=2)
            + "\n", encoding="utf-8")
        text, problem = ask_dream(round_dir, round_n, tree, cfg)
        entry: dict = {"round": round_n}
        candidates.append(entry)
        if text is None:
            entry["rejected"] = ["session", problem]
            echo(f"round {round_n}: no candidate ({problem})")
            continue
        entry["diff"] = "".join(difflib.unified_diff(incumbent_text.splitlines(True), text.splitlines(True),
                                                     "incumbent/SKILL.md", f"round-{round_n}/SKILL.md"))
        failed = check_candidate(text, deny, incumbent_text)
        if failed:
            entry["rejected"] = list(failed)
            echo(f"round {round_n}: candidate rejected by {failed[0]} ({failed[1]})")
            continue
        staged = stage_plugin(text, dream_dir / "candidates" / str(round_n) / "plugins" / "factory", tree)
        scored = score_on(f"round-{round_n}", selection, staged, cfg, cache, budget, repeats_selection,
                          work / f"round-{round_n}")
        trace_rows += traces(scored, by_id)
        entry.update({"score": scored.mean, "partial": scored.partial, "tree": str(staged),
                      "source_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                      "generated_hash": provenance.tree_policy(staged)["sha256"]})
        echo(f"round {round_n}: selection score {scored.mean} (incumbent {incumbent.mean})"
             + (" - partial, the call cap was reached" if scored.partial else ""))
        entry["world_scores"] = scored.worlds
        if scored.partial:
            break
        if scored.mean is not None and (best[0].mean is None or scored.mean > best[0].mean):
            best = (scored, text, staged)
    with open(dream_dir / "traces.jsonl", "w", encoding="utf-8") as handle:
        for row in trace_rows:
            handle.write(json.dumps(row) + "\n")
    partial = incumbent.partial or any(c.get("partial") for c in candidates) or best[0].partial
    confirm_points = sum(len(v.points()) for v in confirmation)
    confirmed: dict = {}
    if not partial and confirm_points:
        confirmed["incumbent"] = score_on("incumbent", confirmation, tree, cfg, cache, budget, repeats_confirmation,
                                          work / "confirm-incumbent")
        if best[0] is not incumbent:
            confirmed["candidate"] = score_on(best[0].name, confirmation, best[2], cfg, cache, budget,
                                              repeats_confirmation, work / "confirm-candidate")
        partial = any(s.partial for s in confirmed.values())
    decision = {
        "schema": "factory.dream/1", "dream": dream_dir.name, "at": utc_now(),
        "split": {"rule": chosen.rule, "selection": chosen.selection, "confirmation": chosen.confirmation,
                  "notes": chosen.notes, "confirmation_hash": chosen.confirmation_hash,
                  "rounds_served": rounds_served(dreams, chosen.confirmation_hash, dream_dir)},
        "calls": {"fresh": budget.spent, "max": max_calls}, "partial": partial,
        "best": best[0].name, "selection": {"incumbent": incumbent.mean,
                                            **{f"round-{c['round']}": c.get("score") for c in candidates}},
        "confirmation": {name: score.mean for name, score in confirmed.items()},
        "confirmation_points": confirm_points,
    }
    candidate = None if best[0] is incumbent else {"text": best[1], "tree": best[2], "name": best[0].name}
    decision.update(deploy(dream_dir=dream_dir, candidate=candidate, confirmation=confirmed,
                           confirmation_worlds=confirmation, partial=partial, cfg=cfg, root=root,
                           enabled=deploy_enabled, echo=echo))
    (dream_dir / "decision.json").write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
    report(dream_dir, decision, incumbent, candidates, confirmed, views, selection, confirmation, unlabelled,
           [v for v in selection if not v.points()])
    echo(f"Dream {dream_dir.name}: {'deployed' if decision['deployed'] else 'not deployed'} ({decision['reason']}). "
         f"Report: {dream_dir / 'report.md'}")
    return dream_dir, decision


# --- the report ------------------------------------------------------------------------------

def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.3f}"


def report(dream_dir: Path, decision: dict, incumbent: PolicyScore, candidates: list[dict], confirmed: dict,
           views: list[WorldView], selection: list[WorldView], confirmation: list[WorldView],
           unlabelled: list[str], unscorable: list[WorldView]) -> Path:
    lines = [f"# Dream {decision['dream']}", "",
             f"Decision: **{'deployed' if decision['deployed'] else 'not deployed'}** - {decision['reason']}"
             + (f" ({decision['cause']})" if decision.get("cause") else ""), "",
             f"Best on selection: {decision['best']}. Fresh replay calls: {decision['calls']['fresh']} of "
             f"{decision['calls']['max']}" + (" - partial: the call cap was reached, so nothing deploys"
                                              if decision["partial"] else "") + ".", "",
             "## Split", "",
             f"Applied rule: {decision['split']['rule']}; {len(selection)} selection and {len(confirmation)} "
             f"confirmation world(s).",
             f"The confirmation set (sha256 {decision['split']['confirmation_hash'][:12]}) has served "
             f"{decision['split']['rounds_served']} earlier dream(s).", ""]
    lines += [f"- {note}" for note in decision["split"]["notes"]]
    for name in unlabelled:
        lines.append(f"- excluded: {name} has no labels (label it with `factory history label`)")
    for view in unscorable:
        lines.append(f"- excluded: {view.id} has no scorable labelled point")
    lines += ["", "## Selection scores", "", "| World | Incumbent | " + " | ".join(
        f"Round {c['round']}" for c in candidates) + " | Mean accept-set size |",
              "| --- | --- | " + " | ".join("---" for _ in candidates) + " | --- |"]
    scored_rounds = {c["round"]: c for c in candidates}
    round_scores = {}
    for c in candidates:
        round_scores[c["round"]] = c.get("world_scores", {})
    for view in selection:
        sizes = [len(view.labels[p["id"]]["accept"]) for p in view.points()]
        cells = [_fmt(incumbent.worlds.get(view.id))] + [_fmt((scored_rounds[c["round"]].get("world_scores") or {})
                                                              .get(view.id)) for c in candidates]
        lines.append(f"| {view.id} | " + " | ".join(cells) + f" | {_fmt(sum(sizes) / len(sizes) if sizes else None)} |")
    lines.append(f"| **policy** | {_fmt(incumbent.mean)} | " + " | ".join(_fmt(c.get("score")) for c in candidates)
                 + " | |")
    lines += ["", "## Agreement by turn origin", "", "| Origin | Points | Incumbent mean score |", "| --- | --- | --- |"]
    for origin in ("cold", "warm", "auto"):
        entries = [p for p in incumbent.points if p["origin"] == origin]
        label = origin if origin != "auto" else "auto (model.decide() points, listed separately)"
        lines.append(f"| {label} | {len(entries)} | "
                     f"{_fmt(sum(p['score'] for p in entries) / len(entries) if entries else None)} |")
    lines += ["", "## Confirmation", "", "| Policy | Confirmation mean |", "| --- | --- |"]
    for name, score in confirmed.items():
        lines.append(f"| {name if name == 'incumbent' else score.name} | {_fmt(score.mean)} |")
    if not confirmed:
        lines.append("| (none) | confirmation was not scored |")
    faults: dict[tuple, dict] = {}
    for view in views:
        path = view.dir / "faults.json"
        try:
            entries = json.loads(path.read_text(encoding="utf-8")).get("faults") or []
        except (OSError, json.JSONDecodeError):
            entries = []
        for fault in entries:
            key = (fault.get("kind"), fault.get("code_family"))
            bucket = faults.setdefault(key, {"count": 0, "attempts": 0, "tokens": 0, "runs": set()})
            bucket["count"] += 1
            bucket["attempts"] += int(fault.get("attempts") or 0)
            bucket["tokens"] += int(fault.get("tokens") or 0)
            bucket["runs"].add(view.id)
    lines += ["", "## Faults the foreman cannot fix", ""]
    if faults:
        lines += ["| Kind | Code family | Count | Attempts | Tokens | Runs |", "| --- | --- | --- | --- | --- | --- |"]
    else:
        lines.append("The labeller recorded no faults in these worlds.")
    for (kind, family), bucket in sorted(faults.items(), key=lambda item: (-item[1]["count"], -item[1]["tokens"])):
        lines.append(f"| {kind} | {family} | {bucket['count']} | {bucket['attempts']} | {bucket['tokens']} | "
                     f"{len(bucket['runs'])} |")
    lines += ["", "## Candidates", ""]
    for c in candidates:
        lines.append(f"### Round {c['round']}")
        lines.append("")
        if c.get("rejected"):
            lines.append(f"Rejected by `{c['rejected'][0]}`: {c['rejected'][1]}")
        else:
            lines.append(f"Selection score {_fmt(c.get('score'))}" + (" (partial)" if c.get("partial") else ""))
        if c.get("diff"):
            # Four backticks: the skill's own examples carry three-backtick fences.
            lines += ["", "````diff", c["diff"].rstrip("\n"), "````"]
        lines.append("")
    path = dream_dir / "report.md"
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    return path


# --- the deploy gate -------------------------------------------------------------------------

def gate(*, candidate: dict | None, confirmation: dict, confirmation_worlds: list, partial: bool,
         cfg: config.Config) -> tuple[str | None, str | None]:
    """Why a candidate may not deploy, as (reason, cause); (None, None) when it clears every gate."""
    if partial:
        return "partial", "the call cap was reached mid-round"
    points = sum(len(v.points()) for v in confirmation_worlds)
    repos = {v.repository for v in confirmation_worlds}
    if not confirmation_worlds or points == 0:
        return "floor", "no scorable confirmation point"
    if len(confirmation_worlds) < cfg.dream_floor_worlds or len(repos) < cfg.dream_floor_repos:
        return "floor", (f"{len(confirmation_worlds)} confirmation world(s) from {len(repos)} repositor"
                         f"{'y' if len(repos) == 1 else 'ies'}; the floor is {cfg.dream_floor_worlds} from "
                         f"{cfg.dream_floor_repos}")
    if candidate is None:
        return "incumbent", "no candidate beat the incumbent on selection"
    required = max(cfg.dream_margin, 1 / points)
    old, new = confirmation["incumbent"].mean, confirmation["candidate"].mean
    if old is None or new is None or new - old < required - 1e-9:
        return "margin", f"confirmation {_fmt(old)} -> {_fmt(new)}, needs a gain of {required:.3f}"
    return None, None



# --- the deploy ------------------------------------------------------------------------------

DEPLOY_SUBJECT = "feat(foreman): dream "
SKILL_FILES = ("factory/skills/foreman/SKILL.md", "plugins/factory/skills/foreman/SKILL.md")
REALIGN = "codex plugin add factory@nurbot"


def _git(root: Path, *args: str, timeout: float = 120) -> tuple[int, str]:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 127, str(error)
    return result.returncode, (result.stdout + result.stderr).strip()


def _run(root: Path, argv: list[str], timeout: float = 1800) -> tuple[int, str]:
    try:
        result = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=timeout,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 127, str(error)
    return result.returncode, (result.stdout + result.stderr).strip()


def push_cause(output: str) -> str:
    text = output.lower()
    if "non-fast-forward" in text or "[rejected]" in text or "fetch first" in text:
        return "non-fast-forward"
    if any(s in text for s in ("could not resolve", "unable to access", "connection", "timed out",
                               "does not appear to be a git repository", "could not read from remote")):
        return "network"
    return output.splitlines()[-1][:300] if output else "git push failed"


def unpushed_deploys(root: Path) -> list[str]:
    """Deploy commits on main that origin does not have, by the local tracking ref a failed push left behind."""
    code, out = _git(root, "log", "--format=%H %s", "origin/main..main")
    if code != 0:
        return []
    return [line.split(" ", 1)[0] for line in out.splitlines() if line.split(" ", 1)[-1].startswith(DEPLOY_SUBJECT)]


def _restore(root: Path, originals: dict[str, bytes | None]) -> None:
    for relative, content in originals.items():
        path = root / relative
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    _git(root, "reset", "-q", "--", *originals)


def deploy(*, dream_dir: Path, candidate: dict | None, confirmation: dict, confirmation_worlds: list,
           partial: bool, cfg: config.Config, root: Path, enabled: bool = True, echo=print) -> dict:
    """Gate the best candidate and, when it clears every gate, commit it to main and push it to origin.

    Every repository path resolves under `root`. A failure before the commit leaves the working tree as it
    was; a failed push leaves the commit, and the next dream pushes it again before adding anything new.
    """
    root = Path(root)
    result: dict = {"deployed": False, "reason": None}
    if candidate is not None:
        result["source_hash"] = hashlib.sha256(candidate["text"].encode("utf-8")).hexdigest()
        result["generated_hash"] = provenance.tree_policy(candidate["tree"])["sha256"]
    pending = unpushed_deploys(root) if enabled else []
    if pending:
        code, out = _git(root, "push", "origin", "main")
        if code != 0:
            return {**result, "reason": "push_failed", "cause": push_cause(out), "pending": pending}
        return {**result, "reason": "pushed_pending", "cause": f"pushed {len(pending)} earlier deploy commit(s) "
                                                              "first; no new deploy this round", "pending": pending}
    reason, cause = gate(candidate=candidate, confirmation=confirmation, confirmation_worlds=confirmation_worlds,
                         partial=partial, cfg=cfg)
    if reason:
        return {**result, "reason": reason, "cause": cause}
    if not enabled:
        return {**result, "reason": "no_deploy", "cause": "--no-deploy"}
    code, branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0 or branch != "main":
        return {**result, "reason": "branch", "cause": f"main is not checked out ({branch})"}
    from runner import worktree as wt
    try:
        dirty = wt.dirty_tracked(root)
    except wt.GitError as error:
        return {**result, "reason": "dirty", "cause": str(error)}
    if dirty:
        return {**result, "reason": "dirty", "cause": "tracked changes: " + ", ".join(dirty[:10])}
    builder = [sys.executable, str(root / "scripts" / "build_codex_plugin.py"), "--plugin", "factory"]
    code, out = _run(root, builder + ["--check"])
    if code != 0:
        return {**result, "reason": "stale_plugins", "cause": out.splitlines()[-1][:300] if out else f"exit {code}"}
    originals = {relative: (root / relative).read_bytes() if (root / relative).is_file() else None
                 for relative in SKILL_FILES}
    (root / SKILL_FILES[0]).write_text(candidate["text"], encoding="utf-8")
    code, out = _run(root, builder)
    if code != 0:
        _restore(root, originals)
        return {**result, "reason": "build_failed", "cause": out[-300:]}
    code, out = _run(root, ["bash", str(root / "scripts" / "validate.sh")])
    if code != 0:
        _restore(root, originals)
        return {**result, "reason": "validate", "cause": out[-300:]}
    generated = provenance.policy_identity({"resolution": "direct-path"}, root=root)["sha256"]
    old, new = confirmation["incumbent"].mean, confirmation["candidate"].mean
    body = "\n".join([
        f"The dream loop's {candidate['name']} beat the incumbent foreman skill on confirmation worlds it never saw.",
        "", "| Policy | Confirmation mean |", "| --- | --- |",
        f"| incumbent | {_fmt(old)} |", f"| {candidate['name']} | {_fmt(new)} |", "",
        f"Dream: {dream_dir}", f"Source policy hash: {result['source_hash']}", f"Generated policy hash: {generated}",
        "", "Roll back with `git revert` of this commit."])
    _git(root, "add", "--", *SKILL_FILES)
    code, out = _git(root, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m",
                     f"{DEPLOY_SUBJECT}{dream_dir.name} raises confirmation score {_fmt(old)} -> {_fmt(new)}",
                     "-m", body, "--", *SKILL_FILES)
    if code != 0:
        _restore(root, originals)
        return {**result, "reason": "commit_failed", "cause": out[-300:], "generated_hash": generated}
    _, commit = _git(root, "rev-parse", "HEAD")
    result.update({"generated_hash": generated, "commit": commit})
    code, out = _git(root, "push", "origin", "main")
    if code != 0:
        return {**result, "reason": "push_failed", "cause": push_cause(out)}
    echo(f"Deployed {commit[:12]}; make the installed plugin match again with `{REALIGN}`.")
    return {**result, "deployed": True, "reason": "deployed", "realign": REALIGN}
