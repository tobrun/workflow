# CI Parity and PR Follow-through

The local quality loop is not complete while a required pull-request check is
known red. Spec validation and feature E2E prove the promised change; CI parity
proves the repository's merge gate against the exact checkout being proposed.
Locally that proof covers what the change can reach; the full merge gate is the
pull request's own CI, which the run follows to green.

## Discover the merge gate

Read the repository's pull-request workflows under `.github/workflows/` and
any scripts they call. Build a list of project-owned commands from required
jobs: tests, lint/type/build, generated-artifact checks, screenshot/report
suites, packaging, and repository validation.

Do not attempt to reproduce GitHub-owned setup actions locally. Reproduce the
project command after performing its documented local setup. Prefer a
repository-provided aggregate target when it covers the same jobs.

Record each command and its outcome as it finishes, not after the whole set:
`build` in the plan's `implementation-notes.md`, `ship` on the CI parity line
of the `pr.md` its run keeps.

## Scope every local run to the impact

A full gate re-proves every package the change never touched, and on a grown
repository it is most of a run's wall clock. CI runs it anyway, on every push.
So every local gate - a wave gate, a gauntlet re-run, the parity run below -
first asks what the change reaches:

```bash
python3 {skill-root}/../../scripts/impact-scope.py [--base REF]
```

`--base` defaults to the merge base with the default branch; pass the last
commit (`--base HEAD`) to scope a run to uncommitted fixes alone. Loop against
its verdict rather than judging scope by feel:

- `docs` - no test, build, or lint run is owed; record that and move on.
- `full` - a lockfile, root config, toolchain pin, or CI workflow changed, so
  every package is reached: run each command in full; its reasons name why.
- `impacted` - run each command at the listed packages' scope, through the
  repository's own affected-graph tooling when it has one (`nx affected`,
  `turbo run --filter=...[{base}]`, bazel or pants target queries,
  `go test` over the changed packages and `go list` dependents,
  `cargo test -p`, `jest --findRelatedTests`, `vitest related`, pytest over the
  package's test paths). Without one, add the workspace packages that declare a
  listed package as a dependency. Lint and format run over the changed files;
  typecheck and build over the impacted packages.

A command with no scoped form runs in full when its inputs intersect the
impact (a screenshot suite when UI code changed, packaging when a shipped
package changed) and is otherwise recorded `deferred to CI: outside impact`.
A command the run would otherwise skip as slow is never deferred while its
inputs are impacted: impact, not cost, decides.

Run the full set locally instead when no pull request will carry the change
(a run told to stay local, or a branch without a remote), or when the user asks
for full gates: with no CI to defer to, the local run is the merge gate.

## Run before proposing a PR

Run every reproducible project-owned required-check command at the scope
above against the final checkout, after feature E2E and after any hardening
edits.
A command that already ran green on this exact tree - no file changed since -
is not run a second time: record its result and where it came from.

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

Only offer or create the PR once every command run at the impacted scope is
green and every deferred and remote-only check is identified. A PR that carries
a deferred or remote-only check opens as a draft and is marked ready only when
its required checks are green, so no reviewer is asked to read a change the
merge gate has not yet passed.

## Follow the PR to green

After the user authorizes a push/PR and the PR exists:

1. Watch required checks to a terminal state.
2. For each failure, fetch the failing job log, reproduce its project command
   locally when possible, fix the root cause, run the narrow regression and
   that failing command at the fix's impact, commit, and push.
3. Repeat until every required check is green or a genuine human call is
   reached, then mark the PR ready when nothing else holds it as a draft.

Do not stop at "CI restarted" when the user asked to finish or ship the change.
Do not rerun a failed job unchanged unless the log proves an external service
failure; deterministic failures require a code or test fix.
