# Concept Map

Each row is one SDLC concept the dev workflow depends on, mapped from what the probe finds to what AGENTS.md says.
The slug column is a closed vocabulary: open items use exactly these slugs, and `check-agents-md.py` rejects any other.
The source column names the file in the workflow repository the lesson was distilled from; it is provenance for maintainers, not something to read during a run.

| Slug | Look for | When present, AGENTS.md says | When absent | Source |
| ---- | -------- | ---------------------------- | ----------- | ------ |
| `setup` | lockfile, package manager, toolchain pins | Setup slot: the exact install command, plus any toolchain pin a fresh checkout needs | ask; if still unknown, `none` and an open item | `dev/skills/scope/SKILL.md` |
| `build` | build script or target, artifact location | Build slot: the command, and where the artifact lands if the e2e layer drives it | `n/a (reason)` for source-only libraries and scripts | `dev/skills/scope/SKILL.md` |
| `check` | lint, typecheck, format, an aggregate target | Check slot: the aggregate target first, else the individual commands | `none` and an open item | `dev/skills/ship/references/tools.md` |
| `unit` | test file patterns, runner config | Unit slot: the command, and in Tests where unit tests live and whether they must run from a package directory | `none` and an open item | `dev/skills/build/references/layers.md` |
| `integration` | separate folder, markers, build tags, source sets, testcontainers, CI `services:` | Integration slot: the command, and in Tests the folder and any service it needs | `none` and an open item: no layer runs owned components together against a real store | `dev/skills/build/references/layers.md` |
| `e2e` | playwright, cypress, detox, maestro, XCUITest, espresso, robot, a harness running the built binary | E2E slot: one command that builds, launches, and drives the artifact | `none` and an open item | `dev/skills/build/references/layers.md` |
| `e2e-env` | stub servers, recorded fixtures, seed scripts, fake clocks, test env files | one Tests line naming how third parties are stubbed, the store is seeded, and the clock is fixed | an open item naming what reaches the real world (a live token, a staging URL, an unseeded store); with no e2e layer at all, only when the built artifact itself calls a real third party | `dev/skills/build/references/mocking.md` |
| `browser` | an installed browser automation tool | the tool, named as a fact of this repo | an open item only when the repo ships a frontend | `dev/skills/build/SKILL.md` |
| `ci` | pull-request workflows and the scripts they call | Merge gate slot: the project-owned commands, then `remote-only: job (reason)` for jobs that need secrets or hosted infrastructure | `none` and an open item: nothing gates pull requests | `dev/references/ci-parity.md` |
| `security` | secret scanning, dependency audit, SAST in hooks or CI | nothing extra; the Check or Merge gate slot already runs it | one open item when there is neither a secret scan nor a dependency audit | `dev/skills/ship/references/gauntlet.md` |
| `architecture` | `docs/architecture.md`, `ARCHITECTURE.md`, a workspace dependency graph | Architecture: a pointer to the overview, or one to three lines of dependency direction that the code does not make obvious | one sentence saying there is no written overview | `dev/references/architecture.md` |
| `decisions` | `docs/decisions.md`, an ADR folder | Architecture: where settled decisions live, and to read them after forming a view, not before | covered by the absence sentence | `dev/references/decision-ledger.md` |
| `contracts` | `docs/contracts.md` | Architecture: where cross-boundary guarantees live, and that a change must keep them or say which it breaks | covered by the absence sentence | `dev/references/contracts.md` |
| `dependencies` | `docs/dependencies.md`, dependency-cruiser, import-linter, ArchUnit config | Architecture: where the allowed module edges are declared and what enforces them | covered by the absence sentence | `dev/references/dependency-rules.md` |
| `commits` | conventional subjects in `git log`, existing trailers | Contributions: the commit format and the real scope list | the format is adopted from now on, and scopes are the areas commits touch (see the probe) | `dev/skills/commit/references/message-format.md` |
| `plans` | whether `.gitignore` covers `.dev/` | Contributions: plan and scratch files live under `.dev/` and are never committed | an open item: `.dev/` is not gitignored | `dev/references/plan-layout.md` |
| `pr` | PR templates, a GitHub remote | Contributions: every PR carries evidence from a real run | always stated | `dev/skills/ship/references/pull-request.md` |
| `branch` | `origin/HEAD`, CI branch filters | intro: "Default branch is `x`." | ask | `dev/references/plan-layout.md` |

## Reading the probe onto a row

A concept is **present** when the probe cites a file or command that proves it, **absent** when the probe looked where the row says and found nothing, and **unresolved** when the evidence conflicts or the only proof would be running something unsafe.
Unresolved rows go to the interview; absent rows take their "When absent" behavior without asking.

Watch for evidence that looks present but is not:
- An e2e suite whose base URL is an https staging or production host, or that needs a real credential, is `e2e` present and `e2e-env` absent.
- Integration tests hiding inside the unit suite (a unit test that opens a database or a socket) are not an integration layer; say so in Tests.
- A workflow that only deploys on push is not a merge gate.
- An e2e harness that drives source or a dev server instead of the built artifact is `e2e` present with an `e2e` open item.
- A configured retry (`retries > 0`, a rerun plugin) contradicts the de-flake rule; it is an open item under its layer, not a fact to copy.
