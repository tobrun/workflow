#!/usr/bin/env python3
"""Capture a foreman eval case from a real run: the digest and event as they stood after one attempt.

    python3 factory/evals/foreman/capture.py ~/.factory/runs/<id> --stage ship --attempt 4 --name two-ideas-ship-4 \
        --accept publish repair --reject park cancel

The case holds `digest.json` (attempts up to and including the named one), `event.md` (the attempt.finished
message the foreman would have received), and `expected.json`. Repository identity is redacted: run and
worktree paths, the repository path, GitHub URLs, and the run id become placeholders, so a case can be committed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from runner import config, foreman, gates  # noqa: E402
from runner.history import as_of, redact  # noqa: E402
from runner.model import Run  # noqa: E402

STAGES = ("scope", "scope-review", "build", "ship")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir")
    parser.add_argument("--stage", required=True, choices=STAGES[1:])
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--accept", nargs="+", required=True, help="actions a correct foreman may choose")
    parser.add_argument("--reject", nargs="*", default=[], help="actions that would be wrong here")
    parser.add_argument("--note", default="", help="one line on why this case matters")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent / "cases"))
    args = parser.parse_args()
    run = Run.load(Path(args.run_dir).expanduser())
    snapshot = as_of(run, args.stage, args.attempt)
    attempt = snapshot.data["attempts"][-1]
    cfg = config.parse({"foreman": "codex"})
    digest = foreman.digest(snapshot, cfg)
    outcome = gates.Outcome(attempt["outcome"], attempt.get("reason"), bool(attempt.get("retryable")),
                            attempt.get("source") or "runner", warning=attempt.get("warning"),
                            code=attempt.get("code"), conditions=list(attempt.get("conditions") or []))
    event = foreman.attempt_event(snapshot, args.stage, attempt, outcome)
    message = foreman.event_message(snapshot, cfg, event)
    case = Path(args.out) / args.name
    case.mkdir(parents=True, exist_ok=True)
    (case / "digest.json").write_text(redact(json.dumps(digest, indent=2, default=str), run) + "\n", encoding="utf-8")
    (case / "event.md").write_text(redact(message, run), encoding="utf-8")
    (case / "expected.json").write_text(json.dumps({
        "schema": "factory.foreman-eval/1", "stage": args.stage, "attempt": args.attempt, "note": args.note,
        "accept": args.accept, "reject": args.reject, "source_run": "<run_id>"}, indent=2) + "\n", encoding="utf-8")
    print(f"captured {case}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
