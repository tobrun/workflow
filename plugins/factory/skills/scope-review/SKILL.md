---
name: scope-review
description: Factory copy of scope-review - review and auto-refine a settled spec before build starts - a fresh-context agent panel checks the plan against the actual repo for infeasible change sets, missing failure paths, semantic contradictions, and untestable scenarios, then verified findings are applied to spec.md (and, where they touch settled decisions or cross-boundary invariants, to docs/decisions.md and docs/contracts.md) by refine agents and the panel re-runs; escalations are resolved by applying the panel's recommendation under factory policy and promoted the same way, so a finished run hands build a spec - and ledger - ready to implement with no human in the loop; only an invalidated premise or a genuinely new effort is deferred, and the run ends with scope-review-result.json. Use when the factory runner launches the scope-review stage of a factory run.
---

# Scope Review

Review the spec with agents that did not write it, then refine it in place - before any implementation exists, and without a human in the loop.
A defect caught here costs a spec edit; the same defect after build costs a re-implementation, so this loop runs to completion on its own and ends with a spec build can start on - not a findings list to triage, and not a handoff back to `scope`.
Escalations are decided at the end of the run by applying the panel's recommendation, and recorded as auto-decided so the choice stays auditable.
The panel judges the plan against the actual repo, not against the conversation that produced it.
You are the orchestrator: run tools, dispatch agents, apply the loop, report - your own reading of the spec is not a lens, and findings reach the spec only through verification.
This skill edits `spec.md`, and promotes settled changes to `docs/decisions.md` and `docs/contracts.md` per step 5 below - never code, and never any other file.

`scope`'s own phase-5 reviewer hunts while the spec is still being drafted, from the spec file alone.
This skill is the standalone deeper pass: fresh agents with repo access, adversarial verification, and automatic refinement - worth running when the change is large or risky, or when build will run in a different session.

Invoking this skill is the task - locate the spec yourself and start immediately; do not ask what to review.
First read the run file per "Factory context", then run `python3 {scope-review-skill-root}/../../scripts/skill-metrics.py start scope-review` so the wrap-up can measure this run.

## Factory context

The factory runner launched this stage as a fresh process; its protocol lives in [../../references/factory-run.md](../../references/factory-run.md).

- Read `.dev/factory-run.json` first: `plan` fixes the plan directory, `scratch_dir` is the panel scratch root, and `previous` names why an earlier attempt did not pass. When `guidance` is present, it is the foreman's instruction for this attempt: read all of it before starting, and treat it as the operator note's equal.
- No human is available: step 4 decides every escalation under the factory policy in that reference.
- Write `.dev/{plan-name}/scope-review-result.json` as your last action. Never invoke `build`; the runner starts it after its own gate passes.

## 1. Locate and gate

- Locate the plan directory per [../../references/plan-layout.md](../../references/plan-layout.md) and read its `spec.md`; no spec: stop with a `blocked` result carrying an `input.unusable` condition - there is nothing to review.
- Run `python3 {scope-skill-root}/scripts/lint-spec.py .dev/{plan-name}/spec.md` once. If it reports anything, stop with a `blocked` result carrying an `input.unusable` condition naming the lint problems: refinement here presumes a mechanically settled spec, and repairing an unfinished draft is `scope`'s job, not this loop's.
- Read the target repo's `docs/decisions.md`, `docs/contracts.md`, and `docs/dependencies.md` where they exist; their entries are premises the lenses cite.
- `spec-review_N.md` files present -> unresolved escalations from the highest-numbered one become verification items for round 1, and new reports continue the numbering.

## 2. The review-refine loop

Run up to two rounds; each round is panel -> verify -> refine.
A third panel means the refinements are churning, not converging - stop and escalate what remains.

1. **Panel.** Run batch 1 (all four lenses) and batch 2 (verifiers) per the mechanics below.
   From round 2 on, brief the lenses on what round 1 refined: their job is regressions in the refined material and their own unresolved findings, not a fresh full-spectrum hunt - a round that raises no BLOCK is convergence, and its CONCERNs go verified into the report rather than through another refine cycle.
2. **Aggregate.** Apply verdicts with the script below; REFUTED findings drop, a BLOCK survives only when CONFIRMED.
3. **Split.** Sort surviving findings into refinable and escalations per the authority rules below.
4. **Refine.** No refinable findings: exit the loop. Otherwise dispatch fresh-context refine agents - one per independent group of findings, launched in a single message - each given only its findings, the spec path, and the refinement rules below. They edit `spec.md` only.
5. **Re-gate.** Loop `lint-spec.py` until clean, fixing mechanical fallout with the same refine agents; then start the next round so fresh eyes judge the refined spec.

Exit the loop when a panel raises nothing refinable; two rounds of the same finding surviving refinement is itself an escalation.
Escalations collected across the rounds go to step 4, not to a handoff.

## 3. Authority and refinement rules

Refine agents resolve conflicts by this order - each level beats everything below it:

1. The user's recorded intent: the spec's stated problem, scope, and interview outcomes.
2. The repo's reality: what the code actually contains beats what the spec claims about it.
3. A settled `✓` decision: a change set contradicting its linked decision is rewritten to match the decision, not the other way around.
4. The spec's prose.

The approved intent is sealed: never remove, reword, or retag an existing `tests:` scenario or a `⊘` non-goal - the runner compares them to the handoff snapshot and fails the stage. Add a new scenario instead, and when an approved one is untestable or wrong, defer it as `Kind: premise`.
Refinable: false premises about the repo (rewrite the entry against the real code, including the extra work that reveals), change sets contradicting their linked decisions, missing test scenarios for stated invariants and failure paths, untestable scenarios (add one provable at that layer), and gaps whose resolution is forced once the repo is consulted.
Escalations - never applied by a refine agent mid-loop, queued for step 4 instead: anything that would flip a `✓` decision to a rejected alternative, change the user-visible scope or behavior, add or drop a dependency, or contradict the user's recorded intent.
A refine agent that cannot fix its finding without crossing that line marks it escalated and leaves the spec alone.
Refinements follow `scope`'s notation: decision entries keep their slugs and marks, change sets keep their numbering, new scenarios carry layer tags.
A refinement that adds or rewrites a decision entry, or a cross-boundary invariant, is promoted to the ledger immediately per step 5 - it does not wait for step 4.

## 4. Decide escalations

After the loop, decide each escalation yourself under the factory policy in [../../references/factory-run.md](../../references/factory-run.md); nobody is asked.
For each one, have the panel's finding, the alternatives with their tradeoffs in the ledger's notation, and the recommendation the repo evidence and the settled intent support best - when options are tied, the smallest, most reversible one closest to the existing code.
Apply the recommendation immediately with a refine pass: update the decision entry's marks and because clauses, rewrite the affected change sets and `tests:` lines, keep `scope`'s notation, and loop `lint-spec.py` until clean.
Verify the applied refinement yourself against the repo rather than re-running a panel for it, and record `Answered: factory policy (auto-decided) - {recommendation}` in the report.
When the decision settles or flips a decision, or changes a cross-boundary invariant, promote it to the ledger per step 5 as soon as the refine pass lands.

Defer instead, with exactly one `Kind:` per Deferred item, only when:

- `Kind: premise` - every option would invalidate the change's premise or contradict the user's recorded intent (authority level 1), or the change needs credentials or a destructive action the run may not take; the run parks for a human.
- `Kind: new-effort` - the resolution opens a genuinely new sub-effort with its own decision tree; name `scope` for that piece, and the run parks.
- `Kind: unanswered` - you applied the recommendation, but its refine pass could not pass `lint-spec.py` or your repo verification this run; the runner retries from the report.

A preference, an unfamiliar tradeoff, or a change to a `✓` decision is never a reason to defer: decide it.

Verdict: APPROVED when nothing is deferred - every finding was refined or auto-decided; APPROVED WITH DEFERRALS otherwise.

## 5. Promote to the ledger

Every settled decision entry and cross-boundary invariant that this run added or changed in `spec.md` - from refinement or from an auto-decided escalation - gets promoted the same run, so build never has to wait on a separate `scope` pass for it.
Apply the promotion test in [../../references/decision-ledger.md](../../references/decision-ledger.md): copy qualifying decisions into `docs/decisions.md` verbatim, dated, sourced to this spec, evidence marks included, and promote recurring rationales to `P-` principles under the same bar.
Promote cross-boundary invariants to `docs/contracts.md` under the same test, phrased for the relying side - same as `scope` phase 8.
A decision or invariant that fails the promotion test, or a `? verify:` mark still open, stays in `spec.md` only and is not forced into the ledger.

## 6. Panel mechanics

Lenses live in [references/lenses.md](references/lenses.md): `feasibility`, `completeness`, `consistency`, `testability` - all four, every round.
Run the batches on the transport selected by [../ship/references/orchestration.md](../ship/references/orchestration.md), which owns transport choice, result-file delivery, and the batch mechanics, with these substitutions:

- The artifact under review is `spec.md`, not a diff: hand each agent the spec path and the repo root instead of a diff file, and drop the diff-location line from the prompt contract.
- Lens definitions and shared rules come from this skill's [references/lenses.md](references/lenses.md).
- Verifiers refute findings against the spec file and the actual repo; `file`/`line` in a finding points into `spec.md` unless a repo path is named.
- Scratch root: `{scratch_dir}/round-{R}/` from the run file.

If no transport is available, stop and explain that this panel-based skill cannot preserve its verification contract.
Between the batches, number the findings the verifiers get, and after batch 2 aggregate:

```bash
python3 {ship-skill-root}/scripts/aggregate-findings.py plan {batch-1}
python3 {ship-skill-root}/scripts/aggregate-findings.py aggregate {batch-1} {batch-2} --kind spec \
  --expected feasibility,completeness,consistency,testability --out .dev/{plan-name}/spec-review_N.json
```

Rerun only the tasks `aggregate-findings.py pending` lists, once. The final round's record sits beside `spec-review_N.md` at the same index, and the runner's gate rejects it unless all four lenses returned valid results and every BLOCK and CONCERN has a verifier outcome.

## 7. Write the report

Write `.dev/{plan-name}/spec-review_N.md` at the next free index, one per run, covering all rounds:

```markdown
# Spec review N - {plan-name} - {date}

Verdict: APPROVED | APPROVED WITH DEFERRALS
Rounds: {R} - {finding counts per round}

## Refinements applied
### R1 - {lens} - {one-line title}
{spec.md:line} - {what was wrong} -> {what the spec says now}

## Escalations resolved
### E1 - {lens} - {one-line title}
Question: {the escalation} - Answered: factory policy (auto-decided) - {recommendation} -> {what the spec says now}

## Promoted to the ledger
{decision slug or contract entry} -> `docs/decisions.md` | `docs/contracts.md`

## Deferred
### D1 - {lens} - {one-line title}
Kind: premise | new-effort | unanswered
{why it could not be decided in this run}

## Strengths
{the good notes worth keeping, deduplicated}
```

## Wrap up

Open the chat summary with the table from `python3 {scope-review-skill-root}/../../scripts/skill-metrics.py end scope-review --count findings_verified=N --count findings_refuted=N --count refinements_applied=N --count escalated=N`, pasted verbatim.
Then summarize in the same message: the verdict, what was refined and what each auto-decision changed (so the loop's edits stay auditable after the fact), what was promoted to `docs/decisions.md` and `docs/contracts.md`, anything deferred with its kind, and a link to the report.
The `Verdict:` line and each Deferred item's single `Kind:` value are parsed by the runner's gate; write exactly one value each, never the template's alternatives.
Then write `scope-review-result.json` per [../../references/factory-run.md](../../references/factory-run.md) as the final action: `done` for APPROVED, `blocked` for APPROVED WITH DEFERRALS, a `premise.invalidated` or `scope.new_effort` condition for each `premise` or `new-effort` deferral (an `unanswered` one carries none, so the runner retries), `next: "build"`, and `counts.auto_decided`.
Never invoke `build` or `scope`; the runner decides the next stage from its gate.
