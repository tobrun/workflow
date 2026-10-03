# Probe

Read-only.
Every fact lands in the probe table with the file and line, or the command and its output, that proves it.
A fact you could only get by guessing is unresolved, not a low-confidence guess.
In a workspace or monorepo, probe the root and every member; a large repository gets one read-only subagent per group below, each returning its rows.

## A. Git

- Default branch, first that answers: `git symbolic-ref --short refs/remotes/origin/HEAD`; `gh repo view --json defaultBranchRef` when `gh` is authenticated; the branch filter on the pull-request trigger in CI; the only one of `origin/main`, `origin/master`, `origin/develop` that exists.
- Remotes, and whether this is a fork (an `upstream` remote, an upstream note in the README).
- Scope vocabulary: parse `git log --format=%s -400` with `^(\w+)(\(([^)]+)\))?!?:`, split comma-separated scopes, keep those used at least three times (twice in a history under 100 commits), and map each to the folder its commits touch.
  With no conventional history, take the areas commits actually touch: workspace packages, else the source subfolders that change most, never a lone package name or `src`.
- Whether history already carries `Co-Authored-By` trailers, and whether `.gitignore` covers `.dev/`.

## B. Stack and runners

- JS/TS: the package manager from the lockfile or `packageManager`; `scripts`; workspaces, turbo, or nx; tsconfig; the lint and format config; vitest, jest, playwright, and cypress configs.
- Python: `pyproject.toml` tool sections (pytest markers, ruff, mypy, pyright, coverage), tox, nox, uv, poetry, `conftest.py` fixtures.
- Go: `go.mod`, `//go:build` tags that split test layers, `testdata/`, golangci config.
- Rust: the Cargo workspace, `tests/` integration crates, nextest, clippy config.
- JVM and Android: Gradle source sets (`integrationTest`, `androidTest`), detekt, ktlint, spotless, Maven profiles.
- Swift and Flutter: `Package.swift`, XCUITest targets, swiftlint; `pubspec.yaml`, `integration_test/`.
- Task runners: Makefile and included `.mk` files, justfile, Taskfile, a `scripts/` folder; prefer one aggregate target that covers lint, types, and tests when it exists.
- Toolchain pins (`.tool-versions`, `.nvmrc`, `.python-version`, mise, devcontainer, nix) and pre-commit, husky, or lefthook hooks.

## C. Merge gate

- Read every workflow whose trigger includes pull requests, collect each job's `run:` steps, and follow them into the Makefile targets and scripts they call.
- A job that reads `secrets.*` or needs hosted infrastructure is `remote-only`; record the reason.
- Required checks come from branch protection when `gh api` can read it; otherwise treat every pull-request job as required and say so.
- Read GitLab, CircleCI, Jenkins, Azure, Bitbucket, and Buildkite config the same way.
- A workflow that only deploys on push is not a gate.

## D. Test layers

- Unit: file patterns, where they live, the command, and whether they must run from a package directory rather than the root.
- Integration: a separate folder, a marker, a build tag, a source set, testcontainers, a compose file for tests, CI `services:`, or a `test:integration` script.
  With none of those, grep the unit suite for database or network use and note integration tests hiding there.
- E2E: the tools in the concept map, an `e2e/` folder, a `test:e2e` script, or a harness that runs the built binary as a subprocess.
  Find its launch command (playwright `webServer.command`, a start or preview script, a compose file, a run skill under `.claude/skills`) and whether it drives the built artifact or a dev server or source.
  A harness that drives source or a dev server is still the e2e layer; the gap becomes an `e2e` open item, not a question.
- E2E environment: stub servers (msw, nock, wiremock), recorded fixtures (VCR, polly), localstack, seed scripts, test env files, fake timers.
  Flag a base URL on an https staging or production host, and any real credential the run needs.
- Browser automation: whichever tool is installed, and whether the repo ships a frontend at all.
- Retries: `retries > 0`, jest retries, a rerun plugin; configured retries become an open item under the layer they belong to, since this skill never edits config.

## E. Quality tools

A tool counts only when a hook, script, or CI step runs it; one that is merely available (`npm audit`) does not.
Map each configured tool onto lint and typecheck, secret scanning, dependency audit, SAST, dead code, duplication, dependency rules, coverage and complexity, flakiness, and mutation.
Only a security gap, neither a secret scan nor a dependency audit, becomes an open item; the other categories are the ship gauntlet's concern, not this file's.

## F. Durable docs and merge input

- `docs/architecture.md`, `docs/decisions.md`, `docs/contracts.md`, `docs/dependencies.md`, an ADR folder, `ARCHITECTURE.md`, `CONTRIBUTING.md`.
- Dependency direction between workspace packages, only when it is clear from the manifests and not obvious from the names.
- The existing root AGENTS.md and CLAUDE.md are read in SKILL.md step 2; test each behavioral claim they make (a gitignore rule, a generator's output, a CI step) against the code, not only their paths and commands.
  Nested agent files, `.cursorrules`, and `.github/copilot-instructions.md` are evidence only; `CLAUDE.local.md` is private and never read.
