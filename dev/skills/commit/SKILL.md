---
name: commit
description: Group pending changes into granular commits with structured what/why bodies and metadata trailers, fix documentation the changes made wrong, capture durable decisions and contracts into the project ledger, and push only when requested. Use when the user asks to commit, or signals implementation work is ready to be committed.
disable-model-invocation: true
---

# Commit

Turn the requested changes into clean, reviewable commits.
Follow [../../references/execution.md](../../references/execution.md): other pending work is excluded unless the user included it, and a request to commit does not authorize a push.

## 1. Read the state

Read `git status` (never `-uall`), `git diff HEAD`, and `git log --oneline -5` for the repository's message style.
With no changes, say so and stop.
Exclude and warn about anything that must not be committed, such as `.env` files, secrets, or large binaries.

## 2. Group

Decide the grouping yourself; never ask the user.
Each commit is one reviewable idea, small enough that its subject line says what changed.
Split within a file when it holds several logical changes (stage the hunks with a partial patch and `git apply --cached`), and across files when they serve different concerns.
Keep together what is inseparable: a fix or feature with its test, a rename with all its call sites.
Config, cleanup, formatting, and documentation are separate commits; order infrastructure first, then core changes, then dependents, then cleanup.

## 3. Commit

Write each message per [references/message-format.md](references/message-format.md): a typed subject, a body that says what changed and why, and the trailers that apply.

```bash
git add <file1> <file2> && git commit -m "$(cat <<'EOF'
type(scope): subject line

What: what changed.

Why: the problem this solves or the goal it serves.

Severity: value
EOF
)"
```

- Name files explicitly; never `git add -A` or `git add .`.
- Never add a `Co-Authored-By` line: the user is the author.
- Never amend without asking. When a pre-commit hook fails, fix the cause, re-stage, and make a new commit, up to three tries.

## 4. Sync documentation and the ledger

Per [references/sync-checks.md](references/sync-checks.md): fix documentation the changes made wrong, and capture decisions and contracts the commits settled.
Skip this when the changes are formatting, comments, or whitespace only.

## 5. Confirm and push

Show `git log --oneline -<N>` for the commits created.
Push only when the user asked for it and nothing restricts it: `git push`, or `git push -u origin <branch>` without an upstream.
Never force-push unless the user explicitly asks; when a push fails, report the error and stop.
