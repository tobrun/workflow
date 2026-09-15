---
name: ship
description: Run the quality pass that ships a change in three phases - phase 1 is a deterministic harden gauntlet (the repo's own static analysis, security scan, dead code, duplication, dependency rules, coverage-weighted complexity, test flakiness, mutation testing) that loops fresh-context fix agents until every check passes, phase 2 is a read-only adversarially verified review panel over the post-fix diff, writing .dev/{plan-name}/review_N.md, a BLOCK verdict gets two autonomous fix-and-re-review rounds before it counts as a real blocker, and phase 3 pushes and opens the pull request with visual proof of the change (e2e screenshots for a frontend, before/after state or a red-then-green reproducing test otherwise) gated by a deterministic evidence check. Use after build to finish a change and open its PR, or on request for a single phase such as gauntlet only or review only.
---

# Ship

Ship a change in three phases: a deterministic gauntlet that fixes mechanics, a review panel that judges meaning, then a pull request that carries proof the change works.
Prompted quality rules soften into guidelines as a context grows; a checker's exit code does not, and judgment belongs to a verified panel, not to one context.
You are the orchestrator for both phases: run tools, dispatch agents, aggregate, report.
Never weaken a check to make it pass, and never stand in for the panel - your own reading of the code is not a lens.

Invoking this skill is the task - detect the diff yourself and start immediately; do not ask what to ship.
First run `python3 {ship-skill-root}/../../scripts/skill-metrics.py start ship` so the wrap-up can measure this run instead of recalling it.

## Phase selection

Default is phase 1 (gauntlet), phase 2 (review), then phase 3 (pull request): the panel reviews the diff as it stands after the gauntlet's fixes, so its findings are about meaning, not mechanics already settled, and the PR carries both verdicts.
On request, run a single phase: "gauntlet only" (or "harden only") runs phase 1 alone; "review only" runs phase 2 alone - the right mode when mutating fixes are unwanted, such as on someone else's PR. "No PR" or "local only" runs the default flow without phase 3.
The phases differ in contract: phase 1 mutates the repo (fix agents edit code, tools get committed, accepted thresholds land in `docs/decisions.md`); phase 2 is strictly read-only and never edits code or the ledger, but a BLOCK verdict hands the diff to the remediation loop below, which mutates like phase 1; phase 3 commits, pushes, and opens or updates the PR, never force-pushing.

## Scope

Both phases share one scope.

- PR number given -> target that PR; else the host's PR tooling, when it has any, for an open PR; else the local branch against the default branch.
- Gather the diff per the diff-scope rules in [../../references/plan-layout.md](../../references/plan-layout.md): local git only, standard exclusions.
- Locate the plan directory by the same reference's convention and read its `spec.md`; degrade gracefully without one.
- Full-repo runs only when the user asks - they are expensive, and the loops are the same.
- No reviewable files: say so and stop. Diff is tiny (1-2 files) or huge (>25k lines): confirm with the user before spending on agents.

## Phase 1: the gauntlet

Eight checks, cheapest first - the five read-only analyzers scan as one parallel batch, the three test-contending ones run sequentially, per the loop reference; [references/tools.md](references/tools.md) owns their definitions and the acquisition ladder.
Never weaken or skip a check because acquiring its tool is work.

1. **Project static analysis** - every linter, type checker, and format checker the repo already configures, run over the in-scope files.
2. **Security scan** - secrets, vulnerable dependencies, and static security rules; a found secret escalates immediately, it is never quiet fix-agent work.
3. **Dead code** - symbols the diff added that nothing references, and symbols it orphaned by removing their last caller.
4. **Duplication** - token-level clones the diff introduced against the rest of the repo.
5. **Dependency rules** against `docs/dependencies.md` ([../../references/dependency-rules.md](../../references/dependency-rules.md) owns the checker semantics; absent file: skip with a clear note, never invent rules).
6. **Coverage-weighted complexity** per in-scope function.
7. **Flakiness** - the tests the diff added or touched, repeated and shuffled until trusted.
8. **Mutation testing** over the in-scope source.

Run each check to completion per the loop in [references/gauntlet.md](references/gauntlet.md).
When any check dispatched fixes, end the phase with the e2e refresh in the same reference: re-run the spec's `[e2e]` scenarios and overwrite the report, so phase 2 judges the post-fix code instead of stale evidence.
Before phase 2, always run the required pull-request commands per
[../../references/ci-parity.md](../../references/ci-parity.md), even when no
fix agent fired. Feature E2E is not a substitute for a repository screenshot,
packaging, or report job that CI requires.

## Phase 2: the review panel

Review the diff with a panel of concern-focused agents, verify every finding against the repo, and write the report as a plan artifact.

### 1. Load intent

The review checks the diff against what was specced, not just general quality.

- Write the diff to a scratch file; agents read it from there, not inline.
- Read the target repo's `docs/contracts.md` first, if it exists ([../../references/contracts.md](../../references/contracts.md)): its boundary guarantees are premises. Excerpt the entries whose guarantee or reliance sites the diff touches into the brief handed to every lens; a diff that breaks a cited guarantee while reliance sites still assume it is finding material.
- From the spec read in Scope: the scope section for invariants, boundaries, and error handling, the change plan for per-set files and layer-tagged `tests:` scenarios, and the research section for the decision rationale used to judge deviations.
- Also read `.dev/{plan-name}/implementation-notes.md` if present (deviations logged during implementation) and the latest `/tmp/{project-slug}/reports/{plan-name}-e2e-report.html`'s data block, per the reading rules in [../../references/reporting.md](../../references/reporting.md). Both are additional evidence for the `spec-conformance` and `tests` lenses, not just the diff itself.
- No spec: derive `{plan-name}` from the branch name and fall back to the PR body and commits for intent.
- Build a short brief: what the change does, what's on the critical path, what was specced.

### 2. Check for previous reviews

`review_N.md` files present -> this is a re-review: extract unresolved findings from the highest-numbered one as verification items (fixed or not?), and the new report gets the next index.

### 3. Select the panel

Read [references/lenses.md](references/lenses.md); select only lenses with surface in this diff (a docs-only change skips performance).
Include `spec-conformance` whenever a spec was found, and always include `simplify` - every diff has simplification surface.
At most one diff-specific custom lens (migrations, concurrency, i18n) when clearly warranted, defined in the same shape as the built-ins.
Tell the user which lenses you selected and why before launching.

### 4. Run the review panel

Run the two batches - all lens agents first, then all verifiers - on the transport selected by [references/orchestration.md](references/orchestration.md), which owns transport choice, result-file delivery, and the prompt contracts.
If no transport is available, stop and explain that this panel-based phase cannot preserve its verification contract.
Between the batches, number the findings the verifiers get:

```bash
python3 {ship-skill-root}/scripts/aggregate-findings.py plan {batch-1 results}
```

Every BLOCK and CONCERN it lists is adversarially verified - the verifier's only job is to refute it against the actual repo.

### 5. Aggregate

The same script applies the verdicts, so these rules live in one place instead of softening across a long context:

```bash
python3 {ship-skill-root}/scripts/aggregate-findings.py aggregate {batch-1 results} {batch-2 results} --expected {lenses you selected}
```

It drops REFUTED findings, keeps a BLOCK only when CONFIRMED and carries every other surviving finding as a CONCERN, merges duplicates by file:line while crediting each lens that found them, names the lenses that failed or returned nothing parseable, and sets the verdict.
A failed lens doesn't abort the review - the report names it, so the verdict is honestly partial.

Then, with findings verified, read the target repo's `docs/decisions.md` if it keeps one, and classify each colliding finding per the recommender contract in [../../references/decision-ledger.md](../../references/decision-ledger.md). Read-only - this phase never edits the ledger; classifications land in the report's Decision reconciliation section, and ledger writes belong to the follow-up `scope` run.

### 6. Write the report

Write `.dev/{plan-name}/review_N.md` in the shape of [references/report-format.md](references/report-format.md), at the next free index.

### 7. Publish the report

Map the report onto `REVIEW_DATA` per [references/data-schema.md](references/data-schema.md) and render [templates/review.html](templates/review.html) to `/tmp/{project-slug}/reports/review_N.html`, opening and publishing per [../../references/reporting.md](../../references/reporting.md) (stable review favicon; title names the plan and review number).

## Blocker remediation

A BLOCK verdict is work before it is a question: run up to two rounds per [references/remediation.md](references/remediation.md), each a fresh-context fix pass over the confirmed blockers, a re-harden of the touched files, and a re-review at the next index.
PASS or CONCERNS ends the loop; a blocker still standing after round 2, or one an agent escalated as needing a spec or decision change, is a real blocker and the only kind the wrap-up presents as a human call.
Skipped in review-only mode.

## Phase 3: the pull request

Open the PR automatically, with evidence a reviewer can see before reading the diff, per [references/pull-request.md](references/pull-request.md).

1. Commit what the gauntlet left uncommitted, branch off the default branch if still on it, and push.
2. Build the Evidence section from the e2e report with `python3 {ship-skill-root}/scripts/pr-evidence.py extract`, publishing frontend screenshots to the `pr-evidence` branch with its `publish` command; without an e2e report, capture the evidence now per the reference - screenshots for a UI, a labeled before/after pair otherwise, and a red-on-base, green-on-branch reproducing test for every bug fix.
3. Write `.dev/{plan-name}/pr.md` in the reference's body shape and loop `pr-evidence.py check` on it until it passes; the check, not your judgment, decides whether the proof is real enough.
4. Create the PR (draft when a real blocker survived remediation) or update the one that already exists, then follow its required checks to green per [../../references/ci-parity.md](../../references/ci-parity.md).

## Wrap up

Summarize whichever phases ran in one chat message, opening with the measured run metrics:

```bash
python3 {ship-skill-root}/../../scripts/skill-metrics.py end ship --count violations_found=N --count violations_fixed=N --count violations_surviving=N --count findings_verified=N --count findings_refuted=N --count remediation_rounds=N --count blockers_cleared=N --count evidence_items=N
```

Pass only counters you tallied from tool output and the aggregate script; the table it prints (time, tokens, agents, tool calls, git delta, trend against earlier runs) is pasted verbatim, never retyped.
For the gauntlet, per tool: violations found, fixed, and surviving (with the human call each is waiting on); name the tools acquired or built this run and where they live; state the scope honestly - "hardened the diff" is not "hardened the repo".
For the review: the final verdict and top findings, linking every `review_N.md` this run wrote, the local HTML report, and the published URL when one was requested and created; per remediation round, which blockers cleared and which survived.
For the pull request: its URL, draft or ready, what the Evidence section shows and where it came from, and the state of its required checks.
Recommend next steps, never invoking them:

- `commit` for the gauntlet's accumulated fixes, only when phase 3 did not run.
- `scope` on this plan directory when the user accepts findings needing real work - remediation is its job, even when no spec exists.
- As independent optional next steps rather than a mandatory chain: `to-pitch` when the change needs buy-in from someone who wasn't in this conversation, and `to-quiz` when a reviewer wants a comprehension check before merging.
