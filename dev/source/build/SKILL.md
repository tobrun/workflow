---
name: build
description: Execute the change sets of a spec at .dev/{plan-name}/spec.md, writing a test at its tagged layer for every scenario and running the fast layers (unit, component, integration, contract), running independent change sets in parallel, and looping until every change set is committed; the e2e, smoke, property, fuzz, and benchmark tests it writes run first in ship's verify phase. Use when the user asks to build or implement a spec or its change sets.
disable-model-invocation: true
---

# Build

Execute every change set of a spec, writing each `tests:` scenario as a real test at its tagged layer, until all change sets are committed and every fast-layer test is green.
The deferred layers in [references/layers.md](references/layers.md#deferred-layers) - `[e2e]`, `[smoke]`, `[property]`, `[fuzz]`, `[benchmark]` - are written here and typechecked, never run: `ship`'s verify phase runs them first.

Input: `.dev/{plan-name}/spec.md`, located per [../../references/plan-layout.md](../../references/plan-layout.md). Without a spec or with an empty change plan, ask the user to run `scope` first.
Follow [../../references/execution.md](../../references/execution.md) for branch, commit, and documentation actions; use the authorized current branch, or create `dev/{plan-name}` when a new branch is permitted. Build never pushes or opens a PR - that is `ship`.
Read [references/layers.md](references/layers.md), [references/tests.md](references/tests.md), and [references/mocking.md](references/mocking.md) before implementing, and [references/test-evidence.md](references/test-evidence.md) for runner receipts.

## Workflow

1. Read the whole spec: the decisions and their rationale, the scope with its Validation block, and the change plan. Without a Validation block, discover the repo's real test and typecheck commands and log them.
2. Run the change sets in the waves `lint-spec.py` prints, per [references/parallel.md](references/parallel.md): change sets in one wave run in parallel subagents, each wave passes its gate once, and each finished change set is committed in spec order. Every change set in the plan is in scope; never stop after one to ask about review.
3. Keep `test-evidence.json` current as scenarios land: a runner receipt for each fast-layer scenario, and a mapping without one for each deferred scenario, per [references/test-evidence.md](references/test-evidence.md).
4. After the last wave, write the `[e2e]` and `[smoke]` tests against the environment in [references/mocking.md](references/mocking.md#the-e2e-environment), typecheck and lint them, and commit them; do not launch the app or run them.
5. Loop `python3 {build-skill-root}/scripts/check-tests.py .dev/{plan-name} --deferred` until every fast-layer scenario maps to a current passing runner case and every deferred one to a written test.
6. Close per "Closing message". The Validation commands marked `(end of build)` and the repository's required pull-request commands belong to `ship`, which runs them before the PR.

## The change-set loop

Each change set, whether you run it or a subagent does:

- Test at the seams the spec's scope declares, per [references/tests.md](references/tests.md); when a declared boundary is wrong or missing, follow its fallback and log the deviation instead of stalling.
- Work in vertical slices: one scenario's behavior at a time, its test written with the code, at its tagged layer per [references/layers.md](references/layers.md). A scenario is not met until a real test exists there.
- When the spec's `formal` line names Lean, Verus, or Kani, prove its statements per the Proof entry of [references/layers.md](references/layers.md), never editing an approved statement.
- Run only the change set's own fast-layer tests and typecheck while working; the Validation block is the wave gate, not a per-change-set run. A deferred-layer test is written and typechecked, never run.
- Write each test to green; never run a test just to watch it fail or break working code to prove it can. A bug fix is the exception: start with a test that reproduces the defect, run it once red, then fix it to green.
- Refactoring belongs to `ship`'s review, not this loop.
- A red test, a failing scenario, or an edge case that contradicts the spec is work to do: fix it, log the deviation, continue. Stop early only when a blocking question makes further work unsafe or wasted.

## Implementation notes

Keep `.dev/{plan-name}/implementation-notes.md` as the run goes: the run entry before the first wave, a change set's entry when it completes, and a fixup entry with each fix the wave gate forces.
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
- Found by: {the wave gate | the deferred check}
- What was done: ...
- Tests added: ...

```

## Closing message

End with these two lines, in this order, whether the run is green or blocked:

1. `Next step: run ship over this work, pointed at .dev/{plan-name}/implementation-notes.md; its verify phase runs the deferred tests and the e2e pass.` Name `ship` in quick mode when this run built change sets that fix an earlier `review_N.md`. Recommend it; never launch it.
2. Any question left for the user: a blocked gate or an unresolved deviation.
