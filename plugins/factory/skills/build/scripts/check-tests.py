#!/usr/bin/env python3
"""Check that every scenario of a factory run is mapped to evidence the runner can execute.

Usage: python3 check-tests.py .dev/{plan-name} [--repo-root .]

Reads the run's scenario list (`scenarios_file` in .dev/factory-run.json) and validates
`.dev/{plan-name}/scenario-map.json` with the same validator the runner's gate uses: every
present scenario mapped at its own layer, unit and integration scenarios to test ids whose
files exist, e2e scenarios to a driver case, and no test listed twice for one scenario.
Exit 0 when clean, 1 with one problem per line, 2 when the run file or scenarios are missing.

A clean map is only the precondition: the runner then executes exactly these tests and the
e2e driver itself, and a test that is not collected, is skipped, or fails does not count.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import factory_records as records  # noqa: E402


def main(argv: list[str]) -> int:
    args, repo_root = [], Path(".")
    rest = argv[1:]
    while rest:
        value = rest.pop(0)
        if value == "--repo-root" and rest:
            repo_root = Path(rest.pop(0))
        else:
            args.append(value)
    if len(args) != 1:
        print("usage: check-tests.py <plan directory> [--repo-root .]", file=sys.stderr)
        return 2
    plan = Path(args[0])
    run_file = repo_root / ".dev" / "factory-run.json"
    try:
        context = json.loads(run_file.read_text(encoding="utf-8"))
        catalog = json.loads(Path(context["scenarios_file"]).read_text(encoding="utf-8"))["scenarios"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"cannot read the run's scenarios through {run_file}: {error}", file=sys.stderr)
        return 2
    present = [s for s in catalog if s.get("present")]
    spec = plan / "spec.md"
    if spec.is_file():
        known = {(s["layer"], s["requirement"]) for s in present}
        for scenario in records.parse_scenarios(spec.read_text(encoding="utf-8")):
            if (scenario["layer"], scenario["requirement"]) not in known:
                print(f"note: [{scenario['layer']}] {scenario['requirement']} has no id yet; the runner assigns one at "
                      "the next gate, then map it")
    try:
        mapping = records.load_scenario_map(plan / "scenario-map.json", present)
    except records.RecordError as error:
        print(f"{error} [{error.code}]")
        return 1
    problems = []
    for scenario_id, entry in sorted(mapping["scenarios"].items()):
        for test in entry.get("tests", []):
            path = records.test_path(test)
            if path is not None and not (repo_root / path).is_file():
                problems.append(f"{scenario_id}: {path} does not exist")
    for message in problems:
        print(message)
    if problems:
        print(f"\n{len(problems)} problem(s); the runner will not find these tests.")
        return 1
    print(f"{plan}: clean - {len(present)} scenario(s) mapped; the runner executes them at the gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
