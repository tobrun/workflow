#!/usr/bin/env python3
"""Run unittest tests repeatedly in shuffled order and report any test whose outcome disagrees between runs.

    cd pkg && python3 ../tools/harden/flaky.py --runs 5 tests.test_parser tests.test_cache

Each run shuffles module, class, and in-class test order with its own seed (printed, so a disagreement can be
replayed); classes stay grouped. All runs share one process, so state leaked by one run is visible to the next.
Skipped tests are excluded; fixture errors fail every test in their class or module. Exit 1 when any test is
flaky or fails every time, or no tests are found.
"""

import argparse
import os
import random
import re
import sys
import unittest

# Run from the directory the test modules import from, like `python3 -m unittest`.
sys.path.insert(0, os.getcwd())


def flatten(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    tests = []
    for item in suite:
        tests += flatten(item) if isinstance(item, unittest.TestSuite) else [item]
    return tests


def shuffled_suite(tests: list[unittest.TestCase], rng: random.Random) -> unittest.TestSuite:
    """Shuffle module order, class order within a module, and test order within a class; classes stay grouped so
    setUpClass and setUpModule run once per run, as they do in a normal run."""
    modules: dict[str, dict[type, list[unittest.TestCase]]] = {}
    for test in tests:
        modules.setdefault(type(test).__module__, {}).setdefault(type(test), []).append(test)
    suite = unittest.TestSuite()
    module_names = list(modules)
    rng.shuffle(module_names)
    for name in module_names:
        classes = list(modules[name].values())
        rng.shuffle(classes)
        for members in classes:
            rng.shuffle(members)
            suite.addTest(unittest.TestSuite(members))
    return suite


def fixture_failures(result: unittest.TestResult, tests: list[unittest.TestCase]) -> tuple[set[str], set[str]]:
    """Ids of tests that failed, plus fixture errors (setUpClass, setUpModule, ...) no test could be attributed to.

    A fixture error arrives as an `_ErrorHolder` whose id is `setUpClass (pkg.mod.Class)`; it fails every test in
    that class or module.
    """
    failed: set[str] = set()
    orphans: set[str] = set()
    known = {t.id() for t in tests}
    for test, _ in result.failures + result.errors:
        if test.id() in known:
            failed.add(test.id())
            continue
        match = re.search(r"\((.+)\)$", test.id())
        scope = match.group(1) if match else test.id()
        hit = {i for i in known if i.startswith(scope + ".")}
        failed |= hit
        if not hit:
            orphans.add(test.id())
    failed |= {t.id() for t in result.unexpectedSuccesses}
    return failed, orphans


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("modules", nargs="+")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260918)
    args = parser.parse_args()
    if args.runs < 2:
        parser.error("--runs must be at least 2 to compare outcomes")
    outcomes: dict[str, list[str]] = {}
    skipped: set[str] = set()
    orphans: set[str] = set()
    for run in range(args.runs):
        seed = args.seed + run
        tests = flatten(unittest.defaultTestLoader.loadTestsFromNames(args.modules))
        if not tests:
            print("no tests found in: " + " ".join(args.modules), file=sys.stderr)
            return 1
        result = unittest.TestResult()
        shuffled_suite(tests, random.Random(seed)).run(result)
        skips = {t.id() for t, _ in result.skipped}
        skipped |= skips
        failed, bad = fixture_failures(result, tests)
        orphans |= bad
        for test in tests:
            if test.id() not in skips:
                outcomes.setdefault(test.id(), []).append("fail" if test.id() in failed else "pass")
        print(f"run {run + 1} seed {seed}: {result.testsRun} tests, {len(failed) + len(bad)} failed, "
              f"{len(skips)} skipped", flush=True)
    flaky = {t: o for t, o in outcomes.items() if len(set(o)) > 1}
    broken = {t: o for t, o in outcomes.items() if set(o) == {"fail"}}
    for test, seen in sorted(flaky.items()):
        print(f"FLAKY  {test}: {' '.join(seen)}")
    for test in sorted(broken):
        print(f"FAILS  {test}")
    for holder in sorted(orphans):
        print(f"FIXTURE ERROR  {holder}")
    print(f"{len(outcomes)} tests x {args.runs} runs: {len(flaky)} flaky, {len(broken)} always failing, "
          f"{len(skipped)} skipped (excluded), {len(orphans)} unattributed fixture errors")
    return 1 if flaky or broken or orphans else 0


if __name__ == "__main__":
    sys.exit(main())
