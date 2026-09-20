# CI Parity and PR Follow-through

The local quality loop is not complete while a required pull-request check is
known red. Spec validation and feature E2E prove the promised change; CI parity
proves the repository's merge gate against the exact checkout being proposed.

## Discover the merge gate

Read the repository's pull-request workflows under `.github/workflows/` and
any scripts they call. Build a list of project-owned commands from required
jobs: tests, lint/type/build, generated-artifact checks, screenshot/report
suites, packaging, and repository validation.

Do not attempt to reproduce GitHub-owned setup actions locally. Reproduce the
project command after performing its documented local setup. Prefer a
repository-provided aggregate target when it covers the same jobs.

Record the commands and outcomes in the plan's `implementation-notes.md`.

## Run before proposing a PR

Run every reproducible project-owned required-check command against the final
checkout, after feature E2E and after any hardening edits.

- A failure is work to fix, including a test described as flaky, unrelated, or
  pre-existing. Diagnose and remove its nondeterminism; do not add retries,
  sleeps, or looser assertions.
- "Pre-existing" is not a waiver. Prove it by running the same command on the
  merge base in an isolated worktree. If the base also fails and fixing it is
  materially outside scope, present the evidence as a human call. Do not call
  the branch PR-ready while the required check remains red.
- A command that cannot run locally because it needs GitHub-only credentials or
  infrastructure is marked `remote-only`, with the reason. It is verified by
  PR follow-through rather than silently skipped.

Only offer or create the PR once all reproducible required checks are green and
all remote-only checks are identified.

## Follow the PR to green

After the user authorizes a push/PR and the PR exists:

1. Watch required checks to a terminal state.
2. For each failure, fetch the failing job log, reproduce its project command
   locally when possible, fix the root cause, run the narrow regression and the
   full CI-parity command, commit, and push.
3. Repeat until every required check is green or a genuine human call is
   reached.

Do not stop at "CI restarted" when the user asked to finish or ship the change.
Do not rerun a failed job unchanged unless the log proves an external service
failure; deterministic failures require a code or test fix.
