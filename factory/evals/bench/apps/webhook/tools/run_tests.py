"""Run unittest tests by `path::Class::method` id and report factory.test-results/1."""
import argparse
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.getcwd())


def summary(trace):
    lines = [line for line in trace.strip().splitlines() if re.match(r"^[A-Za-z_.]*(Error|Exception)\b", line)]
    return lines[-1] if lines else trace.strip().splitlines()[-1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True)
    parser.add_argument("ids", nargs="*")
    args = parser.parse_args()
    reported = []
    for test_id in args.ids:
        path, klass, method = test_id.split("::")
        module = path[:-3].replace("/", ".")
        try:
            suite = unittest.defaultTestLoader.loadTestsFromName(f"{module}.{klass}.{method}")
        except AttributeError:
            continue
        except Exception as error:
            reported.append({"id": test_id, "outcome": "error", "detail": f"{type(error).__name__}: {error}"})
            continue
        failed_load = [test for test in suite if type(test).__name__ == "_FailedTest"] if hasattr(suite, "__iter__") \
            else ([suite] if type(suite).__name__ == "_FailedTest" else [])
        if failed_load:
            error = failed_load[0]._exception
            if isinstance(error, AttributeError):
                continue
            reported.append({"id": test_id, "outcome": "error", "detail": summary(str(error))})
            continue
        result = unittest.TestResult()
        suite.run(result)
        if result.errors:
            outcome, detail = "error", summary(result.errors[0][1])
        elif result.failures:
            outcome, detail = "failed", summary(result.failures[0][1])
        elif result.skipped:
            outcome, detail = "skipped", result.skipped[0][1]
        elif result.testsRun == 0:
            continue
        else:
            outcome, detail = "passed", ""
        reported.append({"id": test_id, "outcome": outcome, "detail": detail})
    with open(args.results, "w", encoding="utf-8") as handle:
        json.dump({"schema": "factory.test-results/1", "framework": "unittest", "tests": reported}, handle)
    return 0 if all(test["outcome"] == "passed" for test in reported) else 1


if __name__ == "__main__":
    sys.exit(main())
