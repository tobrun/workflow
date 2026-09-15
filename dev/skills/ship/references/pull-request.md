# The Pull Request

How phase 3 of `ship` turns a hardened, reviewed branch into an open pull request that carries its own proof.
A PR without evidence asks the reviewer to trust the description; this phase makes the change visibly work before anyone reads the diff.

## When it runs

Phase 3 runs in the default flow, after the review report is written and the remediation loop in [remediation.md](remediation.md) has run its course: PASS and CONCERNS open a PR ready for review, a real blocker - one that survived both remediation rounds - opens a draft PR with the blockers listed first, so the work is preserved and CI runs while the human call is made.
It is skipped in "gauntlet only" and "review only" runs and when the user says "no PR" or "local only"; a review-only run on someone else's PR never pushes anything.
Never force-push, never rebase, and never touch a branch other than the work branch and the evidence branch.

## Commit and push

1. Fixes the gauntlet left uncommitted are committed now, one commit per tool, in `commit`'s `type(scope): subject` plus What/Why shape.
   Name files explicitly; never `git add -A`, never a `Co-Authored-By` line.
2. On the default branch, create the work branch first: `{plan-name}`, or `{EPIC-KEY}-{plan-name}` when [../../../references/jira.md](../../../references/jira.md) is enabled.
3. `git push -u {remote} {branch}`; a rejected push is reported and stops the phase.

## Evidence

The Evidence section is the PR's proof of the feature or fix, captured from a real run, never composed from memory.
`scripts/pr-evidence.py check` is the gate: loop on its output until it passes before creating or updating the PR.

- **An e2e report exists** (`/tmp/{project-slug}/reports/{plan-name}-e2e-report.html`, refreshed by phase 1 when fixes landed): run `pr-evidence.py extract` on it.
  Frontend reports yield one screenshot per meaningful step; `pr-evidence.py publish` pushes the PNGs to the `pr-evidence` branch so they render inline, because data URIs do not render on a PR.
  Non-frontend reports yield labeled Before/After state per step, plus captured output.
- **No e2e report** (no spec, no e2e suite): capture the evidence in this phase, the same way build would.
  A system with a UI: launch it in the mocked environment via the `run` skill or the repo's documented command, drive the changed behavior with browser automation, save one screenshot per meaningful step under the evidence directory, then publish them.
  Anything else: the observable effect before and after, as a labeled pair of fenced blocks - a CLI transcript, an API response, a table row, a rendered file.
- **A bug fix** always adds the reproducing test as a labeled pair: `**On merge base**` shows it failing in an isolated worktree at `git merge-base HEAD origin/{default}`, `**On this branch**` shows the same test passing.
  Red on base is what proves the bug was real; green on the branch is what proves it is gone.
- A screenshot proves the UI; when the real effect is a data change the screen does not show, add the Before/After pair for it as well.
- Never fabricate, reuse a screenshot from another run, or paste a data URI; a scenario that failed stays marked failed in the evidence.

Evidence files live under `/tmp/{project-slug}/pr-evidence/`; the `pr-evidence` branch is an orphan branch that only ever receives images, one commit per run.
On a non-GitHub remote pass `--url-template` with wherever the images are hosted.

## Body

Write the body to `.dev/{plan-name}/pr.md` and hand it to the PR tool with `--body-file`; the file stays as the record of what was proposed.

```markdown
## Summary

{What changed and why, 2-4 sentences from the spec's research and scope sections.}
Plan `.dev/{plan-name}` | Review {N}: {verdict} | Jira {EPIC-KEY when enabled}

## Evidence

{pr-evidence.py extract output, or the hand-captured section per the rules above}

## Quality

| Check | Found | Fixed | Surviving |
| ----- | ----- | ----- | --------- |
{one row per gauntlet tool}

Review panel: {lenses}; {blockers} blockers, {concerns} concerns ({review_N.md path}).
CI parity: {each reproducible required command and its result}; remote-only: {checks verified by the PR itself, or "none"}.

## Open calls

{Each surviving human call from the gauntlet, each real blocker with its two-round history, and each concern from the review, one line each, or "none".}
```

Title: `{EPIC-KEY} ` prefix when Jira is enabled, then the change in imperative mood, under 70 characters.

## Create or update

- No PR for the branch: `gh pr create --title ... --body-file .dev/{plan-name}/pr.md` (`--draft` on BLOCK), base = the default branch.
- A PR already exists (build opened it on request, or a re-review): `gh pr edit --body-file ...`; `gh pr ready` when a BLOCK verdict has cleared, never the reverse.
- No `gh` and no equivalent host CLI: push, keep `pr.md`, print the compare URL, and say the PR must be opened by hand.

Then follow the PR's required checks to a terminal state per [../../../references/ci-parity.md](../../../references/ci-parity.md).
Opening the PR is not the end of the phase; every required check green, or a genuine human call, is.
