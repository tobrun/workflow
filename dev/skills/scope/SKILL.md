---
name: scope
description: Spec a change by interviewing for the real problem, arguing every design decision against alternatives, and writing a self-contained spec with a change plan at .dev/{plan-name}/spec.md. Use to plan work before implementation, to turn accepted review findings into new change sets, or to audit the decisions already embedded in existing code or commit history.
disable-model-invocation: true
---

# Scope

The spec is the deliverable: do not implement anything.
It is a fresh-context handoff - planning happens here, implementation happens in another session (often another model) that reads only the spec.

The numbered sections are an addressing scheme, not a march. Four orderings are load-bearing: catalog decisions before arguing them, argue them before reading the project's ledger, settle the spec before promoting anything out of it, and never let the change plan outrun an open decision. The rest is judgment - work it in whatever order the change calls for.
Before the interview, run `python3 {scope-skill-root}/../../scripts/skill-metrics.py start scope` so the retrospective can measure the run.

## 1. Interview

Interview the user until you understand what they want, one consequential question at a time, using the host's structured user-input tool when available. Then write down what you understood.

The request often arrives one level too low: a solution ("add rate limiting with Redis") hides the problem it solves, a symptom hides the cause that picks the fix.
Climb up before interviewing about the change itself - what led to this, what they observed, what would look different if it worked - then pressure-test the premise: what data shows the problem is real, and does it point where they think? Committing to a fix before the cause means speccing the wrong change well.
Once the problem holds up, brainstorm alternatives at the problem level, including other layers entirely (rate limit at the ALB instead of in the app); the user's original ask becomes one alternative among them, and the whole question lands in the research section as the first decision.
If the premise doesn't survive, that resolves as a `⊘` not-doing line, not as a failed interview.

The interview is latency-bound by the user, so hide machine work inside it: once the change's rough area is clear, launch background read-only subagents on phase 2's prior-art hunt - existing idioms, helpers, earlier attempts, the areas the change touches - so their results are waiting when the interview closes.
These explorers never read `docs/decisions.md`: the ledger must stay out of context until the phase 5 reconcile, and phase 9 treats an early leak as contamination.
The same prefetch arms the quiz below with real code to ask about.

Then size the change: **small** - a handful of decisions, an area the user knows, limited blast radius - or **full**, everything else.
Tell the user which you picked and why; they can override. Small changes run the lighter variants where marked below - the spec file, its argued decisions, scope, and change plan always happen.

**Iterate small.** Bias toward the smallest spec that delivers observable behavior - a slice implemented and looked at teaches more than a bigger plan.
A big request becomes a first slice specced now, with later slices as `⊘` lines naming what reopens them; reopen the spec per "Starting from review findings" once build and ship land. The ledger carries the decisions across slices, so going small loses nothing argued.

For full-size changes, quiz the user on the code and domain involved: medium to hard, zero jargon, each question carrying enough context to be answerable, enough of them to find where the understanding is thin.
Afterwards tell them what they understand well, what they don't, and their unknown unknowns - things that would affect their decisions on this task.

## 2. Catalog the decisions

Look at the code, starting from the phase 1 explorers' results - prior art first: existing idioms, helpers, and earlier attempts - so decisions get argued against what the codebase already does, not from scratch. Identify all the design decisions involved:

- **Big** - overall approach.
- **Medium** - things that seem simple but can cause big shifts in how much work is done (store a file on S3 or locally, retry strategy). The trickiest tier - make sure you catch all of them.
- **Small** - timeout lengths, shapes of data structures.

Pay special attention to edge cases and error handling. How the change gets verified is a decision too: what level to test at, what needs a real dependency versus a fake, what can't be tested and why.

**Blind spot pass** (full-size only): don't grade your own catalog - you'll reread it the way you wrote it. Get a second one from something that hasn't seen your reasoning, reliably a subagent handed only the user's original request and the relevant code - not the conversation, not your catalog.
Both of its inputs are settled when the interview ends, so launch it before you start cataloging; it hunts while you do, and the fold-in is the sync point.
Fold the diff in: what it found and you didn't are blind spots, what you found and it didn't deserves a second look.
Then tell the user what their framing didn't account for: constraints already in the code, behavior the change would break, second-order work, and what a mature solution handles in this domain that they wouldn't know to ask about.

Between the catalog and the talk, gather each big and medium decision's evidence - call sites, existing constraints, what the code already does - in one parallel subagent batch, one agent per decision area, so each discussion starts armed instead of pausing to grep.
Talk through the big and medium decisions with the user, highest-impact first; when a decision's criteria are taste-driven, sketch the alternatives concretely instead of describing them in prose.

## 3. Research section

Pick a kebab-case `{plan-name}` naming the outcome and write `spec.md` in the plan directory ([../../references/plan-layout.md](../../references/plan-layout.md)), dated under its title, in three sections: `## Research`, `## Scope`, `## Change plan`.
Research comes first, one entry per decision, ordered by impact.
Every decision gets a stable ID: `D-` plus a short kebab slug (`D-file-storage`); keep a slug once assigned.

Each entry: the decision phrased as a question, then one line per alternative in the mark and because-clause notation of [../../references/decision-ledger.md](../../references/decision-ledger.md), which owns it.
Evidence marks apply from phase 1 onward, so the premise pressure-test data lands here cited or marked.
A decision still being argued is marked `[open]`; one that resolves to not doing it gets `✗` on every alternative and a closing `⊘`.
`⚑` marks a line waiting on the user. It may stay open in research and scope, but the change plan never links a decision that is `[open]` or carries one.

```
D-input-validation: How should input be validated?
  ✓ custom validation - rules fit in ~20 lines, no new dependency ⚠ we maintain edge cases ourselves
  ✗ validation library - adds a dependency for one call site

D-file-storage: Where do uploaded files live? [open]
  ? S3 - survives redeploys, needs bucket + IAM work
  ⚑ ask: expected file sizes and retention?

D-response-caching: Should responses be cached?
  ✗ Redis - operational burden for an unproven need
  ⊘ not doing - no measured latency problem ? verify: p95 from prod metrics; reopen if p95 exceeds 500ms
```

When a later section references a decision, echo the resolution in parentheses - `D-file-storage (✓ S3)`, `(open)`, `(⊘ not doing)` - so the reader only jumps back for the why.

## 4. Scope section

Record the scope: inputs, outputs, invariants, and error handling.
An invariant that crosses a boundary - another module will rely on it without seeing the enforcing code - gets drafted in contract notation ([../../references/contracts.md](../../references/contracts.md)) so phase 8 can promote it.
A change that adds a module or dependency edge, or collides with `docs/dependencies.md`, is a decision with the alternatives named in [../../references/dependency-rules.md](../../references/dependency-rules.md); the chosen resolution edits that file inside a change set, never implicitly.
Efforts have second-order effects - capture them as nested sub-efforts, each carrying its own decisions back into the research section (rate limiting in scope means Redis setup, which carries config and deploy decisions).
Record considered non-goals as `⊘` lines with a because clause - things someone weighed and cut, not mere omissions.
End the scope with a `### Validation` block listing the repo's real typecheck/test/lint/build commands, discovered from `package.json`, a `Makefile`, CI config, or equivalent - never guess `npm test` into a `pytest` repo; ask if you cannot determine them.
Writing style for the spec: ELI12, no similes or metaphors.

## 5. Review and research

1. Spawn a subagent reviewer with the spec file only - not the conversation, not your reasoning - before starting any of the work below, so it hunts while you research. Give it a hunting job, not a checklist: find the decisions this spec makes without realizing it, the alternatives rejected without a stated reason, and the chosen options whose downsides the spec doesn't admit. It succeeds by finding problems; "looks complete" is a failed review. For small changes, run this hunt yourself against the spec file.
2. While it runs, research how to implement everything: exact call sites, APIs, the idioms from the phase 2 prior-art hunt. Ask the user when you hit weird stuff.
3. Also while it runs, reconcile the drafted decisions against the project's `docs/decisions.md`, if it keeps one, per the recommender contract in [../../references/decision-ledger.md](../../references/decision-ledger.md). The decisions were argued fresh, so this diff means something; a `diverged` classification is raised with the user and marked `⚑` until resolved.
4. When the reviewer returns, ask remaining questions and update the doc. Anything still unanswerable stays a `⚑` line.

## 6. Change plan

Short fragmented sentences. Link decisions by ID wherever one applies, echoing the choice.
Each change set ends with one `;`-separated `tests:` line - concrete scenarios as input -> expected outcome, each tagged `[unit]`, `[integration]`, or `[e2e]`, covering happy path, edge cases, and failure paths; a set with nothing to test says `tests: none - {reason}`.
Specific enough that whoever writes the tests invents nothing; the author tags layers here because a fresh implementation session can't recover that intent.
Order change sets so each builds only on the ones before it; keep file lists disjoint where possible - `build` parallelizes consecutive change sets whose files don't overlap.

```
1. Change set 1
   a. file 1 - describe changes - decisions: D-input-validation (✓ custom)
   b. file 2 - describe changes
   tests: [unit] payload missing name -> 400 naming the field; [integration] valid payload -> 200 and row written; [e2e] 11MB upload -> rejected before the transfer starts

2. Change set 2
   a. file 3 - describe changes
   tests: none - deploy config only, verified by the deploy itself
```

Then loop `python3 {scope-skill-root}/scripts/lint-spec.py .dev/{plan-name}/spec.md` until it exits clean.
It owns the mechanics above; the spec is not final while it reports anything.

## 7. Visualize

Full-size changes only. Map the settled spec onto `SPEC_DATA` per [references/data-schema.md](references/data-schema.md) and render [templates/spec.html](templates/spec.html) to `/tmp/{project-slug}/reports/{plan-name}-spec.html`, opening and publishing per [../../references/reporting.md](../../references/reporting.md).
Once the spec is settled, this phase, the phase 8 promotion, and the Jira sync have no ordering between them - overlap them rather than running a march.

## 8. Promote to the ledger

Copy into `docs/decisions.md` every decision that passes the promotion test in [../../references/decision-ledger.md](../../references/decision-ledger.md).
Entries go in verbatim, dated, sourced to this spec, evidence marks included; a `? verify:` never gets silently dropped, and promotion is the cheapest moment to check what's checkable now.
Promote recurring rationales to `P-` principles per the ledger's bar.
Cross-boundary invariants from phase 4 promote to `docs/contracts.md` under the same test, phrased for the relying side.
Update the familiarity line of each area the phase 1 quiz covered.

## 9. Run retrospective

Audit the run itself. Four checks, each reported as a typed line - silence reads as "never ran":

- **Contamination** - `clean | contaminated`. Did the ledger enter context before the phase 5 reconcile? If so, name the decisions drafted after exposure: their reconcile outcomes are inherited, and a `still-holds` on them proves nothing.
- **Sizing** - `held | mis-sized`. Did the small/full call survive? Name the evidence when it didn't.
- **Catalog gaps** - `none | gap`. What did the reviewer or blind-spot pass find that phase 2 should have caught? A missed *category* is a proposed edit to the phase 2 list - propose it to the user, never apply it silently.
- **Familiarity** - `matched | adjusted`. Did the decision talk match what the quiz predicted? If not, amend the phase 8 familiarity line and say so.

Then print the measured run metrics with `python3 {scope-skill-root}/../../scripts/skill-metrics.py end scope --count decisions=N --count change_sets=N --count scenarios=N`, pasting its table verbatim.
Then recommend next steps, never launching them: `scope-review` first when the change is large or risky or build will run in a different session - it reviews the spec with a fresh-context panel and refines it in place before any code exists - and `build` to implement.

## Jira sync

Read `.dev/config.json`; when `jira.enabled` is true, follow [../../references/jira.md](../../references/jira.md) from before the first `acli` call - it owns the command shapes, the Initiative/Epic/Task timing, and the failure protocol.
With an absent or disabled config, no Jira behavior or mention.

## Starting from review findings

When the plan directory ([../../references/plan-layout.md](../../references/plan-layout.md)) holds `review_N.md` or `spec-review_N.md` files, the highest-numbered review's accepted Blockers and Concerns are the interview's opening agenda: each becomes an open decision, and rejected or deferred findings land as `⊘` lines so they are visibly not dropped.
Append new change sets to the existing `spec.md` with continued numbering - never renumber - and apply the review's Decision reconciliation section to `docs/decisions.md`.
A defect change set includes the review's triggering scenario as its reproduction.

## Reverse mode

When the user points at existing code instead of a planned change ("audit the decisions in the sync layer"), follow [references/reverse-mode.md](references/reverse-mode.md): inventory the decisions already embedded in the code, reconcile them against the ledger, and write ratified entries and unexamined defaults out - no spec file is produced.
To seed a ledger from commit history instead of code, follow [references/bootstrap.md](references/bootstrap.md).
