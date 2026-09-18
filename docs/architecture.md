# Architecture

Purpose: this repository is a monorepo of agent skills and the tooling that ships them.
Two plugins live here: `dev`, the hand-invoked development workflow (scope, build, ship, commit, and two presentation skills), and `factory`, factory copies of four `dev` skills plus a local Python runner that chains them into an unattended pipeline from one scoped change request to a ready pull request.
A change request enters through `factory new`, is scoped interactively in Claude, then scope-review, build, and ship run as fresh Codex processes chained through artifacts on disk, with a long-lived foreman session deciding after every attempt what the runner does next.
Generated Codex distributions under `plugins/` are built from the two source plugins and never edited by hand.

Captured: 2026-09-17 (full, scope) - Updated: 2026-09-18 (the dream loop: history worlds, hindsight labels, dreams)

## Components

| Component | Responsibility | Lives at | Talks to |
| --------- | -------------- | -------- | -------- |
| dev skills | the interactive workflow skills and the references and scripts they share | `dev/skills/`, `dev/references/`, `dev/scripts/` | nothing at runtime; read by Claude, Codex, and Pi hosts |
| factory skills | unattended copies of scope, scope-review, build, and ship, the foreman decision skill, and the runner-only hindsight labeller and dream policy developer | `factory/skills/`, `factory/references/`, `factory/scripts/` | factory runner (through .dev/factory-run.json and {stage}-result.json; hindsight and dream through their output records) |
| factory runner | the stdlib-only Python CLI and detached worker: run directories, worktrees, stage launches, gates, foreman turns, caps, exports, dashboard | `factory/runner/` | Codex and Claude hosts, git, GitHub through `gh`, Chrome, the runtime root |
| history | finished runs turned into replay worlds with rewritten paths, hindsight labels and the fault feed, human labels, and the shared history lock | `factory/runner/history.py` | run directories (read only), the Codex host (the hindsight skill), the runtime root |
| dream | replays the foreman skill over labelled worlds, scores it on the action ladder, splits selection from confirmation, checks and stages candidates, and deploys a winner | `factory/runner/dream.py` | history, the Codex host (replays and the dream skill), git and GitHub (the deploy), the runtime root |
| factory evals | comprehension evals, the offline behavioral benchmark with stub hosts, and the paid foreman eval on captured cases | `factory/evals/` | factory runner modules (imported by path) |
| generated plugins | the Codex distributions built from `dev/` and `factory/`, with invocation policy in agents/openai.yaml | `plugins/` | Codex plugin marketplace |
| repo scripts | validation of the whole repository, the plugin generator, and the offline end-to-end runner test | `scripts/` | every other component |
| research and todo | plans, findings, and reading notes that informed the factory | `research/`, `todo/` | nothing |

## Flows

### A dev skill run
1. A person invokes `/dev:scope`, `/dev:build`, `/dev:ship`, or `/dev:commit` in Claude Code (or the Codex or Pi equivalent).
2. The skill (dev skills) reads its SKILL.md and references, writes plan files under .dev/{plan-name}/ in the consuming repository, and recommends the next skill; skills never invoke each other.
3. `dev/scripts/skill-metrics.py` measures each run and appends a row to the consuming repository's .dev/metrics.jsonl.

### A factory run
1. `factory new` (factory runner, `factory/runner/cli.py`) creates ~/.factory/runs/{id}/, a git worktree on factory/{plan}, and launches interactive scope in Claude.
2. At handoff the runner seals the intent (`factory/runner/intent.py`) and starts a detached worker (`factory/runner/worker.py`) that holds worker.lock and is the only writer of run.json.
3. For each headless stage the worker writes .dev/factory-run.json (`factory/runner/pipeline.py`), launches a fresh Codex process (`factory/runner/executor.py`, `factory/runner/hosts.py`), and judges the result with a deterministic gate (`factory/runner/gates.py`) that re-runs tests, e2e, validation, and gauntlet commands and reads {stage}-result.json.
4. After every attempt the worker asks the foreman (`factory/runner/foreman.py`) for one typed decision, `enforce()` in `factory/runner/model.py` clamps it to the caps and hard stops, and `apply_decision` turns it into the next transition.
5. Ship ends with a pull request; `factory outcome`, `factory export`, and `factory gc` record what happened after, export a checksummed bundle (run records, the foreman/ turns without raw streams, and each attempt's stderr.log), and archive merged runs.

### A foreman turn
1. The worker writes foreman/digest.json (every attempt, condition, cap, and path) and resumes the run's Codex thread with one event message (`factory/runner/foreman.py`).
2. The session reads the attempt's gate.json, last-message.md, plan files, and git state, may edit `.dev/` and push, and answers one factory.decision/1 object validated by `factory/scripts/factory_records.py`.
3. The turn is recorded under foreman/turns/{n}/ as factory.foreman-turn/2, with a copy of the digest it was shown and the policy hash (the generated foreman skill and the factory-run.md it links) its session's cold turn resolved (`factory/runner/provenance.py`); a malformed or timed-out turn falls back to `model.decide()`.

### Build and label history
1. `factory history build` (history, `factory/runner/history.py`) reads each finished run under runs/ and archive/ and writes ~/.factory/history/{run-id}/: world.json and one point per headless attempt that reached a decision, with its digest snapshot, event, named files, and plan inputs, every path rewritten; it never writes inside a run directory.
2. `factory history label` runs the hindsight skill in one read-only Codex session per world; the runner validates its factory.hindsight/1 answer and splits it into labels.json and faults.json, keeping every human label from `--set`.

### Dream and deploy
1. `factory dream` (dream, `factory/runner/dream.py`) splits labelled worlds into selection and confirmation, replays the incumbent foreman skill on every selection point (cached under ~/.factory/dreams/cache/), and scores each answer against its label.
2. Each round the dream skill reads the selection traces and writes a revised skill; deterministic checks reject a candidate before any replay, and a passing one is staged as a full plugin tree and scored the same way.
3. The best by selection score, incumbent included, is scored on confirmation; the report and decision.json land under ~/.factory/dreams/{dream-id}/.
4. A winner that clears the floor and the margin is written to `factory/skills/foreman/SKILL.md`, rebuilt into plugins/, validated, committed on main, and pushed; runs read it on their next cold turn.

### Building the Codex distributions
1. `scripts/build_codex_plugin.py` copies skills/, references/, and `scripts/` of each source plugin into plugins/{name}/, strips Claude-only frontmatter, and writes agents/openai.yaml.
2. `scripts/validate.sh` checks structure, links, skill length, unattended wording (F03), the runner tests (F01), and the offline benchmark (F04); `--check` mode of the generator fails when `plugins/` is stale.

## Boundaries

| Boundary | Kind | Owned by | Notes |
| -------- | ---- | -------- | ----- |
| ~/.factory runtime root | store | factory runner | runs/, archive/, exports/, slots/, history/, dreams/, config.json, optional pricing.json; overridden by `FACTORY_HOME` |
| Codex CLI | external process | factory runner | headless stages, the foreman, the hindsight labeller, dream replays and revisions, and paid evals; stubbed by `factory/runner/tests/stubs/codex` in tests |
| Claude Code CLI | external process | factory runner | interactive scope only |
| git and GitHub | external | factory runner | worktrees, pushes, `gh pr` calls; the ship gate verifies the PR head against local HEAD; a dream deploy commits to this repository's main and pushes it |
| Chrome | external process | factory runner | one headless browser per build and ship attempt for e2e, over CDP |
| Jira | external HTTP | dev and factory skills | through `acli`, only when .dev/config.json enables it |
| consuming repository | store | the skills | `.dev/` plan files, `docs/` ledgers, .factory/contract.json |

## Cross-cutting

Records: every runner-owned file has a schema id and a strict validator in `factory/scripts/factory_records.py`, shared by the skills and the runner.
Testing: `factory/runner/tests/` runs the runner end to end against stub hosts; paid model calls never run in `scripts/validate.sh`.
Rules: no em dash anywhere, SKILL.md under roughly 150 lines, every skill `disable-model-invocation: true`, unattended factory material never routes a decision to a person.

## Entry points

`factory/bin/factory` (the CLI, `python3 -m runner`), `scripts/validate.sh`, `scripts/build_codex_plugin.py`, `scripts/test_factory_runner.sh`, `factory/evals/bench/bench.py`, `factory/evals/foreman/run.py`, `dev/scripts/skill-metrics.py`.
