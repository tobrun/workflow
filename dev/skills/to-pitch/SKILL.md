---
name: to-pitch
description: Package a completed change - its spec, implementation notes, and verification evidence - into a local or shareable buy-in document. Use once implementation is done and the change needs buy-in or visibility beyond the author, not for trivial one-line fixes.
disable-model-invocation: true
---

# To Pitch

Turn finished work into a doc that gets someone else to say yes: demo first, then the reasoning, then the evidence.
This is purely presentational - it never gates anything and never implies otherwise.

## Right-sizing

A one-line fix or an internal refactor with no visible behavior change doesn't warrant buy-in.
Say so and skip rather than manufacture ceremony.

## Workflow

1. Locate the spec directory per [../../references/plan-layout.md](../../references/plan-layout.md).
2. Read whatever exists, degrading gracefully when something doesn't:
   - `.dev/{plan-name}/spec.md` - the scope, change plan, and research sections.
   - `.dev/{plan-name}/implementation-notes.md` (from `build`) - what was done, and any Deviations.
   - `/tmp/{project-slug}/reports/{plan-name}-e2e-report.html`'s data block - verification evidence, read per the rules in [../../references/reporting.md](../../references/reporting.md).
3. Capture the demo. Start the app launch in the background first and draft the Why, What changed, and How-verified sections from the step 2 artifacts while it boots - the pitch's slowest step should overlap its writing. For UI-affecting changes, use an installed `run` skill when direct skill invocation is available; otherwise launch the app from its documented command. Record a short clip with available browser automation when GIF capture exists, or use a concise screenshot sequence otherwise. For non-UI changes, lead with the clearest before/after example.
4. Map everything onto `PITCH_DATA` per [references/data-schema.md](references/data-schema.md) in this section order: Demo, Why, What changed, How we verified it, Deviations (omit the section entirely when none were logged), Try it yourself.
5. Render [templates/pitch.html](templates/pitch.html) to `/tmp/{project-slug}/reports/{plan-name}-pitch.html`, opening and publishing per [../../references/reporting.md](../../references/reporting.md) (stable pitch favicon; title and description from the spec).

## What good pitch content looks like

- Demo first, always - a reviewer who only looks at the top of the page should already get it.
- Why: the goal and the problem in the reviewer's terms, not implementation detail - a pitch that can say "we rejected X because Y" from the spec's research section is stronger than one that only recaps the goal.
- What changed: a real before/after plus the concrete files touched, not a restated diff.
- How it was verified: the e2e summary and the test layers actually exercised, as evidence - "14/14 scenarios passing across unit, integration, e2e" beats "thoroughly tested".
- Deviations: what edge case forced the conservative call, and why it still holds.
- Try it yourself: concrete steps a reviewer follows to run or reach the change themselves.
