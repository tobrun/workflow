# Parallel Execution

You are the orchestrator; subagents implement change sets.

## Waves

Use the waves `lint-spec.py` prints for the spec: change sets in one wave have disjoint file lists.
Before launching a wave, check that no change set in it consumes something another change set of the same wave, or an earlier unfinished one, introduces (a module, function, endpoint, or decision outcome); move a doubtful change set to the next wave.
Never start the next wave while the current one is in flight.
A wave of one change set needs no subagent: implement it yourself.

## Launching a wave

Launch one fresh-context agent per change set, up to the host's available child capacity, and queue the rest.
Each prompt gives:

1. The role: it implements exactly one change set of a spec while sibling agents implement others in the same tree.
2. The absolute paths of `spec.md`, `implementation-notes.md`, this skill's `SKILL.md` (its "The change-set loop" section), `layers.md`, `tests.md`, and `mocking.md`, and the number of the change set it owns.
3. Constraints:
   - Implement only this change set's `[unit]` and `[integration]` scenarios; the orchestrator runs `[e2e]` once per spec.
   - Do not touch git state or `implementation-notes.md`; report instead.
   - Edit only files in this change set's list; a needed file another change set owns is a conflict to report, not to edit.
   - Run only this change set's tests plus typecheck and lint over the packages it touches - siblings are mid-edit, so a wider run measures their half-finished work. Report commands and real results.
4. The output contract: the final message is one fenced JSON block.

```json
{
  "changeSet": 3,
  "status": "done | blocked",
  "whatWasDone": "2-4 lines",
  "seamsTested": ["..."],
  "testsAdded": ["path::test name"],
  "runnerReceipts": ["absolute/path/to/receipt.json"],
  "deviations": ["edge case found -> conservative choice made: what/why"],
  "selfValidation": { "commands": ["..."], "result": "pass | fail", "output": "the tail that matters" },
  "conflicts": ["file another change set owns that this change set needed"]
}
```

## After a wave

1. Run the wave gate below yourself; do not take the reports' word for it.
2. Commit each change set in number order, one commit each.
3. Append each change set's notes entry from its report, and map its scenarios to runner cases in `test-evidence.json` per [test-evidence.md](test-evidence.md).

A blocked change set, a failing gate, or a reported conflict is yours to finish before the next wave; relaunch the same agent on the same failure at most once.

## The wave gate

The spec's Validation block is the gate between a wave and its commits, and the only Validation run a wave gets.
Run it at the wave's impact - `python3 {build-skill-root}/../../scripts/impact-scope.py --base HEAD`, read per [../../../references/ci-parity.md](../../../references/ci-parity.md) - and run each command once per tree; after a fix, re-run the failed command first.
Leave out commands marked `(end of build)`, and the e2e suite, benchmarks, and analyses that re-run the suite (coverage, complexity, mutation) even when unmarked; they run once on the final tree.
