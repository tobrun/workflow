---
name: ship
description: Harden a change with the repository's checks and a gauntlet of analyses, review it with an independent panel whose findings are adversarially verified, and open or update an authorized pull request carrying current evidence. Use after build, for a gauntlet-only, review-only, or local-only pass, or in quick mode to re-check fixes for an earlier review; incomplete review results never approve the change.
---

# Ship

Ship a change in three phases: a gauntlet that fixes mechanics, a review panel that judges meaning, then a pull request that carries proof the change works.
You are the orchestrator for every phase: run tools, dispatch agents, aggregate, report.
Never weaken a check to make it pass, and never stand in for the panel - your own reading of the code is not a lens.

Invoking this skill is the task: detect the diff yourself and start.
Follow [../../references/execution.md](../../references/execution.md), resolving the allowed branch, base, and publication actions before remote-only work.

## Modes and scope

The default runs all three phases. On request: "gauntlet only" runs phase 1, "review only" runs phase 2 without mutating anything (right for someone else's PR), "no PR" or "local only" skips phase 3, and "quick" runs the quick mode below.

- PR number given -> that PR; else an open PR for the branch when the host's PR tooling finds one; else the local branch against the default branch.
- Gather the diff and locate the plan directory per [../../references/plan-layout.md](../../references/plan-layout.md); read its `spec.md`, and degrade gracefully without one.
- No reviewable files: say so and stop. A full-repo run happens only when the user asks.

## Phase 1: the gauntlet

Run every check the repository already requires, then these analyses, each scoped by applicability per [references/tools.md](references/tools.md); a check with nothing meaningful to analyze is reported as skipped with its reason, never as passed:

1. **Static analysis** - every linter, type checker, and format checker the repo configures, over the in-scope files.
2. **Security** - secrets, vulnerable dependencies, static security rules; a found secret escalates at once and is never quiet fix-agent work.
3. **Dead code** - symbols the diff added that nothing references, and symbols it orphaned.
4. **Duplication** - token-level clones the diff introduced against the rest of the repo.
5. **Coverage-weighted complexity** per in-scope function.
6. **Flakiness** - the tests the diff added or touched, repeated and shuffled until trusted.
7. **Mutation testing** over changed decision-heavy logic (pricing, permissions, validation), skipping glue code; survivors guide contract review and new tests, and the counts are reported, never a score gate.

Mutation testing runs unless `.dev/config.yaml` at the repository root sets `mutation_testing: false`; an absent file or key means on. A malformed file or a non-boolean value stops phase 1 with the path and the problem. When it is off, report `mutation testing: skipped (disabled in .dev/config.yaml)` without counts.

Run each check to completion per [references/gauntlet.md](references/gauntlet.md): fix agents per independent area, re-run until clean, and a violation that survives two rounds on the same root cause becomes a human call.
A run that will reach phase 3 opens `.dev/{plan-name}/pr.md` before the first check and records each check's result in it as the check finishes, per [references/pull-request.md](references/pull-request.md).
When any check dispatched fixes, re-run the spec's `[e2e]` scenarios and overwrite the e2e report, so phase 2 judges the code as it now stands.
Before phase 2, run the repository's required pull-request commands at the branch's impact per [../../references/ci-parity.md](../../references/ci-parity.md), even when no fix agent fired.

## Phase 2: the review panel

1. **Brief.** Write the diff to a scratch file. Build a short brief from the spec (invariants, error handling, layer-tagged scenarios, decision rationale), `implementation-notes.md`, and the e2e report. Excerpt the `docs/contracts.md` entries whose guarantee or reliance sites the diff touches ([../../references/contracts.md](../../references/contracts.md)); breaking a guarantee others still rely on is a finding. Without a spec, take intent from the PR body and commits.
2. **Previous reviews.** When `review_N.md` files exist, the highest-numbered one's unresolved findings become verification items, and the new report takes the next index.
3. **Panel.** Pick the lenses in [references/lenses.md](references/lenses.md) that have surface in this diff - always `simplify`, and `spec-conformance` whenever there is a spec - plus at most one diff-specific custom lens. Tell the user which and why, then open `review_N.md` per [references/report-format.md](references/report-format.md) with `Verdict: IN PROGRESS`.
4. **Run and verify.** Run all lens agents, then all verifiers, on the transport [references/orchestration.md](references/orchestration.md) selects; without a transport, stop - the panel cannot be replaced by your own reading. Between the batches, number the findings with `python3 {ship-skill-root}/scripts/aggregate-findings.py plan {batch-1 results}`; each verifier's only job is to refute its finding against the repo.
5. **Aggregate.** Run `python3 {ship-skill-root}/scripts/aggregate-findings.py aggregate {batch-1 results} {batch-2 results} --expected {lenses}`. It drops refuted findings, keeps a BLOCK only when confirmed, merges duplicates, and sets the verdict; a failed or malformed lens makes the review INCOMPLETE, which never approves a change or marks a PR ready. Retry a failed agent once.
6. **Reconcile.** Classify each finding that collides with `docs/decisions.md` per [../../references/decision-ledger.md](../../references/decision-ledger.md), read-only; ledger writes belong to a later `scope` run. Write the findings, the verdict, and the reconciliation into `review_N.md` as they land.

On BLOCK, remediate before asking: up to two rounds per [references/remediation.md](references/remediation.md), each a fresh-context fix pass over the confirmed blockers, a re-harden of the touched files, and a re-review at the next index. A blocker standing after round 2, or one that needs a spec or decision change, is a real blocker and goes to the user. Review-only mode skips this.

## Phase 3: the pull request

Per [references/pull-request.md](references/pull-request.md):

1. Commit authorized fixes and push only within the authority already resolved, keeping the intended base. Without publication capability, finish `pr.md` and deliver locally.
2. Build the Evidence section with `python3 {ship-skill-root}/scripts/pr-evidence.py extract`, passing `--scenario` for only the scenarios that show what this diff changed, and publish frontend screenshots with its `publish` command. Without an e2e report, capture evidence now: screenshots for a UI, a labeled before/after pair otherwise, and a red-on-base, green-on-branch reproducing test for every bug fix.
3. Loop `pr-evidence.py check --require-current` on `pr.md` until it passes; the check decides whether the proof is real.
4. Create the PR - a draft when a real blocker survived or a required check was deferred to CI - or update the existing one, then follow its required checks to green per [../../references/ci-parity.md](../../references/ci-parity.md).

## Quick mode

For fixes to an earlier review: one validation run, one reviewer, and a PR update - no gauntlet, no lens panel, no remediation loop.

1. Run the required pull-request commands at the diff's impact; fix what fails until green.
2. Launch one fresh-context, read-only reviewer with the spec, the highest-numbered finished `review_N.md`, and the diff. It reports each previous Blocker and Concern as fixed or still open with evidence, then reads the diff since the review's recorded `head` (the whole branch when there is no review or no `head`) for new blockers only, each with its triggering scenario. Confirm a new blocker by reading the code before it counts.
3. Write `review_N.md` at the next index with `Panel: quick`: BLOCK when a blocker stands, CONCERNS when only concerns remain, PASS otherwise, INCOMPLETE when the reviewer's result is missing or malformed. On BLOCK, stop and recommend `scope` on its small path, then `build`, then quick mode again.
4. Update the PR: refresh evidence only for scenarios whose tested behavior changed, keeping earlier evidence whose source hashes still match, loop `pr-evidence.py check --require-current`, then follow the checks to green and mark the PR ready once they are. Never force-push or rebase.

Say plainly in the wrap-up that quick mode skipped the gauntlet and the panel, and recommend a full `ship` when the change has grown beyond its findings.

## Wrap up

Summarize in one message, counting only from tool output and the aggregator. For whichever phases ran: per check, violations found, fixed, and surviving with the human call each waits on, and the scope stated honestly ("hardened the diff" is not "hardened the repo"); the verdict and top findings with every `review_N.md` this run wrote and what each remediation round cleared; the PR URL, draft or ready, what its evidence shows, and the state of its checks.
Recommend next steps, never invoking them: `commit` for the gauntlet's fixes when phase 3 did not run; `scope` on its small path, then `build`, then `ship` in quick mode when the user accepts findings that are plain defects; `scope` on the plan directory when a finding needs a decision or scope change.
