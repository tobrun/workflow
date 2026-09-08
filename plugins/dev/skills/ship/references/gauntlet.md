# The Gauntlet Loop

How phase 1 of `ship` runs its eight checks to completion.
The check definitions and their acquisition ladder live in [tools.md](tools.md); this file owns the loop, the fix agents, and the thresholds.

## The loop

The five read-only analyzers - static analysis, security, dead code, duplication, dependency rules - scan as one parallel batch: their initial runs mutate nothing, so collect all five violation lists concurrently, dispatch fix agents grouped by independent area across the combined lists, and re-run all five together until clean.
The expensive three - complexity x coverage, flakiness, mutation - stay sequential, cheapest first: they contend for the test runner, and each one's input shifts with every fix the previous one landed.

For each tool (the batched five count as one), in order:

1. Run it; collect the violations.
2. Dispatch fixes: one fresh-context agent per independent area, launched in a single message, each given only the violation list for its area, the relevant file paths, and the fix vocabulary below.
3. Re-run the tool until clean, then run the spec's Validation block (or the repo's test suite) to prove the fixes broke nothing; skip that run when the tool dispatched no fixes.
4. A violation that resists two fix rounds on the same root cause, or that the change seems to legitimately require, is a human call: stop and present it - it is either a real defect, a threshold worth changing, or a rule the spec should have amended.

## Exit: refresh the e2e evidence

Fix agents edit code, so once any tool dispatched fixes, the e2e report build wrote no longer describes the code phase 2 will judge.
Before leaving phase 1, re-run the spec's `[e2e]` scenarios exactly as build does - same mocked environment, data mapping, and template, all owned by [../../build/SKILL.md](../../build/SKILL.md) and its references - overwriting `/tmp/{project-slug}/reports/{plan-name}-e2e-report.html`.
A scenario that fails here is a violation like any other: dispatch fixes and loop under the same two-round rule.
Never write new e2e scenarios in this step; generation belongs to build, this step only re-executes.
Skip it when no tool dispatched fixes (the report is still fresh), when there is no spec or no e2e suite to run (note that in the wrap-up), or in review-only mode, which never reaches phase 1.

## Exit: prove CI parity

After the e2e refresh decision, run the required pull-request commands using
[../../../references/ci-parity.md](../../../references/ci-parity.md). This gate
always runs in the default two-phase flow and gauntlet-only mode, even when no
fix agent changed code.

A red required check is a violation. "Pre-existing" requires merge-base proof
and a human call; it is never a note that permits a PR-ready verdict. When a
fix changes behavior or test orchestration, re-run the affected command and
continue until the full CI-parity set is green.

## Fix vocabulary

- Resolve a static analysis finding by fixing the code it points at, never by suppressing it inline or loosening the tool's config; a finding worth suppressing is a human call.
- Resolve a vulnerable dependency by upgrading it; an upgrade that breaks the build is a human call. A found secret is always an immediate escalation, never a quiet fix.
- Delete dead code outright; never comment it out or exclude it from the detector.
- Collapse a clone by extracting one shared helper or calling the one that already exists.
- Cut a complexity-coverage score by splitting the function or covering its paths.
- Resolve a dependency violation by inverting the dependency, inserting an interface, or splitting the module.
- De-flake a test by removing its nondeterminism (time, ordering, shared state, network), never by adding retries, sleeps, or looser assertions.
- Kill a mutant by adding the test that catches it.

Fix agents never edit thresholds, rules files, or the tools themselves, and never delete a test to make a mutant moot.

## Thresholds are decisions

Defaults: zero static analysis findings in scope; zero security findings; zero dead symbols in scope; no new clones over the 50-token threshold; zero dependency violations; complexity-coverage score at most 6 per function; zero flaky tests among those the diff touched; zero surviving mutants in scope.
Agent-written code tolerates a higher complexity threshold than the human default of 4 - agents hold more paths in working memory - but where the line sits is a decision, not a config value.
When the user accepts a different threshold, record it in `docs/decisions.md` as a `D-` entry (notation in [../../../references/decision-ledger.md](../../../references/decision-ledger.md)), dated and sourced to this run; the next run reads it from there instead of re-arguing.
Never adjust a threshold silently to make a run pass.
