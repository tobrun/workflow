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
D-seen-red-cost: What may proving a test red cost? (2026-10-01, user feedback on the contexia searchable-kb build)
  ✓ nothing when the test is written first, and one break per slice, running one test file, when it is written after - the rule guarantees that no test is unable to fail, and one break proves that for every test of the slice; one change set spent 47 break-and-rerun cycles on 75 tests (user, 2026-10-01)
  ✗ drop the rule and lean on ship's mutation testing - it runs after the tests are already trusted, and only over the files the gauntlet scopes in
  ✗ require test-first everywhere - build enforces the outcome, not the order; strict test-first stays reserved for bug fixes
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
