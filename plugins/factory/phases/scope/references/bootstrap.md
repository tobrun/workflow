# Ledger Bootstrap

Build `docs/decisions.md` in one pass from commit history.
The commit skill's message format records `Why:` / `Considered:` / `Constraint:` / `Directive:` bodies and `Severity:` / `Risk:` trailers - in a repo that has used it for a while, the decisions are already written down, scattered across hundreds of commits.
Bootstrap lifts the durable ones into the ledger; after that, commit-time capture (the ledger-capture step of the commit skill's sync-checks reference) keeps it current and this never runs again.

Run for the whole repo or scoped to an area the user names.

## 1. Harvest

Inventory candidate commits - catalog only, no qualifying yet:

```bash
# Commits that argued an alternative - the strongest decision signal
git log --format='%H %s' --grep='Considered:' --all-match

# Commits whose Why: defends a tradeoff (harvested in step 2's read)
git log --format='%H %s' --grep='Why:'

# Trailer-carrying commits, for risk lines
git log --format='%H %s' --grep='Severity:\|Risk:'
```

Group the candidates by area using the commit scope slug (`feat(auth): ...` -> area `Auth`).
Scope slugs are the area vocabulary - don't invent a second taxonomy.

## 2. Extract

For each area, read the candidate commits' full bodies (`git show --format=fuller --no-patch <hash>`) and draft `D-` entries in ledger notation:

- The decision as a question, from `What:` + `Why:` context.
- `✓` the chosen approach, because clause distilled from `Why:`.
- `✗` each rejected alternative from `Considered:`, with its stated reason.
- `⚠` any downside the body admits.
- Dated with the commit date, sourced to the hash.

Qualification bar is the ledger-capture step's: a choice qualifies when someone could plausibly argue it differently later - `Considered:` exists, or the `Why:` defends a tradeoff.
Routine implementation narration doesn't qualify no matter how detailed.

Evidence marks ([../../../references/decision-ledger.md](../../../references/decision-ledger.md) Notation): a claim the body makes from observation - "we saw timeouts in prod" - is a dated claim sourced to the hash; that's a citation, and its staleness is judged at read time.
Reserve `? verify:` for evidence the body asserts but nothing ever demonstrated.
Don't blanket-mark extracted entries - a ledger seeded from history where every clause carries a `?` has buried the mark.

The same read harvests contracts ([../../../references/contracts.md](../../../references/contracts.md)): a body stating a boundary guarantee other code relies on - most often in `Directive:` or `Constraint:` fields ("callers must not retry", "never returns partial results") - drafts a `C-` entry for `docs/contracts.md`.
Before recording, confirm the guarantee against the current code and cite the enforcing function; a guarantee only history asserts gets `? verify:`.
History is a thin source for contracts - the boundaries themselves are the real one, and an audit walk seeds the registry better than commits do; harvest opportunistically here, don't sweep for them.

For a large history, spawn one subagent per area in parallel, each returning drafted entries for its area.
Subagents get the commit bodies as their source - not the ledger, not each other's drafts.

## 3. Reconcile

Each drafted entry gets a typed disposition - report the counts per area:

- `recorded` - enters the ledger.
- `superseded` - a later commit revisited the same decision; the latest resolution is recorded, this entry is folded into it (earlier `✗` alternatives are kept - they're the argument history).
- `stale` - the code the decision governed no longer exists (verify: the touched files or the chosen mechanism are gone). Not recorded; a decision about deleted code is trivia.
- `unqualified` - didn't pass the bar on a closer read.

Dedupe by slug across areas before writing.

## 4. Risk lines

Mechanical, from the trailer harvest, per the area-header semantics in [../../../references/decision-ledger.md](../../../references/decision-ledger.md): domains are the all-time union of `Risk:` values per area (sticky facts); the level is the max `Severity:` across the **recent window only** - last 90 days of trailer commits - because levels observe recent change, they don't ratchet from history.
Date each line `(updated <date>, bootstrap)`.

## 5. Write and present

Assemble `docs/decisions.md` from the ledger layout: principles (only if a rationale already recurs across 3+ extracted decisions - the bar doesn't lower for bootstrap), then area sections with risk lines and entries ordered by date.
Present the per-area summary - entries recorded, superseded, stale, unqualified - and let the user prune before committing the file.
Commit as `docs(decisions): bootstrap ledger from commit history`.
Harvested contracts assemble into `docs/contracts.md` the same way - presented for pruning with the rest, committed separately as `docs(contracts): bootstrap from commit history`.
