# Parallel Execution

`scope` ordered the change plan so each change set builds only on the ones before it, and listed each change set's files.
This file is how to cash that in: you are the orchestrator, subagents are the implementers.

## Waves

Group the change sets into waves by consecutive-disjoint batching:

- Walk change sets in numeric order; the spec author's ordering is the dependency order.
- Grow the current wave with the next change set only when its file list is disjoint from every change set already in the wave AND it consumes nothing a change set in the wave introduces (a module, function, endpoint, or decision outcome - your judgment while reading the spec).
- Any overlap or doubt excludes that change set from the wave; it anchors the next wave.
- A change set past an excluded one may still join the current wave, under a doubled condition: disjoint from, and consuming nothing introduced by, every change set in the wave AND every earlier change set not yet committed. Doubt excludes it - the skip-ahead is the same dependency proxy applied against everything still unfinished, so it cannot produce an order the sequential walk would forbid.

Sequential-by-default means parallelism is a pure optimization that can never produce a wrong order.
Never start work belonging to the next wave while the current wave is in flight.

## Launching a wave

Launch one `Agent` per change set, **all in a single message** so they run concurrently.
A wave of one change set needs no subagent: implement it yourself in the main thread.

First write the wave's briefs, one command for the whole wave:

```bash
python3 {build-skill-root}/scripts/change-set-brief.py .dev/{plan-name} {N} {N} --out-dir /tmp/{project-slug}/briefs/{plan-name}
```

A brief is the spec cut down to one change set: the scope section, the decisions that change set links, its own plan, and what earlier change sets did and deviated on.
Every line is the spec's own, so an agent starts from about a third of the reading and loses nothing it builds against.
Write them per wave, never once up front: the notes they carry grow with every committed change set.

Each agent prompt contains:

1. The role: `You implement exactly one change set of a spec. Other agents implement sibling change sets concurrently; stay inside your change set's file list.`
2. The absolute path to the change set's brief, the number of the change set the agent owns, and this skill's `layers.md`, `tests.md`, and `mocking.md` - the agent reads them itself rather than receiving them inlined. The brief stands in for `spec.md` and `implementation-notes.md`: the agent reads neither whole, and opens the spec only to follow something the brief points at.
3. The absolute path to this skill's `SKILL.md`, with the instruction to follow its "The change-set loop" and "Rules of the loop" sections - read like the other references, not pasted into the prompt.
4. Hard constraints:
   - Implement only this change set's `[unit]` and `[integration]` scenarios. `[e2e]` scenarios are run once per spec by the orchestrator afterwards - do not launch the app.
   - Do not commit, stage, or touch git state. The orchestrator commits.
   - Do not edit files outside your change set's file list. If the change set genuinely needs a file another change set owns, stop and report it as a conflict instead of editing it.
   - Do not edit `implementation-notes.md`. Report your entry; the orchestrator appends it.
   - Run only your own checks: the test files this change set adds or edits, plus typecheck and lint over the packages it touches. Never run the spec's Validation block, the whole suite, the e2e suite, or a benchmark - siblings are mid-edit in the same tree, so a wider run measures their half-finished work, and the orchestrator runs the Validation block once for the whole wave. Report the commands and their real result. A red result is a fact to report, not something to hide or paper over.
5. The output contract below.

Output contract (the agent's final message must be exactly one fenced JSON block):

```json
{
  "changeSet": 3,
  "status": "done | blocked",
  "whatWasDone": "2-4 lines",
  "seamsTested": ["..."],
  "testsAdded": ["path::test name"],
  "deviations": ["edge case found -> conservative choice made: what/why"],
  "selfValidation": { "commands": ["..."], "result": "pass | fail", "output": "the tail that matters" },
  "conflicts": ["file another change set owns that this change set needed"]
}
```

## After a wave

1. Verify rather than trust the reports: run the wave gate below yourself, once per wave - the shared tree already holds the whole wave's changes, so one run covers every change set in it.
2. Commit each change set's work on the current branch, in number order, one commit per change set. A skipped-ahead change set's commit waits until every earlier change set is committed, so history keeps the spec's order.
3. Append each change set's entry to `implementation-notes.md` from `whatWasDone`, `seamsTested`, `testsAdded`, and `deviations`. `testsAdded` becomes the entry's `Tests added:` line, which the scenario checker reads.

A `status: blocked` change set, a failing validation, or a reported conflict is yours to finish in the main thread before the next wave starts - do not carry a red change set forward and do not relaunch the same agent on the same failure more than once.
If two change sets in a wave edited the same file anyway, reconcile it yourself and log it under Deviations.

## The wave gate

The spec's Validation block is the gate between a wave and its commits, and the only Validation run a wave gets - a wave of one you implemented yourself included.
Each command runs at the wave's impact, not over the whole repo: `python3 {build-skill-root}/../../scripts/impact-scope.py --base HEAD` scopes it to the wave's uncommitted changes, read per "Scope every local run to the impact" in [../../../references/ci-parity.md](../../../references/ci-parity.md).
Each of its commands runs once per tree: a green result stands until a file changes, so nothing is re-run "to be sure" before committing, and after a fix the failed command runs first and the rest only once it is green.
Start commands that share no state together (lint, typecheck, and the unit suite), and write the wave's notes entries while they run.

A command the block marks `(end of build)` is left out of the wave gate, and so are three kinds even when a spec lists one unmarked, because each costs minutes and tells a wave nothing it needs to commit: the e2e suite (the e2e pass runs it), benchmarks, and any analysis that re-runs the suite to measure it - coverage, complexity, mutation.
Each of those runs once, on the final tree, with the CI-parity gate and at its scope.

## When not to parallelize

- The spec has one change set, or every change set's files overlap with the one before it: run them sequentially yourself.
- Two change sets batched into a wave visibly collide anyway: treat that as a `scope` sizing miss, run them sequentially, and note it in `implementation-notes.md`.
