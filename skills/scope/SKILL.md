---
name: scope
description: Spec a change at .dev/{plan-name}/spec.md - find the real problem, argue each consequential decision against its alternatives, and write a self-contained spec with a layer-tagged change plan and an HTML review page rendered from it, then record the settled decisions in docs/decisions.md and commit that ledger. Use to plan work before implementation, for a quick plan of a small request or of accepted review findings, or for a deeper verified review of a large spec.
compatibility: Needs git and Python 3.10+.
metadata:
  version: "3.10.0"
  source: "https://github.com/tobrun/workflow"
  invocation: "explicit - recommend it, never run it unasked"
---

# Scope

The spec is the deliverable: do not implement anything.
Another session, often another model, builds from the spec alone, so everything it needs to know goes in the file.
Paths follow [../../references/plan-layout.md](references/plan-layout.md), the decision notation follows [../../references/decision-ledger.md](references/decision-ledger.md), and authority and the confidence policy follow [../../references/execution.md](references/execution.md).

## Choose the path

- **Full** is the default: research, argued decisions, a reviewer, promotion to the ledger.
- **Small** is for a small request with an obvious implementation, or for accepted review findings that are plain defects: no reviewer, at most one question and only when you cannot proceed without it, and a `D-` decision only where the code offered a real choice.
  Switch to full when a finding needs a recorded decision flipped or user-visible scope changed, or when the request turns out not to be small.

Tell the user which path you took.

## 1. Understand the problem

Create `spec.md` as soon as you know what the change is about, with its title, the date, what you understood in one or two sentences, a `Risk: low|medium|high - why` line naming what raises the stakes (public APIs, schemas, auth, data loss, migrations), and empty `## Research`, `## Scope`, and `## Change plan` sections.
From then on write each answer, decision, and resolution into the file when it happens, so the file never lags the conversation.

A request often arrives one level too low: a solution hides the problem it solves, and a symptom hides its cause.
Ask what led to the request and what would look different if it worked, and check the data behind the premise before speccing a fix.
The user's original ask then becomes one alternative among others in the first decision; a premise that does not survive resolves as a `⊘` line.

Answer your own questions first: read the code, the data, and the request, and give each open question a recommendation with its confidence.
Apply a recommendation at `Confidence: 50%` or above and write `auto-applied at Confidence: NN%` on its chosen line.
Ask the user only below 50%, or when the answer changes the requested problem, drops a requirement, or adds a non-goal; ask one question at a time, using the host's structured user-input tool when available.
List the recommendations you applied without asking in one block, so a single reply can overturn any of them before later decisions rely on them.
In that block and in every question, name a decision by its title, not its slug, per the ledger's "Naming decisions for people".
Prefer the smallest spec that delivers observable behavior, but never shrink the requested scope without the user.

## 2. Argue the decisions

Find every consequential decision: the overall approach, the choices that look simple but change how much work is done (where a file is stored, the retry strategy), and the small values that change observable behavior (timeouts, value domains, data shapes).
How the change is verified is a decision too: which layer to test at, what needs a real dependency, what cannot be tested and why.
Routine implementation choices need no entry.

Each decision gets a stable `D-` slug, is phrased as a question ending in an `[impact: high|medium|low]` tag (how much changes if it is decided wrong), and lists at least two alternatives with because clauses, argued against what the codebase already does.
Write it as `[open]` when you find it and settle it in the file under the confidence rule above, highest impact first; `⚑` marks a line waiting on the user.
When you do ask about a taste-driven choice, sketch the alternatives concretely instead of describing them.

A decision about structure - which module may depend on which, a public entry point, a banned API, a dependency policy - lands on the strongest level of the ladder in [../../references/architecture.md](references/architecture.md) that can hold it.
When the repository keeps `docs/architecture.toml` and the decision lands on a rule, the change plan edits that file (with fixtures for a banned API), runs `python3 .arch/arch.py generate`, and gives the decision an `enforced by:` line; loosening a rule is a scope change the user resolves.

Argue each decision on its merits before reading `docs/decisions.md`, then compare: a collision with a recorded entry is raised with the user and marked `⚑` until resolved, never silently overridden.

## 3. Write the spec

**Research**: one entry per decision, by impact.
When a later section references a decision, echo the chosen line's leading label in parentheses - `D-file-storage (✓ S3)`, `(open)`, `(⊘ not doing)` - so lead each chosen line with the short label you will echo.

**Scope**: inputs, outputs, invariants, and error handling.
Second-order work becomes nested sub-efforts with their own decisions; weighed and cut options become `⊘` non-goals with a because clause.
End with a `### Validation` block of the repository's real typecheck, test, lint, and build commands, discovered from its manifests and CI config, never guessed.
List each once, and mark `(end of build)` the ones a build wave does not need, such as the e2e suite, benchmarks, or a coverage run that repeats the suite.
Then a `### Verification` block marking each of the twenty kinds in [../../references/verification.md](references/verification.md) `yes - how` or `no - reason`, judged by the triggers there; `formal: yes` names its mechanism as a decision (a TLA+ or Alloy model, or a stateful `[property]` test).
When `threat-model` applies, a `### Threat model` block: per trust boundary, each STRIDE threat with its mitigation and the scenario that tests it.
Only when the change adds or reorders calls between components, or moves a boundary, add a `### Flow` block holding one fenced `mermaid` sequence or component diagram of the new flow, with the changed parts named; a local fix gets none.

**Change plan**: short fragments, linking decisions by ID with their echo.
Each change set ends with exactly one `tests:` line of concrete scenarios, input -> expected outcome, each tagged with the layer that proves it - `[unit]`, `[component]`, `[integration]`, `[contract]`, `[property]`, `[fuzz]`, `[e2e]`, `[smoke]`, or `[benchmark]` - and covering the happy path, edge cases, and failure paths; a set with nothing to test says `tests: none - {reason}`.
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

On the full path, run `python3 {scope-skill-root}/scripts/render-spec.py .dev/{plan-name}/spec.md --open` once lint is clean, and again after every later edit to the spec.
It writes `spec.html` beside the spec: the decisions waiting on the user, the recommendations applied without asking with reply buttons that overturn them, decisions by impact and confidence, scope, tests per layer, and build waves, all counted from the file.
Never edit the page by hand; fix the spec and render again.

## 4. Review (full path)

Hand a fresh-context subagent the spec file and the repository, not the conversation.
Its job: find decisions the spec makes without saying so, alternatives rejected without a reason, downsides it does not admit, and claims about the repo that are false.
Check each finding against the code yourself, fold in what holds, and ask the user about any that would change scope or a settled decision.

For a large or risky change, offer the user a deeper pass before finishing: four reviewers in parallel - feasibility against the repo, completeness of scenarios for every invariant and failure path, consistency between decisions and change sets, testability at each tagged layer - then a separate verifier per finding that tries to refute it against the repo.
Apply confirmed findings that stay within the settled decisions, ask the user about the rest, and lint again.

## 5. Record

Copy into `docs/decisions.md` every decision that passes the promotion test in [../../references/decision-ledger.md](references/decision-ledger.md), verbatim, dated, and sourced to this spec.

Commit `docs/decisions.md` if this run changed it, and nothing else, so `build` starts from a clean tree; the plan directory stays out of git, because plan files travel with the run instead.
Commit on the current work branch; on the default branch, create `dev/{plan-name}` from HEAD first, a branch change that waits for the user's consent where the repository requires it.
Write the message per the commit skill's [message format](../commit/references/message-format.md), typed `docs`, such as `docs({plan-name}): record the spec's decisions`.

Give the user the path to `spec.html` on the full path, and recommend `build`; never launch it.

## From review findings

When the plan directory holds `review_N.md` files, the highest-numbered one is the input.
On the small path, each accepted Blocker becomes one change set, and a Concern only when the user names it; on the full path, the accepted findings become decisions, and rejected or deferred ones land as `⊘` lines.
Read the code each finding names before writing its change set.
Append to the existing change plan with continued numbering - never renumber or rewrite what is there - and use the review's triggering scenario as the change set's test.
Apply the review's Decision reconciliation section to `docs/decisions.md`, and commit the ledger as in Record.
For these change sets, recommend `build` followed by `ship` in quick mode.
