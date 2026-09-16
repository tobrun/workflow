# Decision Ledger

`docs/decisions.md` in the consuming project: one repo-tracked file that accumulates design decisions across specs and audits.
Spec files are per-change and live in `.dev/{plan-name}/`; the ledger holds what outlives a change - decisions a future spec or audit could collide with.
It is what stops a settled question from being re-litigated every run.

## Notation

The marks, used identically here and in a spec's research section: `✓` chosen, `✗` rejected, `?` open, `⚠` accepted downside, `⊘` not doing (with the condition that would reopen it), each line with a short "because" clause.
This file is the canonical definition; consumers cite it rather than restating it.
Every entry keeps its `D-` slug and adds a date and a source - the spec file (`{plan-name}/spec.md`) or audit that produced it.

### Evidence marks

A because clause often leans on a claim about the world - "no abuse observed", "matches user expectations".
When the claim is an observed fact that would flip the decision if false, it carries one of two marks:

- **A citation, in parentheses** - the claim was checked when written.
  Code is cited by function name, never line number (line numbers rot on every edit; a function name survives until a rename).
  Data is cited by its source - the log, query, metric, or dashboard.
  A user statement is cited as `user (<date>)`.
  The entry's date scopes the citation: it means *verified then*, and consumers judge staleness at read time like everything else here.
- **`? verify: <what would confirm it>`** - nobody checked, even at write time.
  Inline `?` after a claim is distinct from a line-leading `?` (an open alternative) by position.

Judgment clauses - "operational burden", "adds a dependency for one call site" - are arguments, not evidence, and take no mark.
The mark is only signal while it's rare; marking every clause drowns it.

Two maintenance rules keep the marks honest.
A flow that re-reads an entry and can check its `?` right now resolves it - the mark becomes the citation (a ledger write, subject to the consumer's candidate flow).
A citation that no longer resolves - the function renamed away, the dashboard gone - downgrades to `? verify:`; it is never silently removed.

One `⊘` recorded about this notation itself: typed gap kinds (code-checkable vs. needs-runtime-data vs. needs-user) are not doing - no consumer dispatches on the distinction, so `verify:` stays free text; reopen when three flows would route differently on it.

## Layout

Principles at the top, then one section per codebase area.

```
# Decisions

## Principles

P-no-new-infra: no new infrastructure for an unproven need
  promoted 2026-08-08 - recurred in D-response-caching, D-job-queue, D-metrics-store

## Auth

risk: high - domains: security, data (updated 2026-08-02, review oauth2-providers)

D-session-length: How long do sessions last? (2026-07-14, login-sessions/spec.md)
  ✓ 30 days sliding - matches user expectations ? verify: no support-ticket data checked ⚠ stolen-token window is long
  ✗ 24h fixed - support burden from daily re-login

D-rate-limit-login: Rate limit the login endpoint? (2026-08-02, security audit)
  ⊘ not doing - no abuse observed (checkLoginAttempts audit log, none flagged); reopen if failed-login volume exceeds 100/day
```

## Area headers

Two dated lines open each area section.
Both are priors, and a prior biases whoever holds it - so consumers read them only at the edges of a run: before dispatch (routing, gating how much autonomy a fix gets) or after findings exist (presentation, escalation).
Never during the walk itself, where they would shape what gets found - a walker who knows "high risk" starts seeing danger everywhere, and one who knows "the user knows this area cold" starts deferring.
Headers change how findings are *treated*, never whether things get *found*.

`risk: <low | moderate | high | critical> - domains: <list> (updated <date>, <source>)`.
Maintained mechanically from commit `Severity:`/`Risk:` trailers by the commit skill and by review runs, and its two halves age differently.
**Domains accumulate** - they're sticky facts about the area (auth touches tokens forever), so new `Risk:` values union in and are never removed mechanically; only a human prunes one, when the area genuinely sheds it.
**The level overwrites** - it's an observation about the most recent trailer-carrying changes (max of their `Severity:` values), not an all-time high-water mark.
Never ratchet: an area whose risky code was removed must be able to come back down, and the date tells consumers how stale the observation is.
Commits without severity trailers leave the level untouched.
A section without a risk line carries no signal - treat it as unassessed, not as safe.

## Promoting principles

A `P-` entry names a rationale that has recurred.
When the same because-clause logic picks or rejects alternatives in a third decision, promote it: kebab slug, one-line statement, the decisions it recurred in.
Never author a principle ahead of recurrence - three citations is the bar.
Once named, cite it by ID (`✗ Redis - P-no-new-infra`); "violates P-no-new-infra" replaces re-deriving the argument in reviews and audits.

## What gets promoted

From a finished spec or audit, copy the entries that pass this test: **would a future spec or audit in this area need to know this was decided?**
Approach choices, `⊘` lines with reopen conditions, and accepted-`⚠` tradeoffs usually pass; change-local trivia (a variable name, an internal function split) doesn't.
Copy entries verbatim with date + source; don't rewrite them.

## Recommender contract

For any flow that emits recommendations.
The order is the point:

1. **The contract starts after judgment.**
   A consumer skill brings the ledger into scope only at its reporting step - findings formed, not yet presented.
   Analysis that reads recorded conclusions before forming its own inherits them instead of testing them; a fresh pass that collides with a recorded decision is signal, not waste.
   When wiring a new consumer, sequence it so the ledger's first mention is its first use - earlier phases never name it, and never carry a "don't read it yet" guard, which only advertises the file where it must stay out of scope.
2. **Reconcile.**
   Grep the ledger sections for the touched areas and classify every finding that collides with an entry:
   - `still-holds` - a `⊘` or accepted `⚠` covers the finding and its reopen condition isn't met.
     Suppress the finding but report the check ("D-rate-limit-login still holds - failed logins ~20/day").
     A silent suppression is indistinguishable from never having checked.
     If the reopen condition can't be verified this run, the report carries the gap instead of implying a check - "still holds `? verify:` current failed-login volume" - a suppression resting on unverified evidence is still a suppression, but says so.
   - `reopened` - the entry's reopen condition now holds.
     Raise it citing the condition and the evidence that tripped it, not as a fresh recommendation.
   - `diverged` - the fresh analysis reached a different conclusion than a recorded `✓` or `⊘`, and no reopen condition explains it.
     Surface the disagreement as its own item: either the old decision missed something or the new analysis lacks its context.
     Never silently suppress, never silently override.

   Reconcile also maintains evidence marks on the entries it touched, per Notation; those are ledger writes and wait for step 4 like any other.

   Findings with no collision pass through unchanged.
   The reconcile report also states whether the pass was `clean` or `contaminated` - contaminated meaning recorded conclusions entered context before findings were formed (an unlucky grep, a file read that pulled them in).
   A suppression from a contaminated pass proves nothing; say which findings were exposed.
3. **Recover.**
   The walk saw decisions nobody recorded - timeouts, retry counts, validation gaps, structural choices, defaults of any kind.
   For each one worth remembering (promotion test), ask what problem it was solving and record a ledger entry with the answer - or `no known problem - unexamined default`, which marks a decision nobody made and invites ratification.
   Recovery rides along with reporting; it is not a separate pass over the code.
4. **Write back.**
   After findings are resolved - by the user in interactive `scope`, or by factory policy in an unattended stage, which records the recommended option as accepted: a declined recommendation becomes a `⊘` line with a concrete reopen condition, dated, sourced to this audit - declined always gets written; that's what makes the next run stateful.
   An accepted one becomes a `✓` entry if it passes the promotion test, or warrants a recommended scope run if it carries enough decisions to need one.
   A recommendation that's real but needs a call the run can't make becomes an `[open]` entry carrying the alternatives - escalations get a durable home instead of dying in a run directory.
