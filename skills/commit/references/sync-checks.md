# Documentation Sync and Ledger Capture

## Documentation sync

For each change, look for documentation that now says something false: specs and docs that mention a changed function, command, flag, path, or module (search `docs/` and the `.md` files beside and above each changed file), and any `@spec(` tag in the changed code.
Fix only what is factually wrong, with minimal edits, and commit each fix with the change it follows, or as its own `docs` commit when it belongs to no single change; never amend without authorization.

## Ledger capture

Decisions are cheapest to record at commit time, while the why is fresh - this step is what lets future reviews and specs read `docs/decisions.md` instead of excavating git history.
Format reference: [../../../references/decision-ledger.md](decision-ledger.md).
For each commit just created:

1. **Decisions.**
   If the body's `Why:`/`Considered:` content records a choice that passes the ledger's promotion test, write a `D-` entry to the matching area section of `docs/decisions.md`, in ledger notation: the chosen alternative `✓` with its because clause from `Why:`, rejected alternatives `✗` from `Considered:`, known downsides after `⚠`, dated and sourced to the commit hash.
   Search for an existing slug first - a commit revisiting a recorded decision updates that entry, never duplicates it.
   The bar is high: most commits don't qualify.
   A choice qualifies when someone could plausibly argue it differently later (`Considered:` exists, or the `Why:` defends a tradeoff); routine implementation choices don't.
   Apply evidence marks per the ledger's Notation, sourcing observed facts to the commit hash.
2. **Risk lines.**
   If this batch's commits carry `Severity:`/`Risk:` trailers: union the `Risk:` domains into each touched area's `risk:` header line, and overwrite the level with the batch's max `Severity:` (levels observe recent changes, they don't ratchet), dated, sourced to the commits.
   Trailer-less commits leave risk lines untouched.

If the ledger changed, commit it as its own commit: `docs(decisions): capture <slug(s)> from <scope>`.
