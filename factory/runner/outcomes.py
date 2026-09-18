"""Portable run exports, post-PR outcome observations, and cross-run summaries.

An export is a directory of copied run artifacts plus `manifest.json` (`factory.export/1`) listing every
file by relative path with its SHA-256 and size, so it stays verifiable after moving it to another machine.
Exports live under `~/.factory/exports/{run-id}/`, which `factory gc` never removes; gc exports a merged run
before archiving it. Outcomes are appended to the run's `outcomes.jsonl` (`factory.outcome/1`) and never
rewrite `run.json`, so the revision and evidence that satisfied completion stay as recorded.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path

from runner import records
from runner.model import atomic_write, utc_now

EXPORT_SCHEMA = "factory.export/1"
OUTCOME_SCHEMA = "factory.outcome/1"
OUTCOMES = "outcomes.jsonl"
ANNOTATIONS = ("rework", "rejection", "regression", "intervention")
# Runner-owned records and selected evidence; raw host streams (stdout.jsonl) stay local because they are large
# and may contain unredacted tool output.
RUN_FILES = ("run.json", "events.jsonl", "request.md", OUTCOMES)
RUN_TREES = ("intent", "checkpoints", "reports", "evidence", "plan", "foreman")
ATTEMPT_FILES = ("gate.json", "receipt.json", "runtime.json", "outputs.json", "request.json", "executor.json",
                 "last-message.md", "subphase.json", "stderr.log")
RAW_STREAMS = ("stdout.jsonl",)
ATTEMPT_TREES = ("gate", "inputs")


class ExportError(Exception):
    pass


def selected(run_dir: Path) -> list[Path]:
    chosen = [run_dir / name for name in RUN_FILES if (run_dir / name).is_file()]
    for tree in RUN_TREES:
        chosen += sorted(p for p in (run_dir / tree).rglob("*") if p.is_file() and p.name not in RAW_STREAMS)
    for attempt in sorted(p for p in (run_dir / "attempts").glob("*") if p.is_dir()):
        chosen += [attempt / name for name in ATTEMPT_FILES if (attempt / name).is_file()]
        for tree in ATTEMPT_TREES:
            chosen += sorted(p for p in (attempt / tree).rglob("*") if p.is_file())
    return [p for p in chosen if not p.is_symlink()]


def export(run_dir: Path, run, dest: Path) -> dict:
    """Copy the run's retained evidence to `dest` and write a checksummed manifest; returns the manifest."""
    staging = dest.with_name(dest.name + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    files = {}
    for source in selected(run_dir):
        relative = source.relative_to(run_dir).as_posix()
        target = staging / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        files[relative] = {"sha256": records.sha256_file(target), "bytes": target.stat().st_size}
    data = run.data
    manifest = {"schema": EXPORT_SCHEMA, "run": run.id, "exported_at": utc_now(), "status": data.get("status"),
                "plan": data.get("plan"), "branch": data.get("branch"), "base_sha": data.get("base_sha"),
                "pr": {k: (data.get("pr") or {}).get(k) for k in ("number", "url")},
                "completion": data.get("completion"), "outcome": latest_outcome(run_dir), "files": files}
    staging.mkdir(parents=True, exist_ok=True)
    atomic_write(staging / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    shutil.rmtree(dest, ignore_errors=True)
    staging.rename(dest)
    return manifest


def digest(bundle: Path) -> str:
    return records.sha256_file(bundle / "manifest.json")


def verify(bundle: Path) -> list[str]:
    """Problems with an export bundle; empty when every listed file resolves and matches its checksum."""
    try:
        manifest = records.load_json_file(bundle / "manifest.json", max_bytes=16 * 1024 * 1024)
    except (OSError, records.RecordError) as error:
        return [f"manifest.json: {error}"]
    if not isinstance(manifest, dict) or manifest.get("schema") != EXPORT_SCHEMA:
        return [f"manifest.json: schema must be {EXPORT_SCHEMA}"]
    problems = []
    for relative, entry in sorted(manifest.get("files", {}).items()):
        path = bundle / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            problems.append(f"{relative}: path escapes the bundle")
        elif not path.is_file():
            problems.append(f"{relative}: missing")
        elif records.sha256_file(path) != entry.get("sha256"):
            problems.append(f"{relative}: checksum mismatch")
    return problems


def pr_comment(manifest: dict, bundle_digest: str) -> str:
    completion = manifest.get("completion") or {}
    return "\n".join([
        "Factory run evidence",
        "",
        f"- Run: `{manifest['run']}` ({manifest['status']})",
        f"- Completed at revision: `{completion.get('revision') or 'not recorded'}`",
        f"- Evidence bundle: {len(manifest['files'])} files, manifest SHA-256 `{bundle_digest}`",
        f"- Verify a copy with `factory export --verify <bundle>`; the operator retains it under "
        f"`~/.factory/exports/{manifest['run']}/`.",
    ]) + "\n"


def read_outcomes(run_dir: Path) -> list[dict]:
    path = run_dir / OUTCOMES
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("schema") == OUTCOME_SCHEMA:
            entries.append(entry)
    return entries


def latest_outcome(run_dir: Path) -> dict | None:
    observations = [e for e in read_outcomes(run_dir) if e["kind"] == "observation"]
    return observations[-1] if observations else None


def append(run_dir: Path, entry: dict) -> dict:
    entry = {"schema": OUTCOME_SCHEMA, "at": utc_now(), **entry}
    with (run_dir / OUTCOMES).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def classify(info: dict | None) -> str:
    if not isinstance(info, dict):
        return "unknown"
    if info.get("state") == "MERGED" or info.get("mergedAt"):
        return "merged"
    return {"CLOSED": "closed-unmerged", "OPEN": "open"}.get(info.get("state"), "unknown")


def observation(run, info: dict | None, problem: str | None) -> dict:
    completion = run.data.get("completion") or {}
    return {"kind": "observation", "state": classify(info), "problem": problem,
            "head": (info or {}).get("headRefOid"), "merged_at": (info or {}).get("mergedAt"),
            "review_decision": (info or {}).get("reviewDecision"),
            "completion_revision": completion.get("revision"),
            "moved_since_completion": bool(info and completion.get("revision") and info.get("headRefOid")
                                           and info["headRefOid"] != completion["revision"])}


def seconds_between(start: str | None, end: str | None) -> float | None:
    from datetime import datetime
    try:
        return (datetime.fromisoformat(end.replace("Z", "+00:00"))
                - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
    except (AttributeError, ValueError):
        return None


def summarize(runs: list[tuple[Path, object]]) -> dict:
    """Cross-run rates, each with its sample size; unknowns are counted, never imputed."""
    terminal = [(d, r) for d, r in runs if r.status in ("done", "cancelled")]
    done = [(d, r) for d, r in terminal if r.status == "done"]
    intervened, reworked, regressed, rejected = set(), set(), set(), set()
    active_minutes, active_known = 0.0, 0
    states = Counter()
    failures = Counter()
    tokens = 0
    cost, cost_known = 0.0, 0
    scope_seconds = 0.0
    for run_dir, run in runs:
        entries = read_outcomes(run_dir)
        kinds = {e.get("annotation") for e in entries if e["kind"] == "annotation"}
        events = (run_dir / "events.jsonl").read_text(encoding="utf-8") if (run_dir / "events.jsonl").is_file() else ""
        if "intervention" in kinds or '"event": "run.needs_human"' in events:
            intervened.add(run.id)
        for name, bucket in (("rework", reworked), ("regression", regressed), ("rejection", rejected)):
            if name in kinds:
                bucket.add(run.id)
        minutes = [e["active_minutes"] for e in entries if isinstance(e.get("active_minutes"), (int, float))]
        if minutes:
            active_known += 1
            active_minutes += sum(minutes)
        latest = latest_outcome(run_dir)
        if run.status == "done":
            states[latest["state"] if latest else "not observed"] += 1
        for attempt in run.data.get("attempts", []):
            if attempt.get("outcome") not in (None, "success"):
                failures[attempt.get("code") or attempt.get("source") or "unclassified"] += 1
            tokens += attempt.get("tokens") or 0
            amount = (attempt.get("cost") or {}).get("amount")
            if amount is not None:
                cost += amount
                cost_known += 1
            if attempt.get("stage") == "scope":
                scope_seconds += seconds_between(attempt.get("started_at"), attempt.get("ended_at")) or 0

    def rate(part: int, whole: int) -> dict:
        return {"count": part, "n": whole, "rate": round(part / whole, 3) if whole else None}

    legacy = [r.id for _, r in done if not r.data.get("completion")]
    return {"runs": len(runs), "success": rate(len(done), len(terminal)),
            "legacy_done": {"count": len(legacy), "note": "done before completion observations were recorded; "
                                                          "historical, not reverified"},
            "intervention": rate(len(intervened), len(runs)), "rework": rate(len(reworked), len(done)),
            "rejection": rate(len(rejected), len(done)), "regression": rate(len(regressed), len(done)),
            "pr_states": dict(states), "failure_categories": dict(failures.most_common()),
            "tokens": tokens, "cost": {"amount": round(cost, 4), "attempts_priced": cost_known},
            "active_operator_minutes": {"total": active_minutes, "runs_measured": active_known,
                                        "runs_unknown": len(runs) - active_known},
            "elapsed_interactive_scope_seconds": scope_seconds}


def turn_policies(run_dir: Path) -> dict[int, str]:
    """The policy hash each foreman turn recorded, by turn number; turns from before the hash are absent."""
    hashes = {}
    for record in sorted((run_dir / "foreman" / "turns").glob("*/decision.json")):
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sha = ((data or {}).get("policy") or {}).get("sha256") if isinstance(data, dict) else None
        if sha and str(record.parent.name).isdigit():
            hashes[int(record.parent.name)] = sha
    return hashes


def by_policy(runs: list[tuple[Path, object]]) -> dict:
    """Intervention, override, and fallback rates per foreman policy hash; runs and turns without one are `unknown`.

    A run that changed policy mid-way (a session restart after a deploy) counts in each group it ran under.
    """
    groups: dict[str, dict] = {}

    def group(sha: str) -> dict:
        return groups.setdefault(sha, {"runs": set(), "intervened": set(), "turns": 0, "fallbacks": 0, "overrides": 0})

    for run_dir, run in runs:
        hashes = turn_policies(run_dir)
        decisions = run.data.get("decisions") or []
        overridden = {entry.get("turn") for entry in run.data.get("overrides") or []}
        events = (run_dir / "events.jsonl").read_text(encoding="utf-8") if (run_dir / "events.jsonl").is_file() else ""
        intervened = run.status == "needs-human" or '"event": "run.needs_human"' in events
        seen = set()
        for decision in decisions:
            sha = hashes.get(decision.get("turn"), "unknown")
            entry = group(sha)
            entry["turns"] += 1
            entry["fallbacks"] += decision.get("source") == "fallback"
            entry["overrides"] += decision.get("turn") in overridden
            seen.add(sha)
        for sha in seen or {"unknown"}:
            group(sha)["runs"].add(run.id)
            if intervened:
                group(sha)["intervened"].add(run.id)

    def rate(part: int, whole: int) -> dict:
        return {"count": part, "n": whole, "rate": round(part / whole, 3) if whole else None}

    return {sha: {"runs": len(entry["runs"]), "turns": entry["turns"],
                  "intervention": rate(len(entry["intervened"]), len(entry["runs"])),
                  "override": rate(entry["overrides"], entry["turns"]),
                  "fallback": rate(entry["fallbacks"], entry["turns"])}
            for sha, entry in sorted(groups.items())}
