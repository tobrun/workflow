---
name: scope-review
description: Review and auto-refine a settled spec before build starts - a fresh-context agent panel checks the plan against the actual repo for infeasible change sets, missing failure paths, semantic contradictions, and untestable scenarios, then verified findings are applied to spec.md by refine agents and the panel re-runs; findings only the user can decide are asked as questions at the end and their answers applied, so a finished run hands build a spec ready to implement. Use after scope settles a spec and before build implements it.
disable-model-invocation: true
---

# Scope Review

Review the spec with agents that did not write it, then refine it in place - before any implementation exists, and without a human in the loop.
A defect caught here costs a spec edit; the same defect after build costs a re-implementation, so this loop runs to completion on its own and ends with a spec build can start on - not a findings list to triage, and not a handoff back to `scope`.
The few findings only the user can decide are asked as questions at the end of the run, and the answers are applied before it finishes.
The panel judges the plan against the actual repo, not against the conversation that produced it.
You are the orchestrator: run tools, dispatch agents, apply the loop, report - your own reading of the spec is not a lens, and findings reach the spec only through verification.
This skill edits `spec.md` and nothing else: never code, never `docs/decisions.md`, never `docs/contracts.md`.

`scope`'s own phase-5 reviewer hunts while the spec is still being drafted, from the spec file alone.
This skill is the standalone deeper pass: fresh agents with repo access, adversarial verification, and automatic refinement - worth running when the change is large or risky, or when build will run in a different session.

Invoking this skill is the task - locate the spec yourself and start immediately; do not ask what to review.
First run `python3 {scope-review-skill-root}/../../scripts/skill-metrics.py start scope-review` so the wrap-up can measure this run.

## 1. Locate and gate

- Locate the plan directory per [../../references/plan-layout.md](../../references/plan-layout.md) and read its `spec.md`; no spec: say so and stop - there is nothing to review.
- Run `python3 {scope-skill-root}/scripts/lint-spec.py .dev/{plan-name}/spec.md` once. If it reports anything, stop and recommend finishing the `scope` run: refinement here presumes a mechanically settled spec, and repairing an unfinished draft is `scope`'s job, not this loop's.
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
Escalations collected across the rounds go to the interview below, not to a handoff.

## 3. Authority and refinement rules

Refine agents resolve conflicts by this order - each level beats everything below it:

1. The user's recorded intent: the spec's stated problem, scope, and interview outcomes.
2. The repo's reality: what the code actually contains beats what the spec claims about it.
3. A settled `✓` decision: a change set contradicting its linked decision is rewritten to match the decision, not the other way around.
4. The spec's prose.

Refinable: false premises about the repo (rewrite the entry against the real code, including the extra work that reveals), change sets contradicting their linked decisions, missing test scenarios for stated invariants and failure paths, untestable scenarios (replace with one provable at that layer), and gaps whose resolution is forced once the repo is consulted.
Escalations - never auto-applied, queued for the interview instead: anything that would flip a `✓` decision to a rejected alternative, change the user-visible scope or behavior, add or drop a dependency, or contradict the user's recorded intent.
A refine agent that cannot fix its finding without crossing that line marks it escalated and leaves the spec alone.
Refinements follow `scope`'s notation: decision entries keep their slugs and marks, change sets keep their numbering, new scenarios carry layer tags.

## 4. Resolve escalations with the user

After the loop, ask the user each escalation as a decision, one consequential question at a time, using the host's structured user-input tool when available - the same interview discipline `scope` uses.
Each question carries what the panel found, the alternatives with their tradeoffs in the ledger's notation, and a recommendation when one is defensible; the user is deciding, not triaging raw findings.
Apply each answer immediately with a refine pass: update the decision entry's marks and because clauses, rewrite the affected change sets and `tests:` lines, keep `scope`'s notation, and loop `lint-spec.py` until clean.
An answer that resolves cleanly in place ends that escalation; verify the applied refinement yourself against the repo rather than re-running a panel for it.

Two outcomes defer instead of resolving:

- An answer that invalidates the change's premise or opens a genuinely new effort - a new sub-effort with its own decision tree - exceeds a Q&A; record it as deferred and name `scope` for that piece.
- No interactive channel, or the user declines to answer: record the escalation as deferred with the question it still needs.

Verdict: APPROVED when nothing is deferred - every finding was refined or answered; APPROVED WITH DEFERRALS otherwise.

## 5. Panel mechanics

Lenses live in [references/lenses.md](references/lenses.md): `feasibility`, `completeness`, `consistency`, `testability` - all four, every round.
Run the batches on the transport selected by [../ship/references/orchestration.md](../ship/references/orchestration.md), which owns transport choice, result-file delivery, and the batch mechanics, with these substitutions:

- The artifact under review is `spec.md`, not a diff: hand each agent the spec path and the repo root instead of a diff file, and drop the diff-location line from the prompt contract.
- Lens definitions and shared rules come from this skill's [references/lenses.md](references/lenses.md).
- Verifiers refute findings against the spec file and the actual repo; `file`/`line` in a finding points into `spec.md` unless a repo path is named.
- Scratch root: `/tmp/scope-review-{session-id}/round-{R}/`.

If no transport is available, stop and explain that this panel-based skill cannot preserve its verification contract.
Between the batches, number the findings the verifiers get, and after batch 2 aggregate:

```bash
python3 {ship-skill-root}/scripts/aggregate-findings.py plan {batch-1 results}
python3 {ship-skill-root}/scripts/aggregate-findings.py aggregate {batch-1 results} {batch-2 results} --expected {lenses}
```

## 6. Write the report

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
Asked: {the question} - Answered: {the user's decision} -> {what the spec says now}

## Deferred
### D1 - {lens} - {one-line title}
{the question still open, and why it exceeded this run: premise invalidated, new effort, or unanswered}

## Strengths
{the good notes worth keeping, deduplicated}
```

## Wrap up

Open the chat summary with the table from `python3 {scope-review-skill-root}/../../scripts/skill-metrics.py end scope-review --count findings_verified=N --count findings_refuted=N --count refinements_applied=N --count escalated=N`, pasted verbatim.
Then summarize in the same message: the verdict, what was refined and what the user's answers changed (so the loop's edits stay auditable after the fact), anything deferred with its open question, and a link to the report.
Recommend next steps, never invoking them:

- `build` when the verdict is APPROVED - the spec was refined, the questions are answered, and implementation can start directly.
- With deferrals: `scope` for the deferred pieces (its remediation mode reads this report); the rest of the spec is still build-ready when the deferred work is separable.
