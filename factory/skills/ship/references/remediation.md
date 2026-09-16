# Blocker Remediation

How `ship` handles a BLOCK verdict from the review panel: two autonomous rounds of fix and re-review before a blocker is real.
A confirmed blocker is a defect with a triggering scenario, and a defect with a triggering scenario is work, not a question - the same reasoning that lets the gauntlet loop its fix agents.
Only a blocker that survives two rounds on the same root cause is a real blocker.

## When it runs

After phase 2 writes `review_N.md` with verdict BLOCK, in the default flow.
Never in "review only" mode: that mode exists for diffs whose owner did not ask for mutations.
CONCERNS and PASS skip straight to phase 3; concerns stay in the report and the PR's Open calls, they never trigger a round.

## A round

1. **Fix.** Take every Blocker from the current `review_N.md` - each carries a `file:line`, a title, and the triggering scenario verification confirmed.
   Group blockers by independent area and dispatch one fresh-context fix agent per group, launched in a single message.
   Each agent gets only its blockers, the relevant file paths, the brief phase 2 built (spec excerpts, contracts, decision rationale), and the fix vocabulary below.
   Agents commit their fix on the run branch as a `type(scope): subject` line plus `What:` and `Why:` paragraphs, naming the review index and finding in the Why, with no `Co-Authored-By` line.
2. **Re-harden.** Run phase 1's five read-only analyzers over the files the round touched, plus the flakiness check over the tests it added; dispatch fixes and loop per [gauntlet.md](gauntlet.md) as usual.
   Then run the spec's Validation block (or the repo's test suite) and re-run the `[e2e]` scenarios when the round touched anything an e2e scenario exercises, overwriting the e2e record and re-rendering its HTML.
3. **Re-review.** Run phase 2 again as a re-review: `review_{N+1}.md`, same lenses, previous findings carried as verification items so the panel states whether each blocker is fixed or still open.
   The summary line names the round: `Remediation round {r} of 2`.
4. **Decide.** PASS or CONCERNS ends the loop and phase 3 opens a ready PR.
   BLOCK after round 1 starts round 2.
   BLOCK after round 2 ends the loop: every surviving blocker is a real blocker.

Round budget is two per ship run, not per blocker: a blocker the fixes introduced counts against the same budget, and a re-review that finds a new blocker after round 2 is a real blocker too.
A retried `ship` attempt starts a fresh budget.

## Fix vocabulary

- Fix the root cause the scenario names, never the symptom the lens observed; a fix that only makes the triggering scenario pass is a suppression.
- Add the test that reproduces the triggering scenario at its right layer, seen red before the fix and green after; a blocker with no test guarding it is not fixed.
- Stay inside the blocker's area: no refactors, no touching findings another agent owns, no concerns or nits.
- Never delete or weaken a test, a check, or a contract to make the blocker moot; never edit `docs/decisions.md` or `docs/contracts.md`.
- A blocker that cannot be fixed without changing specced behavior, a settled decision, or a contract the reliance sites still assume is still fixed: take the fix that changes the least, update every reliance site it touches, mark it `auto-decided` in the agent's result with the decision or contract it departs from, and let the re-review judge it. Only a fix on the factory park list - an invalidated premise, credentials, a destructive action - is marked `parked` and left alone.

## Real blockers

A real blocker is listed in the PR under Open calls with its history: the finding, the round-1 and round-2 attempts (commit and what each changed), and why it still stands - unfixable in scope, escalated, or a new blocker the fixes introduced.
Phase 3 opens the PR as a draft.
The wrap-up lists each real blocker in the PR's Open calls and records each `parked` fix as a typed condition in `ship-result.json`'s `conditions`; every `auto-decided` fix goes to the PR's Auto-decided section with the decision it departs from, so the reviewer sees what was chosen for them.
