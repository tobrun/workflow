---
name: to-quiz
description: Render a context/intuition/what-was-done document with a graded comprehension quiz on finished work. Use when the user wants a local or shareable comprehension check before a change merges.
---

# To Quiz

Turn finished work into a comprehension check: enough context to understand the change, then a quiz the reader must actually pass.

This is a client-side comprehension check, not an enforced merge gate.
Say so visibly; suggest external enforcement only if requested, and do not configure it here.

## Workflow

1. Locate the spec directory per [../../references/plan-layout.md](../../references/plan-layout.md).
2. Read whatever exists, degrading gracefully when something doesn't, shaped for comprehension-checking rather than persuasion:
   - `.dev/{plan-name}/spec.md` - the research decisions (`✓` chosen, `✗` rejected, `⚠` accepted downsides), the scope invariants and error handling, and the change plan's layer-tagged `tests:` scenarios (including edge cases).
   - `.dev/{plan-name}/implementation-notes.md` (from `build`) - what was done, Deviations.
   - `/tmp/{project-slug}/reports/{plan-name}-e2e-report.html`'s data block - concrete scenarios to quiz on, read per the rules in [../../references/reporting.md](../../references/reporting.md).
3. Write Context (what this change is and why), Intuition (the one key design insight or tradeoff a reviewer needs to *get*, in plain language - the spec's most consequential `✓`/`⚠` pair is the natural source), and What was done.
   Write 3-6 quiz questions, each generated from something real: a decision's chosen-over-rejected rationale, a scope invariant or error-handling rule, a `tests:` scenario, or a logged deviation - never a generic question a reader could pass without having read anything. Each needs a correct answer, 2-3 plausible wrong answers, an explanation, and a link back to whichever section of this same document (Context, Intuition, or What was done) backs the right answer. The spec's `✗` rejected alternatives make ideal plausible wrong answers.
4. Map everything onto `QUIZ_DATA` per [references/data-schema.md](references/data-schema.md): sections Context, Intuition, What was done, then the client-side quiz - graded entirely in-browser JS, no server, no network calls.
5. Render [templates/quiz.html](templates/quiz.html) to `/tmp/{project-slug}/reports/{plan-name}-quiz.html`, opening and publishing per [../../references/reporting.md](../../references/reporting.md) (stable quiz favicon; title and description from the spec). Remind the user that the quiz is a comprehension check they take themselves, not an automated gate.

## Writing good questions

- Wrong answers should be plausible misreadings of the change, not absurd distractors - a question only a careless reader would miss is testing attention, not understanding.
- The explanation for a missed question should teach, not just state the answer.
