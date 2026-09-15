# Plan Directory Layout

`.dev/{plan-name}/` is the durable home of one change, named by a kebab-case slug for the outcome.
This reference owns the layout, the locating convention, and the diff scope; skills state only their own role.

## Files and owners

| File | Written by | Read by |
| ---- | ---------- | ------- |
| `spec.md` | `scope`; `scope-review` (verified refinements only) | everyone downstream |
| `spec-review_N.md` | `scope-review` (next free index) | `scope` (remediation), re-reviews |
| `implementation-notes.md` | `build` (append-only) | `ship`, `to-pitch`, `to-quiz` |
| `review_N.md` | `ship` (next free index) | `scope` (remediation), re-reviews |
| `pr.md` | `ship` (phase 3, overwritten per run) | the PR tool via `--body-file`; re-runs |
| `.dev/config.json` | the user | any skill with Jira behavior |

Each producing skill also renders an HTML companion under `/tmp/{project-slug}/reports/` per [reporting.md](reporting.md), named by that skill.
One writer per file; every other skill only reads.

## Locating the plan directory

Match the current branch name to a `.dev/{plan-name}/` slug; fall back to commit messages, then to the only directory in a plausible state for the skill (e.g. the only spec whose change sets aren't done).
Ask which one only if more than one is a plausible match; otherwise proceed without waiting.

## Diff scope

Skills that operate on "the change" (`ship`) scope to the local branch against the default branch (`git merge-base HEAD origin/{default}`), diffed with local git only (never `gh pr diff`), excluding lockfiles, build output, minified files, binaries, fonts, and snapshots:

```
':!*lock*' ':!go.sum' ':!dist/' ':!build/' ':!*.min.*' ':!*.map' ':!*.png' ':!*.jpg' ':!*.gif' ':!*.webp' ':!*.woff*' ':!*.ttf' ':!**/__snapshots__/'
```
