# The E2E Pass

How the verify phase runs the `[e2e]` and `[smoke]` tests build wrote and never ran.
It runs once at step 5 of [verify.md](verify.md), against the deterministic boundary environment in [../../build/references/mocking.md](../../build/references/mocking.md#the-e2e-environment), and again on exit when fixes landed after it.

1. Launch the app from the repository's documented commands with that environment configured; ask the user only when no safe launch command exists.
2. Run each `[smoke]` test first: an artifact that does not start makes every journey fail for one reason.
3. Drive each `[e2e]` scenario and capture evidence from this run: a screenshot per meaningful step for a frontend, through the host's browser integration or Playwright, or the entity's real before/after state otherwise. Never fabricate either. Run `ui-ux`'s axe-core and viewport checks on the same screens, per [tools.md](tools.md).
4. Capture each test's receipt with `check-tests.py capture --layer e2e` (or `--layer smoke`) and add it to the scenario's mapping, per [../../build/references/test-evidence.md](../../build/references/test-evidence.md#deferred-layers).
5. A failing scenario is a violation of `acceptance`: dispatch fixes under the loop's three-round rule, add a test at the cheapest layer that catches the bug, re-run, and re-capture. After three failures on the same root cause it is a human call. A test that is itself wrong (an unreachable selector, a step the spec never promised) is fixed in the test and logged as a deviation, never loosened until it passes.
6. Write the report as JSON to `/tmp/{project-slug}/reports/{plan-name}-e2e.json` per [../../build/references/e2e-report.md](../../build/references/e2e-report.md), with the tested revision, source hashes, command, and capture time. Phase 2 reviews against it, and phase 3 extracts the PR's evidence from it.

Never write a new e2e scenario here: build writes them, and a journey the spec never named is a finding for review, not a test to add.
Skip the pass, noting why in the wrap-up, when there is no spec, the spec has no `[e2e]` or `[smoke]` scenario, or the run is review-only.
