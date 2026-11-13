---
name: build
description: Execute the change sets of a spec at .dev/{plan-name}/spec.md, proving every scenario with a test at its tagged layer across unit/integration/e2e, running independent change sets in parallel, and looping until every change set is committed and the e2e run passes. Use when the user asks to build or implement a spec or its change sets.
---

# Build

Execute every change set of a spec, proving each `tests:` scenario with a real test at its tagged layer, until all change sets are committed and the e2e run is green.

Input: `.dev/{plan-name}/spec.md`, located per [../../references/plan-layout.md](../../references/plan-layout.md). Without a spec or with an empty change plan, ask the user to run `scope` first.
Follow [../../references/execution.md](../../references/execution.md) for branch, commit, and documentation actions; use the authorized current branch, or create `dev/{plan-name}` when a new branch is permitted. Build never pushes or opens a PR - that is `ship`.
Read [references/layers.md](references/layers.md), [references/tests.md](references/tests.md), and [references/mocking.md](references/mocking.md) before implementing, and [references/test-evidence.md](references/test-evidence.md) for runner receipts.

## Workflow

1. Read the whole spec: the decisions and their rationale, the scope with its Validation block, and the change plan. Without a Validation block, discover the repo's real test and typecheck commands and log them.
2. Run the change sets in the waves `lint-spec.py` prints, per [references/parallel.md](references/parallel.md): change sets in one wave run in parallel subagents, each wave passes its gate once, and each finished change set is committed in spec order. Every change set in the plan is in scope; never stop after one to ask about review.
3. Keep `test-evidence.json` current as scenarios land, with a runner receipt for each.
4. Run the e2e pass below over the whole spec, then loop `python3 {build-skill-root}/scripts/check-tests.py .dev/{plan-name}` until every scenario maps to a current passing runner case.
5. Run the repository's required pull-request commands plus every Validation command marked `(end of build)`, at the branch's impact per [../../references/ci-parity.md](../../references/ci-parity.md); what lies outside the impact is deferred to the PR's CI, which `ship` follows to green.
6. Close per "Closing message".

## The change-set loop

Each change set, whether you run it or a subagent does:

- Test at the seams the spec's scope declares, per [references/tests.md](references/tests.md); when a declared boundary is wrong or missing, follow its fallback and log the deviation instead of stalling.
- Work in vertical slices: one scenario's behavior at a time, its test written with the code, at its tagged layer per [references/layers.md](references/layers.md). A scenario is not met until a real test exists there.
- Run only the change set's own tests and typecheck while working; the Validation block is the wave gate, not a per-change-set run.
- Write each test to green; never run a test just to watch it fail or break working code to prove it can. A bug fix is the exception: start with a test that reproduces the defect, run it once red, then fix it to green.
- Refactoring belongs to `ship`'s review, not this loop.
- A red test, a failing scenario, or an edge case that contradicts the spec is work to do: fix it, log the deviation, continue. Stop early only when a blocking question makes further work unsafe or wasted.

## The e2e pass

Run once per spec, after all change sets are committed, covering every `[e2e]` scenario, against the deterministic boundary environment in [references/mocking.md](references/mocking.md#the-e2e-environment).

1. Launch the app from the repository's documented commands with that environment configured; ask the user only when no safe launch command exists.
2. Drive each scenario and capture evidence from this run: a screenshot per meaningful step for a frontend, through the host's browser integration or Playwright, or the entity's real before/after state otherwise. Never fabricate either.
3. A failing scenario is a bug: fix the code, add a test at the cheapest layer that catches it, re-run, and re-capture. After three failures on the same root cause, log what you found and ask the user.
4. Write the report as JSON to `/tmp/{project-slug}/reports/{plan-name}-e2e.json` per [references/e2e-report.md](references/e2e-report.md), with the tested revision, source hashes, command, and capture time; `ship` extracts the PR's evidence from it.

## Implementation notes

Keep `.dev/{plan-name}/implementation-notes.md` as the run goes: the run entry before the first wave, a change set's entry when it completes, and a fixup entry with each fix the wave gate, the e2e pass, or the CI-parity run forces.
Parallel agents see only this file and the code, and `ship` reads it later.

```markdown
## Build run {date}
- Validation: {the commands this run gates on, and where they came from}
- Impact: {impact-scope.py verdict and packages for the branch}
- Waves: [1 2] [3] [4 5]

## Change set {n}: {title}
- What was done: ...
- Seams tested: ...
- Tests added: {path::test name}, ...   # or "none - {reason}"; the checker reads this line
- Deviations from spec: {edge case found} -> conservative choice made: {what/why}   # only when one occurred

## Fixup: {what was wrong}
- Found by: {the wave gate | e2e scenario {id} | CI-parity command}
- What was done: ...
- Tests added: ...

## CI parity
- {command} -> {green at {scope} | red: what failed | remote-only: why | deferred to CI: outside impact}
```

## Closing message

End with these two lines, in this order, whether the run is green or blocked:

1. `Next step: run ship over this work, pointed at .dev/{plan-name}/implementation-notes.md and the {plan-name}-e2e.json report.` Name `ship` in quick mode when this run built change sets that fix an earlier `review_N.md`. Recommend it; never launch it.
2. Any question left for the user: a blocked gate or an unresolved deviation.
