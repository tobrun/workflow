# Documentation Sync and Ledger Capture

## Documentation sync

For each change, look for documentation that now says something false: specs and docs that mention a changed function, command, flag, path, or module (search `docs/` and the `.md` files beside and above each changed file), and any `@spec(` tag in the changed code.
Fix only what is factually wrong, with minimal edits, and commit each fix with the change it follows, or as its own `docs` commit when it belongs to no single change; never amend without authorization.

## Ledger capture

Decisions are cheapest to record at commit time, while the why is fresh - this step is what lets future reviews and specs read `docs/decisions.md` instead of excavating git history.
Format references: [../../../references/decision-ledger.md](../../../references/decision-ledger.md), [../../../references/contracts.md](../../../references/contracts.md).
For each commit just created:

1. **Decisions.**
   If the body's `Why:`/`Considered:` content records a choice that passes the ledger's promotion test, write a `D-` entry to the matching area section of `docs/decisions.md`, in ledger notation: the chosen alternative `✓` with its because clause from `Why:`, rejected alternatives `✗` from `Considered:`, known downsides after `⚠`, dated and sourced to the commit hash.
   Grep for an existing slug first - a commit revisiting a recorded decision updates that entry, never duplicates it.
   The bar is high: most commits don't qualify.
   A choice qualifies when someone could plausibly argue it differently later (`Considered:` exists, or the `Why:` defends a tradeoff); routine implementation choices don't.
   Apply evidence marks per the ledger's Notation, sourcing observed facts to the commit hash.
2. **Risk lines.**
   If this batch's commits carry `Severity:`/`Risk:` trailers: union the `Risk:` domains into each touched area's `risk:` header line, and overwrite the level with the batch's max `Severity:` (levels observe recent changes, they don't ratchet), dated, sourced to the commits.
   Trailer-less commits leave risk lines untouched.
3. **Contracts.**
   If the project keeps `docs/contracts.md`, two checks against the diff:
   - *Capture* - a commit that establishes a boundary guarantee another module will rely on (what qualifies is defined in contracts.md) gets a `C-` entry: the guarantee, `guaranteed by:` the enforcing function, `relied on by:` the known dependents, dated and sourced to the hash. Same high bar as decisions: most commits don't qualify.
   - *Maintenance* - check whether any changed file contains a cited guarantee site. Each hit gets a typed verdict: `holds` (the guaranteed behavior is untouched - refresh the citation date), `broken` (the guarantee no longer holds - the commit must restore it or update the contract, and the `relied on by:` sites surface as follow-up work; a broken contract with live reliers is a finding, not a doc edit; a contract whose source is a `D-` slug means the commit is reversing a recorded decision - the ledger entry needs the same update, or the commit is wrong), or `needs-verify` (can't tell from the diff - downgrade the citation to `? verify:` so nothing downstream treats it as checked).

If the ledger or contract registry changed, commit each as its own commit: `docs(decisions): capture <slug(s)> from <scope>` / `docs(contracts): <capture|maintain> <slug(s)> from <scope>`.
