# Step 4: Documentation Sync and Ledger Capture

Goal: detect whether code changes have drifted from related documentation, update docs inline, capture durable decisions and contracts while the why is fresh, and keep the architecture overview current.

## 4a: Spec sync

**Skip** if no `docs/*/` feature directories exist with spec files.

Detection - find specs related to changed code using two signals (run in parallel):

1. **`@spec` tags in changed files**: grep the diff output for `@spec(` references. Extract spec names (the part before `:`).
2. **Spec file search**: for each changed code file path, grep all `*.yaml` files in `docs/*/` for that path (basename or relative path).

If related specs are found:

1. Read each related spec file.
2. Compare the diff against the spec content - look for: function signatures or behavior that changed but the spec still describes the old version; new parameters, fields, or flows not reflected in the spec; removed functionality still documented.
3. If the spec is still accurate, move on.
4. If drift is detected, update the spec file directly: minimal, targeted edits to bring it in sync; only change sections that are factually wrong - don't rewrite or reorganize. Stage and commit the spec update with the logical commit it relates to (amend if already committed, otherwise a new commit).

## 4b: Documentation sync

Check whether committed changes require updates to nearby documentation (READMEs, CLAUDE.md, contributing guides, API docs).

Discovery - find documentation that might reference the changed code:

1. **`docs/` directory**: if one exists, grep its contents for mentions of changed function names, class names, CLI commands, file paths, or module names from the diff.
2. **Nearby docs**: for each changed file, check for `README.md` or other `.md` files in the same directory or parent directories up to the repo root.

**Skip** if no documentation files reference any of the changed code.

If references are found, read the relevant sections and compare against the diff - look for: API signatures, function names, or CLI flags that changed but docs still show the old version; new public interfaces, commands, or options not reflected; removed or renamed items still referenced; counts, lists, or examples that are now wrong.

If drift is detected, update the doc directly with minimal, targeted edits and commit with the related change (or as a separate `docs` commit if the doc update doesn't logically belong to any single code commit).

## 4c: Ledger capture

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

## 4d: Architecture sync

The system overview, `docs/architecture.md` ([../../../references/architecture.md](../../../references/architecture.md)), is held to a checker rather than to memory:

```bash
python3 {commit-skill-root}/../../scripts/architecture-check.py docs/architecture.md --touched {every file in this batch's commits}
```

- Exit 2 (no overview): run the reference's initial capture - the full system, not the files just committed - and commit it as `docs(architecture): capture the system overview`.
- Exit 1: fix each violation with a targeted edit - an uncharted file gets its component row (or joins an existing one), a stale path is corrected - and re-run until clean.
- Exit 0: still read the batch's diff for what the checker cannot see: a flow whose steps changed order or hops, a boundary added or dropped (a new external client, store, or queue), a component whose responsibility moved. Edit the affected step, row, or cell only, and refresh the `Updated` line.

Commit overview edits as their own `docs(architecture): <what changed>` commit; never rewrite a section a batch only touched one row of.
