---
name: ship
description: Verify a change across twenty kinds of verification - from unit, property, and fuzz tests to coverage, performance, accessibility, and a penetration test against the local build - fixing what fails, review it with an independent panel whose findings are adversarially verified, and open or update an authorized pull request carrying current evidence. Use after build, for a verify-only, review-only, or local-only pass, or in quick mode to re-check fixes for an earlier review; incomplete review results never approve the change.
license: MIT
compatibility: Needs git and Python 3.10+. Needs subagents or the Pi runner for the review panel; gh to open the pull request.
metadata:
  version: "4.4.0"
  source: "https://github.com/tobrun/workflow"
  invocation: "explicit - recommend it, never run it unasked"
---

# Ship

Ship a change in three phases: verify, which runs every kind of verification and fixes what fails; review, an independent panel that judges meaning; and publish, a pull request that carries proof the change works.
You are the orchestrator for every phase: run tools, dispatch agents, aggregate, report.
Never weaken a check to make it pass, and never stand in for the panel - your own reading of the code is not a lens.

Invoking this skill is the task: detect the diff yourself and start.
Follow [../../references/execution.md](references/execution.md), resolving the allowed branch, base, and publication actions before remote-only work.

## Modes and scope

The default runs all three phases. On request: "verify only" runs phase 1, "review only" runs phase 2 without mutating anything (right for someone else's PR), "no PR" or "local only" skips phase 3, and "quick" runs the quick mode below.

- PR number given -> that PR; else an open PR for the branch when the host's PR tooling finds one; else the local branch against the default branch.
- Gather the diff and locate the plan directory per [../../references/plan-layout.md](references/plan-layout.md); read its `spec.md`, and degrade gracefully without one.
- No reviewable files: say so and stop. A full-repo run happens only when the user asks.

## Phase 1: verify

Run every check the repository already requires, then each of the twenty kinds in [../../references/verification.md](references/verification.md) - unit, functional, component, contract, acceptance, regression, smoke, static analysis, lint, coverage, property, fuzz, formal, mutation, benchmark, performance, UI/UX, usability, threat model, and penetration - in the order and loop of [references/verify.md](references/verify.md), with the tools in [references/tools.md](references/tools.md).

- Build wrote the `[e2e]`, `[smoke]`, `[property]`, `[fuzz]`, and `[benchmark]` tests but never ran them, and left the Validation commands marked `(end of build)`; this phase runs them first, the e2e pass per [references/e2e-pass.md](references/e2e-pass.md), and ends with `check-tests.py` holding every scenario to a passing receipt.
- The spec's `### Verification` block says which kinds apply; a kind it marks `no` is skipped with its reason, unless the diff shows the reason false. A kind with nothing meaningful to check is skipped with a reason, never reported as passed.
- Each failure goes to fix agents per independent area and is re-run, up to three rounds. After that, a must-pass kind becomes a human call and a fix-and-report kind (coverage, benchmark, performance, fuzz, property, formal, penetration, usability) becomes an Open call that never blocks the ship; a proof that cheats the proof gate is must-pass.
- A found secret escalates at once and is never fix-agent work.
- Penetration tests target only the build this run started on localhost.

Mutation testing runs unless `.dev/config.yaml` at the repository root sets `mutation_testing: false`; an absent file or key means on. A malformed file or a non-boolean value stops phase 1 with the path and the problem. When it is off, its row reads `skipped (disabled in .dev/config.yaml)` without counts.

A run that will reach phase 3 opens `.dev/{plan-name}/pr.md` before the first check and writes each kind's Quality row as it finishes - all twenty, run or skipped with its reason - per [references/pull-request.md](references/pull-request.md).
When any check dispatched fixes after the e2e pass, re-run it and overwrite the e2e report, so phase 2 judges the code as it now stands.
Before phase 2, run the repository's required pull-request commands at the branch's impact per [../../references/ci-parity.md](references/ci-parity.md), even when no fix agent fired.

## Phase 2: review

1. **Brief.** Write the diff to a scratch file. Build a short brief from the spec (invariants, error handling, layer-tagged scenarios, decision rationale), `implementation-notes.md`, and the e2e report. Without a spec, take intent from the PR body and commits.
2. **Previous reviews.** When `review_N.md` files exist, the highest-numbered one's unresolved findings become verification items, and the new report takes the next index.
3. **Panel.** Pick the lenses in [references/lenses.md](references/lenses.md) that have surface in this diff - always `simplify`, and `spec-conformance` whenever there is a spec - plus at most one diff-specific custom lens. Tell the user which and why, then open `review_N.md` per [references/report-format.md](references/report-format.md) with `Verdict: IN PROGRESS`.
4. **Run and verify.** Run all lens agents, then all verifiers, on the transport [references/orchestration.md](references/orchestration.md) selects; without a transport, stop - the panel cannot be replaced by your own reading. Between the batches, number the findings with `python3 {ship-skill-root}/scripts/aggregate-findings.py plan {batch-1 results}`; each verifier's only job is to refute its finding against the repo.
5. **Aggregate.** Run `python3 {ship-skill-root}/scripts/aggregate-findings.py aggregate {batch-1 results} {batch-2 results} --expected {lenses}`. It drops refuted findings, keeps a BLOCK only when confirmed, merges duplicates, and sets the verdict; a failed or malformed lens makes the review INCOMPLETE, which never approves a change or marks a PR ready. Retry a failed agent once.
6. **Reconcile.** Classify each finding that collides with `docs/decisions.md` per [../../references/decision-ledger.md](references/decision-ledger.md), read-only; ledger writes belong to a later `scope` run. Write the findings, the verdict, and the reconciliation into `review_N.md` as they land.

On BLOCK, remediate before asking: up to two rounds per [references/remediation.md](references/remediation.md), each a fresh-context fix pass over the confirmed blockers, a re-verify of the touched files, and a re-review at the next index. A blocker standing after round 2, or one that needs a spec or decision change, is a real blocker and goes to the user. Review-only mode skips this.

## Phase 3: publish

Per [references/pull-request.md](references/pull-request.md):

1. Commit authorized fixes and push only within the authority already resolved, keeping the intended base. Without publication capability, finish `pr.md` and deliver locally.
2. Build the Evidence section with `python3 {ship-skill-root}/scripts/pr-evidence.py extract`, passing `--scenario` for only the scenarios that show what this diff changed, and publish frontend screenshots with its `publish` command. Without an e2e report, capture evidence now: screenshots for a UI, a labeled before/after pair otherwise, and a red-on-base, green-on-branch reproducing test for every bug fix.
3. Run `pr-evidence.py acceptance` and `pr-evidence.py flow` on `pr.md`, then `pr-evidence.py summarize`, then loop `pr-evidence.py check --require-current --kind {frontend|non-frontend} --spec .dev/{plan-name}/spec.md` until it passes (`--kind` is the e2e report's `kind`); the check decides whether the proof is real, whether every kind has its row, every scenario its mapping, and every gap its category, and that the CI evidence block is left empty for CI.
4. Create the PR - a draft when a real blocker survived or a required check was deferred to CI - or update the existing one, splicing in the CI evidence block it already has, then follow its required checks to green per [../../references/ci-parity.md](references/ci-parity.md).
5. Read the evidence block CI wrote at the head commit. It, not the agent's sections, is the proof: a failure, a weakened test, a new test that passes on the base, a coverage gap, or a spec scenario test it could not find that it reports is fixed and pushed for CI to re-measure, up to three rounds, per [references/pull-request.md](references/pull-request.md).

## Quick mode

For fixes to an earlier review: the deferred tests of the fixes and one validation run, one reviewer, and a PR update - no verify phase, no lens panel, no remediation loop.

1. Run the deferred tests of the change sets this run fixes (the e2e pass when any are `[e2e]` or `[smoke]`), capturing their receipts, and loop `python3 {build-skill-root}/scripts/check-tests.py .dev/{plan-name}` to clean; then run the required pull-request commands at the diff's impact, fixing what fails until green.
2. Launch one fresh-context, read-only reviewer with the spec, the highest-numbered finished `review_N.md`, and the diff. It reports each previous Blocker and Concern as fixed or still open with evidence, then reads the diff since the review's recorded `head` (the whole branch when there is no review or no `head`) for new blockers only, each with its triggering scenario. Confirm a new blocker by reading the code before it counts.
3. Write `review_N.md` at the next index with `Panel: quick`: BLOCK when a blocker stands, CONCERNS when only concerns remain, PASS otherwise, INCOMPLETE when the reviewer's result is missing or malformed. On BLOCK, stop and recommend `scope` on its small path, then `build`, then quick mode again.
4. Update the PR: refresh evidence only for scenarios whose tested behavior changed, keeping earlier evidence whose source hashes still match, run `pr-evidence.py summarize` and loop `pr-evidence.py check --require-current`, splice in the PR's CI evidence block, then follow the checks to green, read the block CI writes for the new head, and mark the PR ready once both are green. Never force-push or rebase.

Say plainly in the wrap-up that quick mode skipped the verify phase and the panel, and recommend a full `ship` when the change has grown beyond its findings.

## Wrap up

Summarize in one message, counting only from tool output and the aggregator. For whichever phases ran: per kind, run or skipped with its reason, and violations found, fixed, and surviving with the human or open call each waits on, and the scope stated honestly ("verified the diff" is not "verified the repo"); the verdict and top findings with every `review_N.md` this run wrote and what each remediation round cleared; the PR URL, draft or ready, what its evidence shows, the state of its checks, and the verdict of CI's evidence block at the head commit.
Recommend next steps, never invoking them: `commit` for the verify phase's fixes when phase 3 did not run; `scope` on its small path, then `build`, then `ship` in quick mode when the user accepts findings that are plain defects; `scope` on the plan directory when a finding needs a decision or scope change.
