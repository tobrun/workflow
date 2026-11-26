# The Pull Request

How phase 3 of `ship` turns a hardened, reviewed branch into an open pull request that carries its own proof.
A PR without evidence asks the reviewer to trust the description; this phase makes the change visibly work before anyone reads the diff.

## When it runs

Phase 3 runs in the default flow, after the review report is written and the remediation loop in [remediation.md](remediation.md) has run its course: PASS and CONCERNS open a PR ready for review - a draft until its required checks are green when a check was deferred to CI, per [../../../references/ci-parity.md](../../../references/ci-parity.md) - a real blocker - one that survived both remediation rounds - opens a draft PR with the blockers listed first, so the work is preserved and CI runs while the human call is made.
It is skipped in "verify only" and "review only" runs and when the user says "no PR" or "local only"; a review-only run on someone else's PR never pushes anything.
Never force-push, never rebase, and never touch a branch other than the work branch and the evidence branch.

Follow [../../../references/execution.md](../../../references/execution.md).
A missing remote or unapproved hosting action leaves a prepared body and local evidence; do not publish or create a branch to bypass a restriction.
An INCOMPLETE review stays local or draft and never becomes ready.

## Commit and push

1. Fixes the verify phase left uncommitted are committed now, one commit per kind, in `commit`'s `type(scope): subject` plus What/Why shape.
   Name files explicitly; never `git add -A`, never a `Co-Authored-By` line.
2. Use the already authorized work branch and PR base; create a branch only when permitted. An explicitly stacked base stays stacked.
3. `git push -u {remote} {branch}`; a rejected push is reported and stops the phase.

## Evidence

The Evidence section is the PR's proof of the feature or fix, captured from a real run, never composed from memory.
`scripts/pr-evidence.py check` is the gate: loop on its output until it passes before creating or updating the PR.

- **Show what this PR changed, nothing else.**
  Read the branch diff and pick the scenarios (or, with no e2e report, the screens and states) that exercise the behavior it adds or alters: the spec's `[e2e]` scenarios for this plan's change sets, a bug fix's reproduction.
  Leave out regression, setup, login, and unrelated scenarios and any step that looks the same before and after the change; a reviewer should see the diff's effect in a few images, not a tour of the app.
  Keep a failing scenario in only when it is one of the picked ones.
- **An e2e report exists** (`/tmp/{project-slug}/reports/{plan-name}-e2e.json`, written by phase 1's e2e pass and refreshed when fixes landed after it): run `pr-evidence.py extract` on it with one `--scenario {id}` per picked scenario.
  Frontend reports yield one screenshot per meaningful step; `pr-evidence.py publish` pushes the PNGs to the `pr-evidence` branch so they render inline, because data URIs do not render on a PR.
  Non-frontend reports yield labeled Before/After state per step, plus captured output.
- **No e2e report** (no spec, no e2e suite): capture the evidence in this phase, the same way the e2e pass would.
  A system with a UI: launch it in the mocked environment via the `run` skill or the repo's documented command, drive the changed behavior with browser automation, save one screenshot per step that shows the change under the evidence directory, then publish them.
  Anything else: the observable effect before and after, as a labeled pair of fenced blocks - a CLI transcript, an API response, a table row, a rendered file.
- **A bug fix** adds the same reproducing test on a known-bad and corrected revision.
  For a pre-existing defect, use `**On merge base**` and `**On this branch**`, naming the actual SHAs.
  For a defect introduced within this feature branch, use `**On known-bad revision**` and `**On corrected revision**`, naming the feature commits and why the merge base cannot demonstrate it.
  A missing API, import failure, or absent dependency does not reproduce the reported behavior; the red must be the defect's triggering assertion.
- A screenshot proves the UI; when the real effect is a data change the screen does not show, add the Before/After pair for it as well.
- Never fabricate, reuse a screenshot from another run, or paste a data URI; a scenario that failed stays marked failed in the evidence.
- Keep capture provenance: revision, command, time, and SHA-256 of the test and implementation sources covered. Extraction copies the E2E report's `provenance` into a `<!-- dev-evidence: {...} -->` marker. For hand-captured evidence, write that same marker from the actual run.
  Run `pr-evidence.py check --require-current --repo-root {repo-root}` before publishing or updating. Source changes invalidate that evidence; refresh affected scenarios, including on quick re-ship. The format/hash gate does not judge assertion quality or authenticate a remote image.

Evidence files live under `/tmp/{project-slug}/pr-evidence/`; the `pr-evidence` branch is an orphan branch that only ever receives images, one commit per run.
On a non-GitHub remote pass `--url-template` with wherever the images are hosted.

## Body

The body lives at `.dev/{plan-name}/pr.md` and is handed to the PR tool with `--body-file`; the file stays as the record of what was proposed.
A run that will reach this phase opens the file in the shape below before its first check and fills it as the run goes, per [../../../references/plan-layout.md](../../../references/plan-layout.md): the Summary at once, a Quality row when its check finishes, the CI parity line as each command returns, the review line when a verdict is set, an Open call when it is raised and each remediation round's attempt when the round ends.
Only the Evidence section is left for this phase; a part the run has not reached is absent from the file, never a placeholder or a guess.

Name decisions and verification kinds by their titles anywhere outside the Quality table, per [../../../references/decision-ledger.md](../../../references/decision-ledger.md).
Everything the agent writes is a claim. What proves it is the DEV-EVIDENCE block at the end, which only CI writes: `ci-evidence.py` (vendored by `bootstrap` as `.verify/ci-evidence.py`) runs at the PR's head commit and fills it with the test totals from CI's own JUnit, a test-diff guard, a fails-on-revert check of the new tests against the base, changed-line coverage, each acceptance criterion's status, and where the claims above disagree with any of it.
The body leaves the markers empty; `pr-evidence.py check` refuses anything between them.

```markdown
## Summary

{What changed and why, at most five lines, from the spec's research and scope sections.}
Plan `.dev/{plan-name}` | Review {N}: {verdict} (advisory)

## Acceptance criteria

| Scenario | Criterion | Test | Status |
| --- | --- | --- | --- |
{one row per spec scenario: its positional id (CS1-S1), the scenario text, the runner case from test-evidence.json as `classname::name`, and Status `awaiting CI` - CI writes the real status}

## Evidence

{pr-evidence.py extract output, or the hand-captured section per the rules above}

## Not verified

{What this PR does not prove, one line each: every planned kind that could not run and why, every fix-and-report kind left failing, every test the diff edited or deleted with the reason, and any environment the tests cannot reach. "Nothing" needs its reason.}

## Verification

{pr-evidence.py summarize output: the twenty kinds grouped, each ran clean, ran with open calls, or skipped}

<details><summary>All twenty kinds as the agent reported them</summary>

## Quality

| Kind | Result | Found | Fixed | Surviving |
| ---- | ------ | ----- | ----- | --------- |
{one row per kind in verification.md, all twenty, first cell the kind's slug (`unit`, `fuzz`, `ui-ux`, ...): Result is `ran` with the counts, or `skipped ({reason})` with the counts left empty, such as `skipped (no parser or untrusted input)` or `skipped (disabled in .dev/config.yaml)`; rows for further checks (security, dead code, duplication, complexity, flakiness) may follow}

Proofs: {statements proved of total, the pinned toolchain, and the gate's result, with each unfinished statement named; omit when the spec names no proof}.

Mutation testing: {selected targets, command, runtime, killed/survived/uncovered counts, regression tests added or strengthened, and reasons for remaining survivors; omit when skipped}.

Review panel: {lenses}; {blockers} blockers, {concerns} concerns ({review_N.md path}).
CI parity ({impact verdict and packages}): {each reproducible required command, the scope it ran at, and its result}; deferred to CI: {commands outside the impact, or "none"}; remote-only: {checks verified by the PR itself, or "none"}.

</details>

## Open calls

{Each surviving human or open call from the verify phase with its three-round history, each real blocker with its two-round history, and each concern from the review, one line each, or "none".}

## Provenance

Written by {agent and model} via {host}{, session: link when the host gives one}. The sections above are the agent's account; the block below is CI's.

<!-- BEGIN DEV-EVIDENCE -->
<!-- END DEV-EVIDENCE -->
```

Title: the change in imperative mood, under 70 characters.

## Create or update

Before creating or updating, run `pr-evidence.py summarize .dev/{plan-name}/pr.md`, then loop `pr-evidence.py check --require-current --kind {frontend|non-frontend} --spec .dev/{plan-name}/spec.md` until it passes; `--kind` is the e2e report's `kind`, and a frontend kind requires an http(s) image, a non-frontend kind a labeled pair.
When the repository has `.verify/ci-evidence.py`, run its `collect` locally against the base as a preflight (`--out /tmp/{project-slug}/ci-evidence`) and fix what it finds before pushing; its local block never goes into the body.

- No PR for the branch: `gh pr create --title ... --body-file .dev/{plan-name}/pr.md` (`--draft` on BLOCK or a deferred check), base = the authorized intended base, defaulting to the default branch only when none was specified.
- A PR already exists (build opened it on request, or a re-review): copy `pr.md` to a scratch file, run `ci-evidence.py splice {scratch} --from {the current body from gh pr view}` so CI's block survives, then `gh pr edit --body-file {scratch}`; `gh pr ready` once no BLOCK verdict stands and its required checks are green, never the reverse.
- No `gh` and no equivalent host CLI: push, keep `pr.md`, print the compare URL, and say the PR must be opened by hand.

Then follow the PR's required checks to a terminal state per [../../../references/ci-parity.md](../../../references/ci-parity.md), and read the DEV-EVIDENCE block CI wrote for the head commit.
A failing test, a guard finding, a new test that passes on the base, coverage under the line, or a claims mismatch there is a verify-phase violation: fix it and push, and CI re-measures, up to three rounds; what remains goes under Open calls and Not verified, and the block stays as CI wrote it.
Without an evidence job the markers stay empty: say so under Not verified and recommend `bootstrap`.
Opening the PR is not the end of the phase; every required check green, or a genuine human call, is.
