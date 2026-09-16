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
4. A violation that resists two fix rounds on the same root cause, or that the change seems to legitimately require, is a decision, not a question: take the recommended resolution from the fix vocabulary, list it in the PR's Auto-decided section, and continue with the remaining checks. When no resolution exists inside the rules, it stays a surviving violation in the Quality table and Open calls - never a suppression, a threshold change, or a reason to stop.

## Exit: record the gauntlet

Write `.dev/{plan-name}/gauntlet.json` (`factory.gauntlet/1`) once every check is clean, with `revision` set to `git rev-parse HEAD`, and rewrite it whenever a later commit lands.
It lists all eight checks by id - `static-analysis`, `security`, `dead-code`, `duplication`, `dependency-rules`, `complexity-coverage`, `flakiness`, `mutation` - each with `applicable`, and for an applicable check the `command` record (argv or script, as in the repository contract) that proves it clean, `tool_version`, `scope`, `threshold_source`, and `surviving` (empty when clean).
The runner re-executes every applicable command on the final revision and requires it to succeed with nothing surviving; a command the contract pins under `gauntlet.{id}.command` runs instead of yours.
A check is inapplicable only when the contract says so under `gauntlet.{id}.not_applicable`, or for `dependency-rules` when the repository has no `docs/dependencies.md`: an unavailable tool or a failed run is a failure, never inapplicability.

## Exit: refresh the e2e evidence

Fix agents edit code, so once any tool dispatched fixes, the e2e record build wrote no longer describes the code phase 2 will judge.
Before leaving phase 1, re-run the spec's `[e2e]` scenarios exactly as build does - same mocked environment, data mapping, and template, all owned by [../../build/SKILL.md](../../build/SKILL.md) and its references - overwriting `{report_dir}/{plan-name}-e2e.json` and re-rendering its HTML.
A scenario that fails here is a violation like any other: dispatch fixes and loop under the same two-round rule.
Never write new e2e scenarios in this step; generation belongs to build, this step only re-executes.
Skip it when no tool dispatched fixes (the record is still fresh), when there is no spec or no e2e suite to run (note that in the wrap-up), or in review-only mode, which never reaches phase 1.

## Exit: prove CI parity

After the e2e refresh decision, run the required pull-request commands using
[../../../references/ci-parity.md](../../../references/ci-parity.md). This gate
always runs in the default two-phase flow and gauntlet-only mode, even when no
fix agent changed code.

A red required check is a violation. "Pre-existing" requires merge-base proof
and a fix in its own commit per the CI-parity reference; it is never a note that permits a PR-ready verdict. When a
fix changes behavior or test orchestration, re-run the affected command and
continue until the full CI-parity set is green.

## Fix vocabulary

- Resolve a static analysis finding by fixing the code it points at, never by suppressing it inline or loosening the tool's config; a finding that seems worth suppressing is rewritten until it is not, or stays surviving.
- Resolve a vulnerable dependency by upgrading it; when the upgrade breaks the build, fix the breakage, else take the newest patched version that builds, else leave it surviving. A found secret parks the run immediately (factory park list), never a quiet fix.
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
Factory runs never change a threshold: a different threshold exists only as a `D-` entry in `docs/decisions.md` (notation in [../../../references/decision-ledger.md](../../../references/decision-ledger.md)) written outside the run, and the run reads it from there instead of re-arguing.
Never adjust a threshold silently to make a run pass.
