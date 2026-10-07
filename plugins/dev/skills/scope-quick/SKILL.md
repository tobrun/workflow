---
name: scope-quick
description: Turn a ship review's findings, or a small request, into minimal change sets in .dev/{plan-name}/spec.md with no interview, no argued decisions, and no subagents, so build can start at once. Use when the user asks for a quick scope, wants review findings fixed without a full scope run, or has a small change that does not need its decisions argued.
---

# Scope Quick

Write the smallest change plan `build` can execute, and nothing else.
This is `scope` without the interview, the decision catalog and its evidence batch, the blind-spot pass, the spec reviewer, the ledger reconcile and promotion, the HTML render, and the retrospective.
Launch no subagents, and ask at most one question, only when you cannot proceed without the answer.

First run `python3 {scope-quick-skill-root}/../../scripts/skill-metrics.py start scope-quick`.
Locate the plan directory per [../../references/plan-layout.md](../../references/plan-layout.md).

## From review findings

When the plan directory holds a finished `review_N.md` and the user named no other request, the highest-numbered one is the input.

- Every Blocker becomes one change set; a Concern becomes one only when the user names it.
- Append to the existing `spec.md` change plan with continued numbering - never renumber, never rewrite what is there.
- With no `spec.md`, create one in the shape below and take the title and scope from the review's summary.
- Read the code each finding names before writing its change set, so the files and the fix are real rather than repeated from the report.

## From a small request

Read the code the request touches yourself, then write `spec.md` in the shape below.
Record a `D-` decision only where the code offered a real choice; most quick specs have none.

## The spec shape

```markdown
# {title}

## Research

{Decisions, or one line saying there was no real choice.}

## Scope

{What changes and what does not, in a few lines.}

### Validation

{The repo's real test and typecheck commands, discovered, one per line.}

## Change Plan

1. {The change in one line}
   a. `{file}` - {what changes in it}
   tests: [unit] {scenario} -> {expected result}
```

Each change set ends in exactly one `tests:` line, tagged at the lowest layer that proves it.
A change set that fixes a finding uses the review's triggering scenario as its test.

## Finish

Loop `python3 {scope-quick-skill-root}/../scope/scripts/lint-spec.py .dev/{plan-name}/spec.md` until it exits clean.
Stop and recommend `scope` instead of guessing when a finding needs a recorded decision flipped or user-visible scope changed, or when the request turns out not to be small.
Close with `python3 {scope-quick-skill-root}/../../scripts/skill-metrics.py end scope-quick --count change_sets=N`, pasting its table verbatim, and list the change sets you wrote.
Then recommend the next steps, never launching them: `build`, followed by `ship-quick` when these change sets fix a review, or `ship` for a change that has not been shipped before.
