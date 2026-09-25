# Architecture

Purpose: this repository is a monorepo of agent skills and the tooling that ships them.
Three plugins live here: `dev`, the hand-invoked development workflow (scope, scope-review, build, ship, commit, and two presentation skills); `factory`, whose `run` skill orchestrates copies of the same four phases unattended; and `bootstrap`, whose `agents-md` skill writes a consuming repository's root AGENTS.md from the workflow's lessons.
A person invokes each `dev` skill by hand; skills never invoke each other, and each one recommends the next step instead - except the factory `run` skill, the one sanctioned invoker, which launches its copied phase skills by path and judges their completion itself.
The generated Codex distributions under `plugins/` are built from their source plugin and never edited by hand.

Captured: 2026-09-17 (full, scope) - Updated: 2026-09-25 (the bootstrap plugin writes a consuming repository's AGENTS.md)

## Components

| Component | Responsibility | Lives at | Talks to |
| --------- | -------------- | -------- | -------- |
| dev skills | the interactive workflow skills and the references and scripts they share | `dev/skills/`, `dev/references/`, `dev/scripts/` | nothing at runtime; read by Claude, Codex, opencode, and Pi hosts |
| dev evals | comprehension evals for the skills | `dev/evals/` | nothing at runtime |
| generated plugins | the Codex distributions built from each source plugin, with invocation policy in agents/openai.yaml | `plugins/` | Codex plugin marketplace |
| repo scripts | validation of the whole repository, the plugin generator, and the Pi transport self-test | `scripts/` | every other component |
| harden tools | optional local analyses a ship gauntlet can draw on: added lines, coverage-weighted complexity, flaky reruns, mutation | `tools/harden/` | nothing; run by hand against a target repository |
| research and todo | plans, findings, and reading notes | `research/`, `todo/` | nothing |
| bootstrap plugin | the `agents-md` skill, its references distilled from the dev skills, `bootstrap/skills/agents-md/scripts/check-agents-md.py` that every draft loops against, and the checker's tests | `bootstrap/skills/`, `bootstrap/evals/` | the consuming repository's root AGENTS.md; reads its manifests, CI config, git history, and CLAUDE.md |
| factory plugin | the `run` orchestrator skill (its only invocable skill), the four built-in phase bodies it reads by path, `factory/scripts/factory-config.py` for the pipeline config, their references and scripts | `factory/skills/`, `factory/phases/`, `factory/references/`, `factory/scripts/`, `factory/evals/` | the host's subagent tool, the consuming repository's .dev/ and .factory/ |

## Flows

### A dev skill run
1. A person invokes `/dev:scope`, `/dev:build`, `/dev:ship`, or `/dev:commit` in Claude Code (or the Codex, opencode, or Pi equivalent).
2. The skill reads its SKILL.md and references, writes plan files under .dev/{plan-name}/ in the consuming repository, and recommends the next skill; skills never invoke each other.
3. `dev/scripts/skill-metrics.py` measures each run and appends a row to the consuming repository's .dev/metrics.jsonl.

### Building the Codex distribution
1. `scripts/build_codex_plugin.py` copies skills/, references/, and `scripts/` of the source plugin (and factory/phases/ for the factory) into plugins/{name}/, strips Claude-only frontmatter, and writes agents/openai.yaml for invocable skills.
2. `scripts/validate.sh` checks structure, links, skill length, the Codex distribution (C01), the Pi package and transport (P01), the factory plugin's unattended wording and result protocol, and the bootstrap checker's tests (B01); `--check` mode of the generator fails when `plugins/` is stale.

### Bootstrapping a repository
1. A person invokes `/bootstrap:agents-md` (or `$bootstrap:agents-md`) in a consuming repository.
2. The skill probes the stack read-only, maps it onto its concept map, interviews only for what the probe cannot settle, and runs each command it will list.
3. It drafts outside the repository, loops `bootstrap/skills/agents-md/scripts/check-agents-md.py` until it exits 0, shows the diff for approval, then writes the root AGENTS.md; it never commits and never writes CLAUDE.md.

### A factory run
1. A person invokes `/factory:run "a request"` (or a path to one); preflight runs factory-config.py check, which classes each phase as built-in, injected or foreign and validates the pipeline from the consuming repository's .factory/config.yaml (the built-in default when there is none), and stops before the interview on any finding.
2. `run` runs the contiguous interactive prefix inline with them (`scope` in the default) and ends it with one go question.
3. From the go, `run` launches each remaining phase in turn as a fresh-context subagent, judges each result's file against its type's declared checks and outcome contract, repairs or relaunches on failure, and never lets a phase launch another.
4. The run ends in an open pull request, or a report naming the exact action a person could take - the pushed branch and the run's state file are what survives a closed session.

## Boundaries

| Boundary | Kind | Owned by | Notes |
| -------- | ---- | -------- | ----- |
| Claude Code, Codex, opencode, Pi | host | the skills | each reads the skills in its own format; the generated tree under `plugins/` serves Codex |
| git and GitHub | external | the ship skill | branch state and `gh pr` calls made during a ship run |
| Jira | external HTTP | dev skills | through `acli`, only when .dev/config.json enables it |
| consuming repository | store | the skills | `.dev/` plan files, `docs/` ledgers, the .factory/ config and injected phase copies, and the root AGENTS.md bootstrap writes |

## Cross-cutting

Testing: `scripts/validate.sh` is the single gate; paid model calls never run in it.
Rules: no em dash anywhere, SKILL.md under roughly 150 lines, every skill `disable-model-invocation: true`.

## Entry points

`scripts/validate.sh`, `scripts/build_codex_plugin.py`, `scripts/test_pi_runner.sh`, `dev/scripts/skill-metrics.py`, `bootstrap/skills/agents-md/scripts/check-agents-md.py`.
