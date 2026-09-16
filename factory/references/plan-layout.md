# Plan Directory Layout

`.dev/{plan-name}/` is the durable home of one change, named by a kebab-case slug for the outcome.
This reference owns the layout, the locating convention, and the diff scope; skills state only their own role.

## Files and owners

| File | Written by | Read by |
| ---- | ---------- | ------- |
| `spec.md` | `scope`; `scope-review` (verified refinements only) | everyone downstream |
| `spec-review_N.md` | `scope-review` (next free index) | `scope` (remediation), re-reviews |
| `implementation-notes.md` | `build` (append-only) | `ship` |
| `review_N.md` | `ship` (next free index) | `scope` (remediation), re-reviews |
| `pr.md` | `ship` (phase 3, overwritten per run) | the PR tool via `--body-file`; re-runs |
| `.dev/config.json` | the user | any skill with Jira behavior |
| `.factory/contract.json` (committed) | `scope`, with the operator | the runner's gates; see [repo-contract.md](repo-contract.md) |
| `.dev/factory-run.json` | the factory runner, before every attempt | every factory skill, first |
| `$FACTORY_RUN_DIR/intent/approved.json` | the factory runner, at the scope handoff (sealed) | unattended stages, read-only |
| `{stage}-result.json` | each factory stage, as its last action | the factory runner |

Each producing skill also renders an HTML companion under the report directory per [reporting.md](reporting.md), named by that skill.
`factory-run.json` and the result files are runtime protocol per [factory-run.md](factory-run.md).
Nothing under `.dev/` is committed in a factory run: the runner excludes the directory from Git, and `factory gc` archives the plan directory before removing the worktree.
One writer per file; every other skill only reads.

## Locating the plan directory

When `.dev/factory-run.json` exists, its `plan` field is authoritative: the plan directory is `.dev/{plan}/`, with no matching and no question.
The heuristics below apply only when the run file is absent.

Match the current branch name to a `.dev/{plan-name}/` slug; fall back to commit messages, then to the only directory in a plausible state for the skill (e.g. the only spec whose change sets aren't done).
Outside a factory run, with more than one plausible match, pick the most recently modified one and say which; never wait.

## Diff scope

Skills that operate on "the change" (`ship`) scope to the local branch against the default branch (`git merge-base HEAD origin/{default}`), diffed with local git only (never `gh pr diff`), excluding lockfiles, build output, minified files, binaries, fonts, and snapshots:

```
':!*lock*' ':!go.sum' ':!dist/' ':!build/' ':!*.min.*' ':!*.map' ':!*.png' ':!*.jpg' ':!*.gif' ':!*.webp' ':!*.woff*' ':!*.ttf' ':!**/__snapshots__/'
```
