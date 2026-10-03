# Plan Directory Layout

`.dev/{plan-name}/` is the durable home of one change, named by a kebab-case slug for the outcome.
This reference owns the layout, the locating convention, and the diff scope; skills state only their own role.

## Files and owners

| File | Written by | Read by |
| ---- | ---------- | ------- |
| `spec.md` | `scope` (from the interview on); `scope-review` (verified refinements only) | everyone downstream |
| `spec-review_N.md` | `scope-review` (next free index, opened before round 1) | `scope` (remediation), re-reviews |
| `implementation-notes.md` | `build` (append-only) | `ship`, `to-pitch`, `to-quiz` |
| `review_N.md` | `ship` (next free index, opened when the panel is selected) | `scope` (remediation), re-reviews |
| `pr.md` | `ship` (opened at the start of a run that reaches phase 3, overwritten per run) | the PR tool via `--body-file`; re-runs |
| `.dev/config.json` | the user | any skill with Jira behavior |

Each producing skill also renders an HTML companion under `/tmp/{project-slug}/reports/` per [reporting.md](reporting.md), named by that skill.
One writer per file; every other skill only reads.

## Written as the run goes

The plan directory is a run's working state, not its closing summary.
A skill creates its file as soon as the run has something true to put in it, and every later fact - an answer, a decision, a finding, a tally, a verdict - reaches the file when it is established, before the run moves on.
A run that dies or compacts mid-stage then loses only what was in flight, the user can open the file at any moment to see where the run stands, and what the file says was recorded when it happened rather than recalled at the end.
Never hold content back to write it once when the stage closes, and never rebuild a file from memory of the conversation.
Each skill names the moments it writes at.

A file its run has not finished says so itself: a report carries `Verdict: IN PROGRESS` until its verdict is set, a spec is unfinished until `lint-spec.py` exits clean, and a `pr.md` until `pr-evidence.py check` passes.
An unfinished file left by a run that is no longer going is a draft, never a result: its own skill picks it up where it stopped, at the same index, and no other skill builds on it.

## Locating the plan directory

Match the current branch name to a `.dev/{plan-name}/` slug; fall back to commit messages, then to the only directory in a plausible state for the skill (e.g. the only spec whose change sets aren't done).
Ask which one only if more than one is a plausible match; otherwise proceed without waiting.

## Diff scope

Skills that operate on "the change" (`ship`) scope to the local branch against the default branch (`git merge-base HEAD origin/{default}`), diffed with local git only (never `gh pr diff`), excluding lockfiles, build output, minified files, binaries, fonts, and snapshots:

```
':!*lock*' ':!go.sum' ':!dist/' ':!build/' ':!*.min.*' ':!*.map' ':!*.png' ':!*.jpg' ':!*.gif' ':!*.webp' ':!*.woff*' ':!*.ttf' ':!**/__snapshots__/'
```
