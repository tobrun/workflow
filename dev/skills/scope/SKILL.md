---
name: scope
description: Spec a change at .dev/{plan-name}/spec.md - interview for the real problem, argue each consequential decision against its alternatives, and write a self-contained spec with a layer-tagged change plan, then record the settled decisions in docs/decisions.md and boundary guarantees in docs/contracts.md. Use to plan work before implementation, for a quick plan of a small request or of accepted review findings, or for a deeper verified review of a large spec.
disable-model-invocation: true
---

# Scope

The spec is the deliverable: do not implement anything.
Another session, often another model, builds from the spec alone, so everything it needs to know goes in the file.
Paths follow [../../references/plan-layout.md](../../references/plan-layout.md), the decision notation follows [../../references/decision-ledger.md](../../references/decision-ledger.md), and authority and the confidence policy follow [../../references/execution.md](../../references/execution.md).

## Choose the path

- **Full** is the default: interview, argued decisions, a reviewer, promotion to the ledger.
- **Small** is for a small request with an obvious implementation, or for accepted review findings that are plain defects: no interview, no reviewer, at most one question and only when you cannot proceed without it, and a `D-` decision only where the code offered a real choice.
  Switch to full when a finding needs a recorded decision flipped or user-visible scope changed, or when the request turns out not to be small.

Tell the user which path you took.

## 1. Understand the problem

Create `spec.md` as soon as you know what the change is about, with its title, the date, what you understood, and empty `## Research`, `## Scope`, and `## Change plan` sections.
From then on write each answer, decision, and resolution into the file when it happens, so the file never lags the conversation.

A request often arrives one level too low: a solution hides the problem it solves, and a symptom hides its cause.
Ask what led to the request and what would look different if it worked, and check the data behind the premise before speccing a fix.
The user's original ask then becomes one alternative among others in the first decision; a premise that does not survive resolves as a `⊘` line.

Interview one consequential question at a time, using the host's structured user-input tool when available, each with your recommendation and its confidence.
Look at the code first so you ask only what the code cannot answer.
List the recommendations you applied without asking in one block, so a single reply can overturn any of them before later decisions rely on them.
Prefer the smallest spec that delivers observable behavior, but resolve any reduction of the requested scope with the user instead of deferring required work silently.

## 2. Argue the decisions

Find every consequential decision: the overall approach, the choices that look simple but change how much work is done (where a file is stored, the retry strategy), and the small values that change observable behavior (timeouts, value domains, data shapes).
How the change is verified is a decision too: which layer to test at, what needs a real dependency, what cannot be tested and why.
Routine implementation choices need no entry.

Each decision gets a stable `D-` slug, is phrased as a question, and lists at least two alternatives with because clauses, argued against what the codebase already does.
Write it as `[open]` when you find it and settle it in the file as the talk-through goes; `⚑` marks a line waiting on the user.
Talk the big and medium decisions through with the user, highest impact first; sketch taste-driven alternatives concretely instead of describing them.

Argue each decision on its merits before reading `docs/decisions.md`, then compare: a collision with a recorded entry is raised with the user and marked `⚑` until resolved, never silently overridden.

## 3. Write the spec

**Research**: one entry per decision, by impact.
When a later section references a decision, echo the chosen line's leading label in parentheses - `D-file-storage (✓ S3)`, `(open)`, `(⊘ not doing)` - so lead each chosen line with the short label you will echo.

**Scope**: inputs, outputs, invariants, and error handling.
An invariant another module will rely on without seeing the enforcing code is written in the notation of [../../references/contracts.md](../../references/contracts.md).
Second-order work becomes nested sub-efforts with their own decisions; weighed and cut options become `⊘` non-goals with a because clause.
End with a `### Validation` block of the repository's real typecheck, test, lint, and build commands, discovered from its manifests and CI config, never guessed.
List each once, and mark `(end of build)` the ones a build wave does not need, such as the e2e suite, benchmarks, or a coverage run that repeats the suite.

**Change plan**: short fragments, linking decisions by ID with their echo.
Each change set ends with exactly one `tests:` line of concrete scenarios, input -> expected outcome, each tagged `[unit]`, `[integration]`, or `[e2e]` and covering the happy path, edge cases, and failure paths; a set with nothing to test says `tests: none - {reason}`.
Order change sets so each builds only on earlier ones, keep their file lists disjoint so build can run them in parallel, and gather edits to a shared file into one change set.
Past 25 scenarios a change set is several, split along a seam.

```
1. Change set 1
   a. file 1 - describe changes - decisions: D-input-validation (✓ custom)
   b. file 2 - describe changes
   tests: [unit] payload missing name -> 400 naming the field; [integration] valid payload -> 200 and row written; [e2e] 11MB upload -> rejected before the transfer starts
```

Write the spec plainly: no similes or metaphors.
Loop `python3 {scope-skill-root}/scripts/lint-spec.py .dev/{plan-name}/spec.md` until it exits clean; it prints the build waves the file lists allow, and a change set queued behind a shared file is a hint to regroup.

## 4. Review (full path)

Hand a fresh-context subagent the spec file and the repository, not the conversation.
Its job: find decisions the spec makes without saying so, alternatives rejected without a reason, downsides it does not admit, and claims about the repo that are false.
Check each finding against the code yourself, fold in what holds, and ask the user about any that would change scope or a settled decision.

For a large or risky change, offer the user a deeper pass before finishing: four reviewers in parallel - feasibility against the repo, completeness of scenarios for every invariant and failure path, consistency between decisions and change sets, testability at each tagged layer - then a separate verifier per finding that tries to refute it against the repo.
Apply confirmed findings that stay within the settled decisions, ask the user about the rest, and lint again.

## 5. Record

Copy into `docs/decisions.md` every decision that passes the promotion test in [../../references/decision-ledger.md](../../references/decision-ledger.md), verbatim, dated, and sourced to this spec.
Promote cross-boundary invariants to `docs/contracts.md`, phrased for the relying side; a planned invariant keeps its `? verify:` until enforcing code exists.

Recommend `build`; never launch it.

## From review findings

When the plan directory holds `review_N.md` files, the highest-numbered one is the input.
On the small path, each accepted Blocker becomes one change set, and a Concern only when the user names it; on the full path, the accepted findings open the interview as decisions, and rejected or deferred ones land as `⊘` lines.
Read the code each finding names before writing its change set.
Append to the existing change plan with continued numbering - never renumber or rewrite what is there - and use the review's triggering scenario as the change set's test.
Apply the review's Decision reconciliation section to `docs/decisions.md`.
For these change sets, recommend `build` followed by `ship` in quick mode.
