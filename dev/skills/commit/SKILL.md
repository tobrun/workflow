---
name: commit
description: Group pending changes into granular commits with structured what/why bodies and metadata trailers, sync drifted docs, capture durable decisions and contracts into the project ledger, and push by default (say "commit only" to skip the push). Use when the user asks to commit, or signals implementation work is ready to be committed.
disable-model-invocation: true
---

# Commit

You are helping a developer create clean, well-structured git commits.
Analyze all changes, intelligently group them into logical commits, and create them automatically.

**Speed is critical** - minimize tool calls, combine commands, never create a todo list.

## Core principles

- **Understand before committing**: read the actual diff, not just file names.
- **Autonomous grouping**: decide the grouping yourself - never ask the user.
- **Meaningful messages**: commit messages must explain both "what" changed and "why" - without motivation, the commit history becomes useless for understanding decisions.
- **Safety first**: never commit secrets, never amend without asking, never force anything.

## Step 1: Gather state

Run these 3 commands **in parallel in a single message** (no other tool calls):

- `git status` - staged, unstaged, and untracked files (never use `-uall`)
- `git diff HEAD` - all changes (staged + unstaged) in one diff
- `git log --oneline -5` - recent commit message style

If there are no changes at all, inform the user and stop.
Scan for files that should NOT be committed (`.env`, secrets, large binaries). Warn and exclude if found.

## Step 2: Classify and plan

Group changes into logical commits. **Each commit = one reviewable idea.** Split aggressively - even within a single file - so every change is discoverable in `git log`; buried changes are lost changes.
If someone reading `git log --oneline` can't tell what changed from the subject line, the commit is too broad.

Splitting rules (most granular first):

1. **Split within a file** when it contains multiple logical changes - e.g. a new function AND a renamed variable. Use partial staging (Step 3) to commit each change separately.
2. **Split across files** when files serve different concerns - e.g. a config change vs. a code change, or two unrelated modules edited in the same session.
3. **Group together** only when changes are inseparable.

Grouping heuristics: a bug fix or new feature groups with its test; a rename/refactor groups across all affected files (definition and call sites); a new export groups with the file that imports it; config, cleanup, formatting, and documentation are always separate commits.

Ordering: infrastructure/config first -> core changes -> dependent changes -> cleanup last.

Then classify each commit - its type, scope, and metadata trailers - per [references/message-format.md](references/message-format.md).

## Step 3: Execute commits

When all changes in a file belong to the same commit, stage and commit in a **single bash call**:

```bash
git add <file1> <file2> ... && git commit -m "$(cat <<'EOF'
type(scope): subject line

What: brief description of what changed.

Why: the motivation - what problem this solves or what goal it serves.

Severity: value
Refs: #123
EOF
)"
```

The full body (What/Why/Considered/Constraint/Directive/Symptoms) and trailer rules are in [references/message-format.md](references/message-format.md).

When a single file contains changes for multiple commits, use patch-based staging to commit each piece separately:

1. **Extract the target hunks** - `git diff <file>` shows all hunks; identify which belong to this commit.
2. **Create a patch file** with only those hunks (`git diff <file> > /tmp/full.patch`, then edit to keep the relevant hunks, preserving the diff header).
3. **Apply the partial patch to the index**: `git apply --cached /tmp/partial.patch && git commit -m ...`
4. **Repeat** for the remaining hunks in subsequent commits.

Execution rules:

- Never use `git add -A` or `git add .` - always name specific files.
- Never add a `Co-Authored-By` line - the user is the author, you are just writing the message.
- If a pre-commit hook fails: fix the issue, re-stage, create a NEW commit (never amend). Retry up to 3 times.

## Step 4: Documentation sync check

Detect whether the code changes drifted from related documentation, and capture durable decisions and contracts, per [references/sync-checks.md](references/sync-checks.md): 4a spec sync, 4b documentation sync, 4c ledger capture, 4d architecture sync.
Skip this step if the only changes are formatting, comments, or whitespace with no behavioral impact.

## Step 5: Confirm

After the last commit succeeds, run `git log --oneline -<N>` (N = commits created) and show the result.
Only run `git status` if you excluded files earlier.

## Step 6: Push

**Push by default.** The user works across multiple computers and needs commits synced to the remote so they're available everywhere.
Skip the push only if the user explicitly says not to (e.g. "don't push", "commit only", "local only").

1. Run `git push` after all commits succeed.
2. If the branch has no upstream, run `git push -u origin <branch>`.
3. Never use `--force` or `--force-with-lease` unless the user explicitly asks.
4. If the push fails (rejected, conflict, auth), report the error and stop - don't try to resolve it destructively.
