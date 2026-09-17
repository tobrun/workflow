#!/usr/bin/env python3
"""Run the foreman eval cases against the real Codex foreman and report action agreement. Paid.

    python3 factory/evals/foreman/run.py [--case NAME ...] [--model openai.gpt-5.6-luna] [--effort medium]

Each case gets one cold-start turn: the case digest, the event message, and the foreman skill, with the
same `--output-schema` the runner uses. A case passes when the decision's action is in `accept` and not
in `reject`, and when no override is recorded unless `expected.json` allows one. Results land in
`--out` (default a temporary directory) as `<case>/decision.json`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from runner import hosts, records  # noqa: E402

CASES = Path(__file__).resolve().parent / "cases"
SKILL = ROOT.parent / "plugins" / "factory" / "skills" / "foreman" / "SKILL.md"


def prompt_for(case: Path, digest_path: Path) -> str:
    event = (case / "event.md").read_text(encoding="utf-8")
    lines = [f"Follow the skill at {SKILL}.",
             "Read it and the references it links from that directory; do not use an installed factory plugin.",
             "",
             "You are the foreman of a factory run replayed for evaluation: the worktree is not available, so decide",
             f"from the digest at {digest_path} and the event below alone. Then answer the event.", ""]
    return "\n".join(lines) + event


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", action="append", help="case name under cases/ (default: all)")
    parser.add_argument("--model", default="openai.gpt-5.6-luna")
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--out")
    args = parser.parse_args()
    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="foreman-eval-"))
    names = args.case or sorted(p.name for p in CASES.iterdir() if p.is_dir())
    schema = out / "decision.schema.json"
    out.mkdir(parents=True, exist_ok=True)
    schema.write_text(json.dumps(records.decision_json_schema(), indent=2) + "\n", encoding="utf-8")
    passed = 0
    for name in names:
        case = CASES / name
        expected = json.loads((case / "expected.json").read_text(encoding="utf-8"))
        work = out / name
        work.mkdir(parents=True, exist_ok=True)
        digest_path = work / "digest.json"
        digest_path.write_text((case / "digest.json").read_text(encoding="utf-8"), encoding="utf-8")
        last = work / "last-message.md"
        argv = hosts.codex_foreman_argv(prompt=prompt_for(case, digest_path), model=args.model, effort=args.effort,
                                        sandbox="read-only", worktree=work, writable=[], last_message=last, schema=schema)
        with open(work / "stdout.jsonl", "wb") as stdout, open(work / "stderr.log", "wb") as stderr:
            code = subprocess.run(argv, cwd=work, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL).returncode
        decision, problem = None, None
        try:
            decision = records.validate_decision(json.loads(last.read_text(encoding="utf-8")), name=name)
        except (OSError, ValueError, records.RecordError) as error:
            problem = f"{type(error).__name__}: {error}"
        ok = decision is not None and decision["action"] in expected["accept"] \
            and decision["action"] not in expected.get("reject", []) \
            and (decision.get("override") is None or expected.get("allow_override"))
        passed += ok
        (work / "decision.json").write_text(json.dumps({"case": name, "exit_code": code, "decision": decision,
                                                        "problem": problem, "passed": ok, "expected": expected},
                                                       indent=2) + "\n", encoding="utf-8")
        verdict = "PASS" if ok else "FAIL"
        action = decision["action"] if decision else f"no decision ({problem})"
        print(f"{verdict}  {name}: {action}" + (f" - {decision['summary']}" if decision else ""))
    print(f"{passed}/{len(names)} cases agree; details in {out}")
    return 0 if passed == len(names) else 1


if __name__ == "__main__":
    sys.exit(main())
