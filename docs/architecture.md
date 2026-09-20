# Architecture

Purpose: this repository is a monorepo of agent skills and the tooling that ships them.
One plugin lives here: `dev`, the hand-invoked development workflow (scope, scope-review, build, ship, commit, and two presentation skills).
A person invokes each skill by hand; skills never invoke each other, and each one recommends the next step instead.
The generated Codex distribution under `plugins/` is built from the source plugin and never edited by hand.

Captured: 2026-09-17 (full, scope) - Updated: 2026-09-20 (the factory plugin and its runner were removed)

## Components

| Component | Responsibility | Lives at | Talks to |
| --------- | -------------- | -------- | -------- |
| dev skills | the interactive workflow skills and the references and scripts they share | `dev/skills/`, `dev/references/`, `dev/scripts/` | nothing at runtime; read by Claude, Codex, opencode, and Pi hosts |
| dev evals | comprehension evals for the skills | `dev/evals/` | nothing at runtime |
| generated plugins | the Codex distribution built from `dev/`, with invocation policy in agents/openai.yaml | `plugins/` | Codex plugin marketplace |
| repo scripts | validation of the whole repository, the plugin generator, and the Pi transport self-test | `scripts/` | every other component |
| harden tools | optional local analyses a ship gauntlet can draw on: added lines, coverage-weighted complexity, flaky reruns, mutation | `tools/harden/` | nothing; run by hand against a target repository |
| research and todo | plans, findings, and reading notes | `research/`, `todo/` | nothing |

## Flows

### A dev skill run
1. A person invokes `/dev:scope`, `/dev:build`, `/dev:ship`, or `/dev:commit` in Claude Code (or the Codex, opencode, or Pi equivalent).
2. The skill reads its SKILL.md and references, writes plan files under .dev/{plan-name}/ in the consuming repository, and recommends the next skill; skills never invoke each other.
3. `dev/scripts/skill-metrics.py` measures each run and appends a row to the consuming repository's .dev/metrics.jsonl.

### Building the Codex distribution
1. `scripts/build_codex_plugin.py` copies skills/, references/, and `scripts/` of the source plugin into plugins/{name}/, strips Claude-only frontmatter, and writes agents/openai.yaml.
2. `scripts/validate.sh` checks structure, links, skill length, the Codex distribution (C01), and the Pi package and transport (P01); `--check` mode of the generator fails when `plugins/` is stale.

## Boundaries

| Boundary | Kind | Owned by | Notes |
| -------- | ---- | -------- | ----- |
| Claude Code, Codex, opencode, Pi | host | the skills | each reads the skills in its own format; the generated tree under `plugins/` serves Codex |
| git and GitHub | external | the ship skill | branch state and `gh pr` calls made during a ship run |
| Jira | external HTTP | dev skills | through `acli`, only when .dev/config.json enables it |
| consuming repository | store | the skills | `.dev/` plan files and `docs/` ledgers |

## Cross-cutting

Testing: `scripts/validate.sh` is the single gate; paid model calls never run in it.
Rules: no em dash anywhere, SKILL.md under roughly 150 lines, every skill `disable-model-invocation: true`.

## Entry points

`scripts/validate.sh`, `scripts/build_codex_plugin.py`, `scripts/test_pi_runner.sh`, `dev/scripts/skill-metrics.py`.
