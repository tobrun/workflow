# Reverse Mode: Recover Implicit Decisions

When the user points at existing code instead of a planned change - "audit the decisions in the sync layer", "what did the AI decide here?" - the change-speccing phases invert into an inventory of decisions already made but never argued.

There's no change to understand, so there is no interview.

1. **Catalog in two passes.**
   First pass: inventory every embedded decision without judging any - timeouts, retry counts, limits, page and buffer sizes, validation rules and their gaps, storage and serialization choices, concurrency and ordering assumptions, error-handling policies, defaults of any kind.
   Include values that look fine; judging while cataloging skips them.
   Second pass: for each entry, ask what problem the value was solving.
   Record the answer - or `no known problem - unexamined default`.
   That line is the discriminator: those decisions were never made by anyone and are up for grabs.
2. **Reconcile** against the project's decision ledger, if it keeps one - `docs/decisions.md`, format in [../../../references/decision-ledger.md](../../../references/decision-ledger.md).
   Catalog entries already recorded -> verify the code still matches the recorded choice (a mismatch is `diverged` - raise it).
   The rest are new.
3. **Talk through the new ones worth deciding**, ordered by blast radius if the value is wrong.
   A decision the user ratifies gets `✓` with the real because clause.
   One worth changing gets a recommended normal scope run; never launch it yourself.
4. **Write out.**
   Reverse mode produces no spec file - everything lands in the ledger: ratified decisions and unexamined defaults not worth deciding, recorded with their `no known problem` line if they pass the promotion test so the next audit doesn't re-litigate them.
