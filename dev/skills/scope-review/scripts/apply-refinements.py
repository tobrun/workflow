#!/usr/bin/env python3
"""Apply independent refinement proposals through one checked spec writer.

Usage: apply-refinements.py spec.md proposal.json ...
Every proposal names the same baseSha256 and an array of {old, new} excerpts.
Excerpts must match once and never overlap; stale or ambiguous batches write
nothing. Semantic authority and spec lint remain the orchestrator's job.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path


def apply(spec: Path, proposals: list[Path]) -> None:
    original = spec.read_bytes()
    text = original.decode("utf-8")
    digest = hashlib.sha256(original).hexdigest()
    edits = []
    for path in proposals:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("baseSha256") != digest:
            raise ValueError(f"{path}: proposal is not based on the current spec")
        changes = payload.get("changes")
        if not isinstance(changes, list) or not changes:
            raise ValueError(f"{path}: changes must be a non-empty array")
        for change in changes:
            if not isinstance(change, dict):
                raise ValueError(f"{path}: change must be an object")
            old, new = change.get("old"), change.get("new")
            if not isinstance(old, str) or not old or not isinstance(new, str):
                raise ValueError(f"{path}: old must be non-empty text and new must be text")
            if text.count(old) != 1:
                raise ValueError(f"{path}: old excerpt must match exactly once")
            start = text.index(old)
            edits.append((start, start + len(old), new))
    edits.sort()
    for left, right in zip(edits, edits[1:]):
        if right[0] < left[1]:
            raise ValueError("overlapping refinements; reconcile them before applying")
    updated = text
    for start, end, new in reversed(edits):
        updated = updated[:start] + new + updated[end:]
    if spec.read_bytes() != original:
        raise ValueError("spec changed during refinement; regenerate proposals")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=spec.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(updated)
    try:
        os.chmod(temporary, spec.stat().st_mode & 0o777)
        temporary.replace(spec)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        apply(Path(argv[1]), [Path(path) for path in argv[2:]])
    except (OSError, ValueError, TypeError) as error:
        print(f"refinement: {error}", file=sys.stderr)
        return 1
    print(f"{argv[1]}: applied {len(argv) - 2} proposal(s); run spec lint and verify authority")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
