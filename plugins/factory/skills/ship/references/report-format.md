# Review Report Format

The shape of `.dev/{plan-name}/review_N.md`, written by phase 2 at the next free index starting at 1.
`REVIEW_DATA` ([data-schema.md](data-schema.md)) mirrors this file section for section; the aggregator's JSON fills both.

```markdown
# Review {N}: {title}

Verdict: {PASS | CONCERNS | BLOCK}
Panel: {lenses run}, {failed lenses if any}
Base: {branch or PR}, {date}

{1-2 sentence summary}

## Spec conformance

Tests: scenarios met/not met, invariants held, seams tested/untested. Omit if no spec.

## Decision reconciliation

{only when the repo keeps docs/decisions.md} Each colliding finding: still-holds (with the checked reopen condition), reopened, or diverged.

## Previous findings

{re-review only} Each finding from review_{N-1}: fixed or still open.

## Blockers

[{lenses}] {file:line} - {title}
{The scenario that triggers it, confirmed by verification.}

## Concerns

Same shape as blockers. A finding the verifier could not confirm belongs here, not in Blockers.

## Nits

| File | Lens | Issue |

## What's good

One line per lens; omit empty ones.

## Next step

What to fix first and why.
```

`scope` reads this file when the user accepts findings that need real work: its Blockers and Concerns open that run's interview, and its Decision reconciliation section is what gets applied to `docs/decisions.md`.
Write it for that reader - a finding with no triggering scenario cannot become a decision.
