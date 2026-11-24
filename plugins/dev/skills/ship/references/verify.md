# The Verify Loop

How phase 1 of `ship` runs every verification kind to completion.
The kinds, their triggers, and their gates live in [../../../references/verification.md](../../../references/verification.md); the tools and their acquisition ladder in [tools.md](tools.md); this file owns the loop, the fix agents, and the thresholds.

## Order

1. Read the spec's `### Verification` block. A kind it marks `yes` runs; a kind it marks `no` is skipped with the spec's reason, unless the diff shows the reason false, which makes it run. Without a spec, decide each kind from the triggers in verification.md and state the reason either way.
2. The read-only analyzers first, as one parallel batch: `static-analysis` (types, security, dead code, duplication) and `lint`.
3. The suite at the change's impact - `unit`, `functional`, `component`, `contract`, `acceptance`, `regression`, `smoke` - from the receipts build left when they are current, re-run otherwise.
4. The expensive analyses, sequential and cheapest first because they contend for the runner and each one's input shifts with every fix: `coverage` and complexity, flakiness, `property` with a larger budget, `fuzz`, `formal`, `mutation`, `benchmark`, `performance`.
5. Against the running local build: `ui-ux`, `usability`, then `threat-model` and `penetration`.

## The loop

The read-only analyzers - static analysis, security, dead code, duplication - scan as one parallel batch: their initial runs mutate nothing, so collect all four violation lists concurrently, dispatch fix agents grouped by independent area across the combined lists, and re-run all four together until clean.
The expensive three - complexity x coverage, flakiness, mutation - stay sequential, cheapest first: they contend for the test runner, and each one's input shifts with every fix the previous one landed.

For each kind that runs (the read-only batch counts as one), in order; skip mutation when disabled by `.dev/config.yaml` or when no changed decision-heavy logic qualifies per tools.md. For mutation, triage survivors before dispatching fixes and apply the reporting exception below:

1. Run it; collect the violations.
2. Dispatch fixes: one fresh-context agent per independent area, scheduled within available host capacity, each given only the violation list for its area, the relevant file paths, and the fix vocabulary below.
3. Re-run the tool until clean, then run the spec's Validation block (or the repo's test suite) at the fixes' impact - `python3 {ship-skill-root}/../../scripts/impact-scope.py --base HEAD`, read per [../../../references/ci-parity.md](../../../references/ci-parity.md) - to prove the fixes broke nothing; skip that run when the tool dispatched no fixes.
4. Up to three fix rounds per kind. What survives the third round on the same root cause follows the kind's gate in verification.md: a must-pass kind becomes a human call - a real defect, a threshold worth changing, or a rule the spec should have amended - and a fix-and-report kind becomes an Open call and the run moves on. A violation the change seems to legitimately require is a human call at once.
5. When the run keeps a `pr.md` ([pull-request.md](pull-request.md)), write the kind's Quality row - result, found, fixed, surviving - and any human or open call before starting the next kind; the wrap-up reads its tallies from there. Every one of the twenty kinds gets a row, a skipped one with its reason.

## Exit: refresh the e2e evidence

Fix agents edit code, so once any tool dispatched fixes, the e2e report build wrote no longer describes the code phase 2 will judge.
Before leaving phase 1, re-run the spec's `[e2e]` scenarios exactly as build does - same mocked environment and report shape, both owned by [../../build/SKILL.md](../../build/SKILL.md) and its references - overwriting `/tmp/{project-slug}/reports/{plan-name}-e2e.json`.
A scenario that fails here is a violation like any other: dispatch fixes and loop under the same three-round rule.
Never write new e2e scenarios in this step; generation belongs to build, this step only re-executes.
Skip it when no tool dispatched fixes (the report is still fresh), when there is no spec or no e2e suite to run (note that in the wrap-up), or in review-only mode, which never reaches phase 1.

## Exit: prove CI parity

After the e2e refresh decision, run the required pull-request commands at the
branch's impact using
[../../../references/ci-parity.md](../../../references/ci-parity.md). This gate
always runs in the default two-phase flow and verify-only mode, even when no
fix agent changed code; a run that opens no PR has no CI to defer to and runs
the full set.

A red required check is a violation. "Pre-existing" requires merge-base proof
and a human call; it is never a note that permits a PR-ready verdict. When a
fix changes behavior or test orchestration, re-run the affected command and
continue until every command the impact owes is green; the PR's CI
proves the rest.

## Fix vocabulary

- Resolve a static analysis finding by fixing the code it points at, never by suppressing it inline or loosening the tool's config; a finding worth suppressing is a human call.
- Resolve a vulnerable dependency by upgrading it; an upgrade that breaks the build is a human call. A found secret is always an immediate escalation, never a quiet fix.
- Delete dead code outright; never comment it out or exclude it from the detector.
- Inspect a clone for shared meaning. Reuse a helper when the duplicated behavior has one contract; do not couple unrelated concepts merely because token sequences match. A deliberate exception is recorded with its rationale, not hidden by changing thresholds.
- Cut a complexity-coverage score by splitting the function or covering its paths.
- De-flake a test by removing its nondeterminism (time, ordering, shared state, network), never by adding retries, sleeps, or looser assertions.
- Address a meaningful mutation survivor by strengthening or adding a regression test for the promised behavior, including nearby boundaries and rule combinations.
- Raise changed-line coverage with tests of the uncovered behavior at the layer the surrounding tests use, never with tests that execute lines without asserting on them.
- Fix a fuzz crash or a property or model counterexample at its root cause, and keep the minimized input as a regression test or a seed in the corpus.
- Fix a benchmark or performance regression in the code that slowed down; a regression the change needs for correctness is an Open call with both numbers.
- Fix an accessibility violation in the markup (roles, labels, contrast, focus order), never by hiding the element from the checker.
- Fix a penetration finding by closing the hole the threat model named and adding the test that tries the exploit.

Fix agents never edit thresholds, rules files, or the tools themselves, never delete a test to make a mutant moot, and never loosen a fuzz, property, or benchmark budget to make a run pass.

## Thresholds are decisions

Defaults: zero static analysis findings in scope; zero security findings; zero dead symbols in scope; no new clones over the 50-token threshold; complexity-coverage score at most 6 per function; zero flaky tests among those the diff touched; at least 80% of changed lines covered; no benchmark or performance regression over 10% against the base; no fuzz crash or hang within the budget (default 5 minutes per target); no medium-or-higher penetration finding; no serious or critical accessibility violation.
These are fallback heuristics, not calibrated safety guarantees.
Use a repository's recorded justified thresholds when present; the same maintainability bar applies to human- and agent-written code.
Five clean flakiness repetitions are observations, not proof that a test can never flake.
When the user accepts a different threshold, record it in `docs/decisions.md` as a `D-` entry (notation in [../../../references/decision-ledger.md](../../../references/decision-ledger.md)), dated and sourced to this run; the next run reads it from there instead of re-arguing.
Never adjust a threshold silently to make a run pass.

## Mutation testing is reported, not a threshold

When enabled, the must-pass kinds judge whether this run may ship; mutation testing does not, and it is the one check with no default threshold at all. A codebase's baseline kill rate depends on how much test coverage the diff's area already had going in, which this run did not choose - failing the run over that inherited debt would make ordinary changes to weakly-tested code unshippable regardless of what the diff itself does.
Triage survivors against the contract before dispatching fixes: meaningful gaps get regression tests under the loop above; equivalent, duplicate, and out-of-contract findings are recorded with reasons and left. A perfect score does not replace boundary and policy review. When a meaningful survivor resists three fix rounds (item 4 of the loop), present it and move on rather than treating it as a blocker. Report scoped killed, survived, and uncovered counts, runtime, useful test changes, and each survivor's disposition; there is no score below which the run fails.
