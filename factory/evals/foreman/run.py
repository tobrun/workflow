#!/usr/bin/env python3
"""Run the foreman eval cases against the real Codex foreman and report action agreement. Paid.

    python3 factory/evals/foreman/run.py [--case NAME ...] [--model MODEL] [--effort EFFORT]

A thin wrapper over the dream loop's replay: each case becomes a point directory named after it (the case
digest and event message), replayed cold under the generated foreman skill by `dream.replay` and scored by
`dream.score_point` against `expected.json`. A case passes when the decision's action is accepted and no
override is recorded unless `expected.json` allows one. `--model` and `--effort` default to the configured
`foreman_model` and `foreman_effort`. Results land in `--out` (default a temporary directory) as
`<case>/decision.json`.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from runner import config, dream  # noqa: E402

CASES = Path(__file__).resolve().parent / "cases"


def main() -> int:
    cfg = config.load()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", action="append", help="case name under cases/ (default: all)")
    parser.add_argument("--model", default=cfg.foreman_model)
    parser.add_argument("--effort", default=cfg.foreman_effort)
    parser.add_argument("--out")
    args = parser.parse_args()
    cfg = dataclasses.replace(cfg, foreman_model=args.model, foreman_effort=args.effort)
    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="foreman-eval-"))
    names = args.case or sorted(p.name for p in CASES.iterdir() if p.is_dir())
    passed = 0
    for name in names:
        case = CASES / name
        expected = json.loads((case / "expected.json").read_text(encoding="utf-8"))
        point = out / "points" / name
        point.mkdir(parents=True, exist_ok=True)
        for filename in ("digest.json", "event.md"):
            shutil.copyfile(case / filename, point / filename)
        answer = dream.replay(point, dream.GENERATED, cfg, None, world="eval", work=out / name)
        ok = dream.score_point(answer.decision, expected) > 0
        passed += ok
        (out / name / "decision.json").write_text(json.dumps({
            "case": name, "decision": answer.decision, "problem": answer.problem, "passed": ok,
            "expected": expected}, indent=2) + "\n", encoding="utf-8")
        action = answer.decision["action"] if answer.decision else f"no decision ({answer.problem})"
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {action}"
              + (f" - {answer.decision['summary']}" if answer.decision else ""))
    print(f"{passed}/{len(names)} cases agree; details in {out}")
    return 0 if passed == len(names) else 1


if __name__ == "__main__":
    sys.exit(main())
