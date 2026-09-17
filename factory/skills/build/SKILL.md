---
name: build
description: Execute the change sets of a spec at .dev/{plan-name}/spec.md, proving every scenario with a test at its tagged layer across unit/integration/e2e, running independent change sets in parallel and looping until every change set is done and the e2e suite passes, unattended, ending with a build-result.json for the factory runner. Use when the factory runner launches the build stage of a factory run.
disable-model-invocation: true
---

# Build

Factory copy of `build`.

Execute every change set of a spec, proving each `tests:` scenario with a real test at its tagged layer, until all change sets are done and the e2e run is green.

Input: `.dev/{plan-name}/spec.md` (from `scope`), located per [../../references/plan-layout.md](../../references/plan-layout.md).
If there is no `spec.md` or its change plan is empty, stop with a `blocked` result carrying an `input.unusable` condition; without one there are no layer-tagged scenarios to implement against.

## Factory context

The factory runner launched this stage as a fresh process; its protocol lives in [../../references/factory-run.md](../../references/factory-run.md).

- Read `.dev/factory-run.json` first: `plan` fixes the plan directory, `report_dir` holds the e2e report, `scratch_dir` holds anything temporary, and `previous` names why an earlier attempt did not pass. On a retry, continue from the committed change sets and `implementation-notes.md`; never redo finished work. When `guidance` is present, it is the foreman's instruction for this attempt: read all of it before starting, and treat it as the operator note's equal.
- No human is available: decide every open question yourself under the factory policy in that reference - take the recommended option, log it as an `auto-decided:` deviation, and keep going. Stop only for that reference's fixed park list.
- The run branch is already checked out. Commit each completed change set on it; never push and never open a PR.
- Write `.dev/{plan-name}/build-result.json` as your last action, with `next: "ship"`. Never invoke `ship`; the runner starts it after its own gate passes.

Read [references/layers.md](references/layers.md), [references/tests.md](references/tests.md), and [references/mocking.md](references/mocking.md) before implementing anything yourself; [references/parallel.md](references/parallel.md) points subagents at them.

## Workflow

1. Run `python3 {build-skill-root}/../../scripts/skill-metrics.py start build`, then read `spec.md` in full: the research section (the decisions and their rationale), the scope section (including its Validation block, which renders the `.factory/contract.json` commands per [../../references/repo-contract.md](../../references/repo-contract.md)), and the change plan. Explore the relevant code. Never edit the contract: the runner executes the one scope handed off.
2. Build waves by disjoint batching per [references/parallel.md](references/parallel.md): sequential in spec order by default, batched only when file lists are disjoint and nothing a wave-mate or earlier unfinished change set introduces is consumed. Every change set in the plan is in scope, not just the first.
3. For each wave, run its change sets in parallel per the same reference, then commit each finished change set on the current branch and append its entry to `implementation-notes.md`. A change set that adds, removes, moves, or rewires a component, flow, or boundary updates `docs/architecture.md` in the same commit and passes `architecture-check.py` first, per [../../references/architecture.md](../../references/architecture.md).
4. Move straight to the next wave. Never stop after one change set or wave to ask about review.
5. Map every scenario id in the run file's `scenarios_file` in `.dev/{plan-name}/scenario-map.json` - unit and integration scenarios to their test ids, e2e scenarios to a driver case - and loop `python3 {build-skill-root}/scripts/check-tests.py .dev/{plan-name}` until it exits clean. The runner then executes exactly those tests itself and rejects any that are not collected, skipped, or failing; a `[repro]` scenario's tests must also fail on the base.
6. Then run the full e2e pass per "The e2e layer" below over the whole spec, and loop on failures until it is green.
7. Run the repository's required pull-request commands per [../../references/ci-parity.md](../../references/ci-parity.md), starting them in the background as soon as the e2e loop is green and rendering the e2e report while they run - the two share nothing. A known-red CI scenario is not an acceptable deviation.
8. After the e2e report and CI-parity gate, close per "Closing message". Build never pushes or opens a PR; that is `ship`'s phase 3.
9. Leave the tracked worktree clean: the runner's gate requires it, then runs the mapped tests, the `[repro]` checks, the e2e driver, and every contract validation command itself under the attempt's deadline. Never delete or move what the contract's setup installed (`node_modules`, `.venv`, and the like): the runner keeps those out of Git status, and the gate's tests need them.

## Jira sync

Read `.dev/config.json`; when `jira.enabled` is true, follow
[../../references/jira.md](../../references/jira.md) from before the first `acli` call - it owns the
command shapes, the transition timing, and the failure protocol. The
orchestrator alone invokes `acli`; subagent prompts and the `parallel.md`
contract do not change.

With an absent or disabled config, perform no Jira behavior or mention.
The factory run branch is already checked out; never create or switch
branches, never push, and never open a PR - `ship` pushes and opens the PR
with evidence after its gauntlet and review.

## Closing message

Every build run first prints the measured run metrics, pasting the table verbatim:

```bash
python3 {build-skill-root}/../../scripts/skill-metrics.py end build --count change_sets=N --count scenarios=N --count e2e_passed=N --count e2e_failed=N
```

Then write `build-result.json` per [../../references/factory-run.md](../../references/factory-run.md) as the final action - a green run, a blocked gate, and a run with open deviations all get one:

- `done` only when every change set is committed, `check-tests.py` is clean, the mapped tests and the e2e driver pass when you run them, and the Validation block passes.
- `blocked` otherwise, with the first failing condition as `reason`, each park-list item as a typed entry in `conditions`, and the counts from the metrics call, including `auto_decided`.

The runner, not this skill, starts `ship`.

## The change-set loop

Each change set, whether you run it yourself or a subagent runs it, follows the same loop:

- Test at the seams the spec's scope section declares, per [references/tests.md](references/tests.md); if the declared boundary is wrong or missing, follow its fallback and log the change under Deviations - do not stall on it.
- Implement in **vertical slices**: one scenario's behavior at a time, its test written before or right after the code - the enforced outcome is what matters, not the ritual order. Each `tests:` scenario's test lives at its tagged layer ([references/layers.md](references/layers.md)); a scenario isn't met until a real test exists there.
- Run the change set's own tests and typecheck continuously; once the change set is green, run the spec's Validation block verbatim - it is the wider suite plus typecheck/lint - and only report done when it passes clean.

## Rules of the loop

- **Every test must have been seen red.** A test that has never failed proves nothing: earn its green by writing it before the code, or by briefly breaking the behavior once after. Bug fixes are strictly test-first: a defect change set starts with a failing test that reproduces the reported issue - red is the proof it was actually reproduced - only then fix, and watch that same test go green.
- **One slice at a time.** One seam, one behavior, one test, one minimal implementation per cycle.
- **Refactoring is not part of the loop.** It belongs to `ship`'s review phase.
- **Keep going.** A red test, a failing e2e scenario, or an edge case that contradicts the spec is work to do, not a reason to hand back. Fix it, log the deviation, continue. A question the spec does not answer is a decision: take the recommended option, log it as `auto-decided:`, continue. Stop early only for the factory park list, as a typed `blocked` result.

## The e2e layer

E2E scenarios are proven by running the actual application against the **fully mocked environment** defined in [references/mocking.md](references/mocking.md#the-e2e-environment). Run this once per spec, after all change sets are committed, covering every `[e2e]` scenario across change sets.

The contract's `e2e.driver` (and its `services`) is what the runner executes: the driver launches or reaches the app, drives each case, and writes the `factory.e2e/1` record to `$FACTORY_E2E_OUT/e2e.json` with `revision` set to `$FACTORY_REVISION` and each scenario's `id` set to its case name, per [references/e2e-report.md](references/e2e-report.md). Commit the driver at the path the contract names; run it yourself the same way while iterating.

1. **Launch the app.** Invoke an installed `run` skill with the mocked environment configured when the host supports direct skill invocation. Otherwise inspect the repository's documented commands and start the app directly. When no safe launch command can be determined, stop with a `blocked` result carrying a `launch.unavailable` condition whose evidence names every discovery attempt and why each was unsafe.
2. **Drive it and capture evidence**, per scenario:
   - `kind: "frontend"` - use available browser automation (the host browser integration or Playwright) to exercise the scenario, one screenshot file per meaningful step with its SHA-256.
     The runner hosts the browser: `AGENT_BROWSER_CDP` and `FACTORY_BROWSER_CDP_URL` point `agent-browser` and Playwright's `connectOverCDP` at a headless Chrome running outside your sandbox, where Chrome itself cannot start. Never launch a browser of your own, and a browser problem is never `launch.unavailable`: record it in Deviations, commit the driver, and finish `done`, because the runner runs the driver with the same browser at the gate and its result is the evidence.
   - `kind: "non-frontend"` - capture the entity's real before/after state from the run's own output or fixtures.
3. **Never fabricate a screenshot or a data-model-state entry.** Both come from this actual run.
4. **Loop until green.** A failed scenario is a bug: diagnose it, fix the code (a new red-green cycle at the right layer), re-run and re-capture that scenario. Never flip a status to pass without a fresh capture. If a scenario fails three times on the same root cause, write what you found into Deviations, leave the scenario marked failed in the report, and finish with a `blocked` result (no condition: the gate retries it).
5. The runner publishes the record it verified to `{report_dir}/{plan-name}-e2e.json` and renders `{report_dir}/{plan-name}-e2e-report.html` with `scripts/render-e2e.py`; never write those files by hand.

## Implementation notes

Maintain `.dev/{plan-name}/implementation-notes.md`, appended after each change set completes, never written once at the end.
It is the shared state across waves - parallel change-set agents can't see each other's conversation, only this file and the code - and the evidence `ship` reads later.

```markdown
## Change set {n}: {title}
- What was done: ...
- Seams tested: ...
- Tests added: {path::test name}, ...   # or "none - {reason}"; scenario-map.json is what the runner executes
- Deviations from spec: {edge case found} -> conservative choice made: {what/why}   # only when a deviation occurred
- Deviations from spec: auto-decided: {question} -> {recommended option taken} - {why it is the recommendation}   # one line per decision
```

This file is a short running log, not a rendered report.

## Anti-patterns

- **Horizontal slicing** - all tests first, then all implementation. Tests then verify an imagined shape and go insensitive to change.
- The other tells - implementation-coupled tests, tautological assertions, top-heavy testing - are defined in [references/tests.md](references/tests.md) and [references/layers.md](references/layers.md); flag and fix them on sight.
