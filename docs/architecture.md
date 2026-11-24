# Architecture

Purpose: this repository is a monorepo of agent skills and the tooling that ships them.
One plugin lives here: `dev`, the hand-invoked development workflow (scope, build, ship, yolo, bootstrap, and commit).
A person invokes each `dev` skill by hand; skills never invoke each other, and each one recommends the next step instead. The `dev` `yolo` skill is not an invoker: it reads the scope, build, and ship bodies by path and follows them inline in one session, answering each of their user questions with the stage's own recommendation.
The generated Codex distributions under `plugins/` are built from their source plugin and never edited by hand.

Captured: 2026-09-17 (full, scope) - Updated: 2026-11-24 (five skills: the remote skill removed per D-remove-remote)

## Components

| Component | Responsibility | Lives at | Talks to |
| --------- | -------------- | -------- | -------- |
| dev skills | the interactive workflow skills and the references and scripts they share | `dev/skills/`, `dev/references/`, `dev/scripts/` | nothing at runtime; read by Claude, Codex, opencode, and Pi hosts |
| dev evals | comprehension evals for the skills | `dev/evals/` | nothing at runtime |
| generated plugins | the Codex distributions built from each source plugin, with invocation policy in agents/openai.yaml | `plugins/` | Codex plugin marketplace |
| repo scripts | validation of the whole repository, the plugin generator, and the Pi transport self-test | `scripts/` | every other component |
| harden tools | optional local analyses ship's verify phase can draw on: added lines, coverage-weighted complexity, flaky reruns, mutation | `tools/harden/` | nothing; run by hand against a target repository |
| research and todo | plans, findings, and reading notes | `research/`, `todo/` | nothing |

## Flows

### A dev skill run
1. A person invokes `/dev:scope`, `/dev:build`, `/dev:ship`, or `/dev:commit` in Claude Code (or the Codex, opencode, or Pi equivalent).
2. The skill reads its SKILL.md and references, writes plan files under .dev/{plan-name}/ in the consuming repository, and recommends the next skill; skills never invoke each other.

### Building the Codex distribution
1. `scripts/build_codex_plugin.py` copies skills/, references/, and `scripts/` of the source plugin into plugins/{name}/, strips Claude-only frontmatter, and writes agents/openai.yaml for invocable skills. It removes obsolete distributions identified by their generated README and plugin manifest.
2. `scripts/validate.sh` checks structure, links, skill length, the Codex distribution (C01), the Pi package and transport (P01) and the dev tests (D01); `--check` mode of the generator fails when `plugins/` is stale, including a retired generated distribution.

### An unattended dev run
1. A person invokes `/dev:yolo "a request"` (or `$dev:yolo`).
2. YOLO reads the sibling scope, build, and ship bodies by path and follows them inline, retaining each stage's quality loops.
3. It applies each stage's recommendation when that stage would ask a question and records guesses and deviations in the PR's Open calls.
4. It ends with a pull request, opened as a draft when the run made guesses, or stops on a secret, destructive action, or inaccessible resource.

## Boundaries

| Boundary | Kind | Owned by | Notes |
| -------- | ---- | -------- | ----- |
| Claude Code, Codex, opencode, Pi | host | the skills | each reads the skills in its own format; the generated tree under `plugins/` serves Codex |
| git and GitHub | external | the ship skill | branch state and `gh pr` calls made during a ship run |
| consuming repository | store | the skills | `.dev/` plan files, `docs/` ledgers, shared config |

## Cross-cutting

Testing: `scripts/validate.sh` is the single gate; paid model calls never run in it.
Rules: no em dash anywhere, SKILL.md under roughly 150 lines, every skill `disable-model-invocation: true`.

## Entry points

`scripts/validate.sh`, `scripts/build_codex_plugin.py`, `scripts/test_pi_runner.sh`, `dev/scripts/skill-metrics.py`, `dev/scripts/claims.py`.
