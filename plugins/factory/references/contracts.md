# Contract Registry

`docs/contracts.md` in the consuming project: one repo-tracked file recording facts about behavior at boundaries - what one side guarantees, what the other side relies on, what can never happen.
It exists because reviewers and fixers invent boundary premises when none are recorded ("someone retries upstream", "this endpoint might return null"), and a wrong premise produces a wrong finding.

Contracts are not decisions, and the difference is when consumers may read them.
A decision is a *justification* - it pre-forgives findings, so `docs/decisions.md` enters a run only after judgment (see the recommender contract in [decision-ledger.md](decision-ledger.md)).
A contract is a *premise* - a fact about what the code does at a boundary, the same class of context as a code map's intent and structure - so contracts enter *before* the walk.
That timing difference is why the two live in separate files: a walker retrieving contracts must not pull in recorded conclusions.

## Notation

```
# Contracts

## Payments

C-webhook-retry: payment-svc delivers each webhook once; it never retries - callers own retry
  guaranteed by: deliverWebhook (single attempt, no loop)
  relied on by: billing worker, reconciliation job
  (2026-08-21, webhook-delivery/spec.md)

C-order-status: GET /orders never returns a status outside {pending, paid, failed}
  guaranteed by: OrderStatus enum at serializeOrder ? verify: migration path for legacy rows
  relied on by: mobile order screen
  (2026-08-21, commit a1b2c3d)
```

Each entry: a `C-` kebab slug, one sentence stating the guarantee or invariant, a `guaranteed by:` line naming the enforcing code, a `relied on by:` line naming the sites that assume it, and a date + source.
When a recorded decision created the guarantee, the decision's slug is the source - `(2026-08-21, D-retry-ownership)` - which makes breaking the contract traceable to the choice behind it; a contract with no parent decision cites its spec, audit, or commit as usual.
Evidence marks and their maintenance follow [decision-ledger.md](decision-ledger.md) Notation exactly.

Entries are filed under the area that owns the *guarantee* side - that's where the enforcing code lives, so that's where a change breaks it.
The `relied on by:` line makes the entry findable from the other side; a consumer searching by either module's name retrieves it.
One global file, one entry per contract: splitting per module would force either an arbitrary owner or a duplicate that drifts.

## What qualifies

A fact qualifies when it crosses a boundary and someone could plausibly assume it differently: retry and idempotency ownership, value domains and nullability of responses, delivery and ordering semantics, which side validates, which errors can actually surface.
Facts the type system already states at the boundary don't qualify - the compiler is their registry.
Module-internal behavior doesn't qualify - it has no second side to mislead.

## How consumers use contracts

- **Premise, pre-walk.**
  Reviewers, verifiers, and fixers working near a recorded boundary read the matching entries before forming claims about the other side.
  A claim that contradicts a cited contract needs to explain why the contract is wrong, not assume it away.
- **Refutation strength follows the evidence mark.**
  A contract whose guarantee carries a citation can refute a finding outright.
  A contract carrying `? verify:` can only weaken one - it records that somebody asserted the guarantee, not that anybody checked it.
  Never refute on a `?`.
- **Violations are findings.**
  A change to a guarantee site that breaks the stated guarantee, while reliance sites still assume it, is a first-class finding - the contract names exactly who gets hurt.

## Who writes entries

Contract writes are candidates until the user approves them, like every other durable write in this toolkit.

- **Commit capture and maintenance.**
  The commit skill captures new guarantees a commit establishes and re-checks recorded contracts whose guarantee sites the diff touches, with typed verdicts per the ledger-capture step of its sync-checks reference.
  This check is what keeps citations trustworthy enough to refute anything.
- **Review refutations.**
  When verification refutes a finding by discovering a boundary fact recorded nowhere - the guard, the value domain, the retry owner - that fact becomes a `C-` write candidate, so the next run inherits the premise instead of re-assuming.
- **Spec invariants.**
  The spec's scope section (written by `scope`) records inputs, outputs, and invariants per change; the invariants that cross a boundary promote here at spec promotion time.
- **Audit recovery.**
  An audit that observes an undocumented reliance ("billing assumes exactly-once but nothing guarantees it") records the gap as a `C-` candidate with the `?` on whichever side is unverified.
