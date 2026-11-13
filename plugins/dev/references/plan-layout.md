# Plan Directory Layout

`.dev/{plan-name}/` is the durable home of one change, named by a kebab-case slug for the outcome.
This reference owns the layout, the locating convention, and the diff scope; skills state only their own role.

## Files and owners

| File | Written by | Read by |
| ---- | ---------- | ------- |
| `spec.md` | `scope` (from the interview on; on its small path, minimal change sets appended when a spec exists) | everyone downstream |
| `implementation-notes.md` | `build` (append-only) | `ship` |
| `review_N.md` | `ship` (next free index, opened when the panel is selected, or by quick mode) | `scope` (remediation), re-reviews |
| `pr.md` | `ship` (opened at the start of a run that reaches phase 3, overwritten per run); quick mode updates only the review line and Open calls | the PR tool via `--body-file`; re-runs |
| `.dev/config.yaml` | the user (repo-tracked settings) | `ship`, including YOLO (mutation testing) |

Build's e2e report lives at `/tmp/{project-slug}/reports/{plan-name}-e2e.json`, outside the plan directory because its screenshots are large.
One writer per file at a time; every other skill only reads.
A `remote` run writes these same files in the instance's copy of the plan directory; its pull merges them home and never overwrites a file edited here meanwhile.

## Git ignore policy

Track `.dev/config.yaml` as shared repository configuration; ignore all other `.dev/` contents, including plans and results.
Use this block in the repository root `.gitignore`:

```gitignore
/.dev/*
!/.dev/config.yaml
```

When repairing an existing policy, replace every blanket `.dev/` or `/.dev/` entry with this block, remove duplicate blanket entries, and preserve unrelated rules.
An exception alone cannot re-include a file when its parent directory is excluded.
Verify the effective policy with `git check-ignore --no-index -q .dev/config.yaml` (exit 1), and with `.dev/ignore-policy-probe/spec.md`, and `.dev/ignore-policy-probe/results/build-1.json` (each exit 0); these probes need not exist.
Use `--no-index` so an already tracked config cannot hide a broken rule.
If a probe fails, use `git check-ignore --no-index -v` to locate the effective rule, including global excludes and `.git/info/exclude`, and repair repository rules until the checks pass.
Do not untrack previously committed artifacts automatically; report them for cleanup.

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
