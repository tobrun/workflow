#!/usr/bin/env python3
"""Run unittest tests repeatedly in shuffled order and report any test whose outcome disagrees between runs.

    cd pkg && python3 ../tools/harden/flaky.py --runs 5 tests.test_parser tests.test_cache

Each run shuffles the test order with its own seed (printed, so a disagreement can be replayed). Exit 1 when any
test is flaky or fails every time.
"""

import argparse
import os
import random
import sys
import unittest

# Run from the directory the test modules import from, like `python3 -m unittest`.
sys.path.insert(0, os.getcwd())


def ids(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    tests = []
    for item in suite:
        tests += ids(item) if isinstance(item, unittest.TestSuite) else [item]
    return tests


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("modules", nargs="+")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260918)
    args = parser.parse_args()
    outcomes: dict[str, list[str]] = {}
    for run in range(args.runs):
        seed = args.seed + run
        tests = ids(unittest.defaultTestLoader.loadTestsFromNames(args.modules))
        random.Random(seed).shuffle(tests)
        result = unittest.TestResult()
        unittest.TestSuite(tests).run(result)
        failed = {t.id() for t, _ in result.failures + result.errors}
        for test in tests:
            outcomes.setdefault(test.id(), []).append("fail" if test.id() in failed else "pass")
        print(f"run {run + 1} seed {seed}: {result.testsRun} tests, {len(failed)} failed", flush=True)
    flaky = {t: o for t, o in outcomes.items() if len(set(o)) > 1}
    broken = {t: o for t, o in outcomes.items() if set(o) == {"fail"}}
    for test, seen in sorted(flaky.items()):
        print(f"FLAKY  {test}: {' '.join(seen)}")
    for test in sorted(broken):
        print(f"FAILS  {test}")
    print(f"{len(outcomes)} tests x {args.runs} runs: {len(flaky)} flaky, {len(broken)} always failing")
    return 1 if flaky or broken else 0


if __name__ == "__main__":
    sys.exit(main())
