"""Checkpoints for expensive gate subphases: reuse only what is still valid, recompute the rest.

A checkpoint records one completed subphase: its name, a fingerprint of every input that
decides its outcome, the revision, output file hashes, and the receipt that proved it.
A later gate (a retry, the ship gate after build, or a recovery) reuses it only when the
fingerprint is identical and every output still matches its hash; otherwise it recomputes
and says why. Nothing here trusts an agent: checkpoints live in the runner-owned run
directory and record runner executions only.

Invalidation table (what each fingerprint covers):

| Subphase | Inputs |
| --- | --- |
| tests | tree, scenario map test ids, contract `tests`, runner content |
| repro | tree, base revision, `[repro]` test ids, contract `tests`, runner content |
| e2e | tree, scenario map e2e cases, contract `e2e`, `services`, `ports`, runner content |
| validation:{id} | tree, the command record, contract boundary and environment names, runner content |
| gauntlet:{id} | tree, the command actually run (pinned or reported), runner content |
| publication | head revision, PR number, published Evidence hash |

A code change moves the tree and invalidates every verification subphase; a PR body edit
changes only the publication inputs; a contract, test command, or runner change invalidates
the subphases whose inputs it appears in.
"""

from __future__ import annotations

import json
from pathlib import Path

from runner import records
from runner import worktree as wt
from runner.model import atomic_write, utc_now

SCHEMA = "factory.checkpoint/1"
DIRECTORY = "checkpoints"


def tree(worktree: Path) -> str:
    return wt.git("-C", str(worktree), "rev-parse", "HEAD^{tree}")


def fingerprint(inputs: dict) -> str:
    return records.canonical_sha256(inputs)


def path(run_dir: Path, name: str) -> Path:
    safe = name.replace("/", "_").replace(":", "--")
    return Path(run_dir) / DIRECTORY / f"{safe}.json"


def load(run_dir: Path | None, name: str) -> dict | None:
    if run_dir is None:
        return None
    try:
        data = json.loads(path(run_dir, name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA else None


def reusable(run_dir: Path | None, name: str, inputs: dict) -> tuple[dict | None, str]:
    """(checkpoint, decision): the checkpoint when it can be reused, and a reason either way."""
    if run_dir is None:
        return None, "computed: no run directory"
    checkpoint = load(run_dir, name)
    if checkpoint is None:
        if path(run_dir, name).is_file():
            return None, "computed: checkpoint is unreadable or an unsupported version"
        return None, "computed: no checkpoint"
    if checkpoint.get("fingerprint") != fingerprint(inputs):
        changed = sorted(key for key in set(inputs) | set(checkpoint.get("inputs", {}))
                         if inputs.get(key) != checkpoint.get("inputs", {}).get(key))
        return None, f"computed: inputs changed ({', '.join(changed) or 'fingerprint'})"
    for relative, digest in checkpoint.get("outputs", {}).items():
        target = Path(relative)
        if not target.is_file() or records.sha256_file(target) != digest:
            return None, f"computed: output {target.name} is missing or altered"
    return checkpoint, f"reused: {name} from {checkpoint.get('completed_at')}"


def save(run_dir: Path | None, name: str, inputs: dict, *, revision: str, outputs: list[Path], receipt: str | None,
         data: dict) -> None:
    if run_dir is None:
        return
    record = {"schema": SCHEMA, "name": name, "fingerprint": fingerprint(inputs), "inputs": inputs,
              "revision": revision, "completed_at": utc_now(), "receipt": receipt,
              "outputs": {str(output): records.sha256_file(output) for output in outputs if Path(output).is_file()},
              "data": data}
    target = path(run_dir, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, json.dumps(record, indent=2) + "\n")


def note(ctx, name: str, decision: str) -> None:
    """Record the subphase and its reuse decision where `factory show` reads them while the gate runs."""
    ctx.checkpoint_log.append({"subphase": name, "decision": decision, "at": utc_now()})
    ctx.attempt_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(ctx.attempt_dir / "subphase.json",
                 json.dumps({"current": name, "decision": decision, "history": ctx.checkpoint_log}, indent=2) + "\n")
