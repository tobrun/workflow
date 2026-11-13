# Scenario Evidence

`check-tests.py` verifies runner results and their mapping, not the meaning of assertions.
Semantic review still checks whether each test proves its scenario and whether its declared layer is accurate.
Never invent a result file or a runner case to satisfy this check.

## Capture execution

Use the repository's runner to produce JUnit XML.
For example, pytest accepts `--junitxml`; use the configured reporter for other runners.
Choose a report path outside tracked source files.
Capture the run through the checker, listing the test files and implementation files whose behavior the run covers:

```bash
python3 {build-skill-root}/scripts/check-tests.py capture \
  --layer unit --report /tmp/coupon-unit.xml \
  --receipt .dev/coupons/unit-run.json \
  --source tests/test_coupon.py --source app/coupon.py \
  -- python3 -m pytest tests/test_coupon.py --junitxml=/tmp/coupon-unit.xml
```

The receipt records the command, exit status, report hash, and source hashes.
The command must write a fresh report; a stale report or sources changing during the run fails capture.
A later source or report change invalidates the receipt.
Rerun affected tests and update their receipts after a fix; untouched sources keep their receipts.

## Map scenarios

Each `tests:` item gets an ID from its change-set number and position: `CS1-S1`, `CS1-S2`, and so on.
Keep numbering stable and append new scenarios instead of reordering existing ones.
Use the exact scenario text, including its layer tag.
Create `.dev/{plan-name}/test-evidence.json`:

```json
{
  "schema": "dev.test-evidence/1",
  "scenarios": [
    {
      "id": "CS1-S1",
      "scenario": "[unit] expired coupon -> rejected",
      "receipt": "unit-run.json",
      "source": "tests/test_coupon.py",
      "case": "tests.test_coupon::test_expired_coupon"
    }
  ]
}
```

`case` is the runner's exact `classname::name` from a JUnit `testcase`, including parameter annotations.
The checker rejects missing, failed, skipped, duplicate, stale, or wrong-layer mappings.
Do not put titles into a source registry just to pass the check.
One runner case may not stand in for several scenario IDs; parameterize distinct scenarios or split their tests.
The notes continue to record tests and deviations for human readers, but a `Tests added:` line is not execution evidence.

## E2E and migration

Capture actual application journeys through the same receipt contract at `--layer e2e`.
A custom browser or CLI harness can emit JUnit cases after observing each journey; retain screenshots or captured state in the E2E report as well.
The XML is an execution result, not a static scenario catalog.
If a repository cannot produce runner results yet, report that gap and add the narrow reporter adapter required by its real harness.

`--inventory-only` supports inspecting old notes.
It recognizes simple test declarations and checks file existence, explicitly reporting that execution and layers were not verified.
It never substitutes for the default build gate.
