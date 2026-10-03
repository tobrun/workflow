# Decisions

## Principles

P-deterministic-guards-over-prose: when a loop or skill must be constrained, prefer a check it cannot argue with over an instruction
  promoted 2026-09-17 - recurred across several decisions in the factory work (removed 2026-09-20); matches CLAUDE.md "prefer a deterministic check it loops against"

## Quality gauntlet

D-complexity-threshold: Where does the ship gauntlet's coverage-weighted complexity line sit in this repository? (2026-09-18)
  ✓ 10 per function, applied to functions a branch adds - holding pre-existing functions a diff only brushes to any line would turn every change into a refactor of code it did not write (user, 2026-09-18); the evidence was the factory runner's established functions sitting far above the default, measured with radon 2026-09-18, and that code was removed 2026-09-20 ⚠ touched pre-existing functions stay over the line until a change that owns them splits them
  ✗ 6 on new functions - splits nearly every new function into helpers that each earn nothing on their own, against keeping indirection earned
  ✗ 6 on every touched function - a refactor of modules outside the change

## Removed

D-remove-factory: The factory plugin and its runner are removed from this repository (2026-09-20)
  ✓ delete `factory/`, `plugins/factory/`, the runner end-to-end test, and the F01 through F04 validator checks - the implementation was not working out (user, 2026-09-20); the repository goes back to shipping `dev` alone
  ✗ keep it unmaintained behind a flag - dead weight in every validation run and every plugin build, and it would keep drifting from `dev`

## Build speed

D-wave-gate: How often does build run a spec's Validation block? (2026-10-01, user feedback on the contexia searchable-kb build)
  ✓ once per wave, by the orchestrator, as the gate before the wave's commits; an implementer runs only the test files its change set adds or edits plus typecheck and lint, and e2e, benchmark, and suite-repeating analysis commands wait for the end of the build - the block ran more than once per change set, and it held the e2e suite, the benchmarks, and a complexity script that re-runs the suite with coverage (user, 2026-10-01; contexia `.dev/metrics.jsonl`: builds of 11,773 s and 17,565 s); in a shared tree an agent's full run also measures its siblings' half-finished work ⚠ a regression outside a change set's own tests is found at the wave gate, not by the agent that caused it, and a complexity or benchmark miss only at the end
  ✗ once per build - the commits in between would be unverified, and a late red has to be bisected across change sets
  ✗ two separate blocks in the spec, a fast one and a full one - a format change every reader of the block would have to learn; an `(end of build)` mark on a command says the same inside the one block, and build recognizes the three kinds unmarked, so older specs get the gate too
D-seen-red-cost: What may proving a test red cost? (2026-10-01, user feedback on the contexia searchable-kb build) superseded by D-no-red-step (2026-10-02, user feedback), which drops the red step outside bug fixes
  ✓ nothing when the test is written first, and one break per slice, running one test file, when it is written after - the rule guarantees that no test is unable to fail, and one break proves that for every test of the slice; one change set spent 47 break-and-rerun cycles on 75 tests (user, 2026-10-01)
  ✗ drop the rule and lean on ship's mutation testing - it runs after the tests are already trusted, and only over the files the gauntlet scopes in
  ✗ require test-first everywhere - build enforces the outcome, not the order; strict test-first stays reserved for bug fixes
D-no-red-step: Does build still prove each test red? (2026-10-02, user feedback)
  ✓ no - a test is written with its slice and run to green; no test is run just to be seen failing, and no working code is broken to prove that it can; a bug fix alone still starts with one run of the reproducing test before the fix, because that red is the proof the issue was reproduced and costs one run per defect - red first and green afterwards costs too many tokens and takes too long, and the tests themselves are still wanted (user, 2026-10-02) ⚠ build no longer catches a test that cannot fail: what is left is the independent-expected-value rule in `tests.md` and ship's mutation run, which is reported and not a gate (D-mutation-threshold) ⚠ this supersedes D-seen-red-cost
  ✗ keep the cheap red of D-seen-red-cost - one break per slice is still an edit, a run, and a revert in every slice of every change set
  ✗ make ship's mutation run a gate to win the guarantee back - D-mutation-threshold rejected a gate, because a diff inherits coverage debt it did not create
D-change-set-size: How large may a change set be? (2026-10-01, user feedback on the contexia searchable-kb build)
  ✓ at most 25 scenarios, enforced by `lint-spec.py`; a change set already logged in `implementation-notes.md` is exempt, because change sets never renumber - a 38-scenario change set took 73 minutes against 18 and 23 for the waves before it (contexia git log, 2026-10-01), and the limit flags 4 of the 61 change sets in the four contexia specs (27, 32, 38, 62 scenarios) ⚠ it counts scenarios, not files, so a change set with few scenarios and many files passes
  ✗ a file-count limit - file lists are prose with abbreviations, so the count would be a guess the author can argue with
  ✗ prose guidance alone - "iterate small" was already asked and did not hold; per P-deterministic-guards-over-prose
D-wave-report: How does scope see the parallelism its plan allows? (2026-10-01, user feedback on the contexia searchable-kb build)
  ✓ `lint-spec.py` prints, on a clean spec, the build waves the file lists allow and the shared files that make a change set wait - "keep file lists disjoint where possible" was already asked and still produced three change sets queued behind `compose.ts`; printed rather than failed, because a chain is sometimes the honest shape of a change ⚠ file detection is a heuristic over backticked names, and what one change set consumes from another stays build's judgment
  ✗ fail the lint on a chain - no threshold separates a careless chain from a necessary one
  ✗ build overlapping change sets in separate worktrees and merge - spec order is the dependency order, so an overlap usually is a real dependency
D-change-set-brief: What does a change-set agent read before it starts? (2026-10-01, user feedback on the contexia searchable-kb build)
  ✓ a brief `change-set-brief.py` cuts from the spec: every section but research and the change plan, the decisions its change set links, its own plan, and the notes without their test inventories - 42 KB against 130 KB of spec and notes for change set 5 of the contexia searchable-kb plan (measured 2026-10-01); every line is verbatim, so nothing is paraphrased ⚠ a decision a change set leans on but does not link is left out; the spec's path stays in the prompt for that
  ✗ the whole spec and the notes - every agent pays for every other change set's plan
  ✗ a summary the orchestrator writes per agent - output tokens are the slow ones, and a paraphrase can be wrong

## Plan files

D-written-as-the-run-goes: When does a skill write its file under `.dev/{plan-name}/`? (2026-10-02, user feedback)
  ✓ as the run goes: the file is created as soon as the run has something true to put in it, and each later fact reaches it when it is established - `scope` opens `spec.md` during the interview and catalogs decisions into it as `[open]` entries, `scope-review` opens `spec-review_N.md` before round 1, `build` adds a run entry and fixup entries to the notes, `ship` opens `review_N.md` when the panel is selected and `pr.md` before the first check; the skills wrote at the end of a stage, and continuously updated files were asked for (user, 2026-10-02); the rule lives once in `plan-layout.md` and each skill names its moments ⚠ a spec, a report, or a PR body can now be found half-written: `Verdict: IN PROGRESS`, a `lint-spec.py` that is not clean, and a failing `pr-evidence.py check` mark those, and no other skill builds on one
  ✗ keep writing when the stage closes - a two-hour run holds its result only in the conversation, so a dead or compacted session loses it, nobody can follow the run from its files, and the file is a recollection instead of a record
  ⊘ a checkpoint command that fails when a file did not change between two phases - not doing, against P-deterministic-guards-over-prose: the late write was what the skill text itself ordered (`scope` phase 3, `scope-review` step 7, `ship` phase 2 step 6), so moving the instruction removes the cause; reopen if runs under the new text still write at the end of a stage
D-ship-record-in-pr-body: Where does ship record its gauntlet results while it runs? (2026-10-02, follows D-written-as-the-run-goes)
  ✓ in `pr.md`, opened before the first check by a run that will reach phase 3 - its Quality table and Open calls already are that record, so the file fills check by check instead of being composed in phase 3; a part not reached yet is absent, never a placeholder ⚠ a gauntlet-only, review-only, or no-PR run still keeps its tallies only in the conversation
  ✗ `pr.md` in every mode - a partial run would overwrite the record of the body an open PR was proposed with
  ✗ a new gauntlet log file - a second artifact and a second reader for what the PR body already holds; reconsider if partial runs need a durable record
D-notes-fixup-entry: How do the notes record code changed outside a change set's own loop? (2026-10-02, follows D-written-as-the-run-goes)
  ✓ a `## Fixup:` entry written with the fix, naming what found it, and `check-tests.py` ends a change set's entry at any other `##` heading - builds already wrote such entries unasked ("Orchestrator fixup after wave 9" in the contexia github-sync-teams notes, read 2026-10-02), and a fixup's `Tests added:` line counted toward the scenarios of the change set above it (reproduced by a unit test, 2026-10-02); fixup tests are still checked to exist
  ✗ a Deviations line on the nearest change set - that change set is committed and its entry says what its agent did
