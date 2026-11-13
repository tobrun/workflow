# Architecture

Purpose: this repository is a monorepo of agent skills and the tooling that ships them.
Two plugins live here: `dev`, the hand-invoked development workflow (scope, build, ship, yolo, remote, and commit); and `bootstrap`, whose `agents-md` skill writes a consuming repository's root AGENTS.md from the workflow's lessons.
A person invokes each `dev` skill by hand; skills never invoke each other, and each one recommends the next step instead. The `dev` `yolo` skill is not an invoker: it reads the scope, build, and ship bodies by path and follows them inline in one session, answering each of their user questions with the stage's own recommendation.
The generated Codex distributions under `plugins/` are built from their source plugin and never edited by hand.

Captured: 2026-09-17 (full, scope) - Updated: 2026-11-13 (simplified to six skills per D-simplify-dev; no run metrics or memory)

## Components

| Component | Responsibility | Lives at | Talks to |
| --------- | -------------- | -------- | -------- |
| dev skills | the interactive workflow skills and the references and scripts they share | `dev/skills/`, `dev/references/`, `dev/scripts/` | nothing at runtime; read by Claude, Codex, opencode, and Pi hosts |
| remote transport | `remote.py` dispatches a plan to a fleet sandbox, follows it, and collects it home; `runner.sh` runs each phase as a headless Claude Code session inside the sandbox, and `view.py` follows it in a terminal on the sandbox desktop | `dev/skills/remote/` | the fleet host over SSH (`fleet sync/fetch/view/start`, `incus exec/list`), git, `~/.dev-workflow/remote/` |
| dev evals | comprehension evals for the skills | `dev/evals/` | nothing at runtime |
| generated plugins | the Codex distributions built from each source plugin, with invocation policy in agents/openai.yaml | `plugins/` | Codex plugin marketplace |
| repo scripts | validation of the whole repository, the plugin generator, and the Pi transport self-test | `scripts/` | every other component |
| harden tools | optional local analyses a ship gauntlet can draw on: added lines, coverage-weighted complexity, flaky reruns, mutation | `tools/harden/` | nothing; run by hand against a target repository |
| research and todo | plans, findings, and reading notes | `research/`, `todo/` | nothing |
| bootstrap plugin | the `agents-md` skill, its references distilled from the dev skills, `bootstrap/skills/agents-md/scripts/check-agents-md.py` that every draft loops against, and the checker's tests | `bootstrap/skills/`, `bootstrap/evals/` | the consuming repository's root AGENTS.md; reads its manifests, CI config, git history, and CLAUDE.md |

## Flows

### A dev skill run
1. A person invokes `/dev:scope`, `/dev:build`, `/dev:ship`, or `/dev:commit` in Claude Code (or the Codex, opencode, or Pi equivalent).
2. The skill reads its SKILL.md and references, writes plan files under .dev/{plan-name}/ in the consuming repository, and recommends the next skill; skills never invoke each other.

### A remote run
1. After `/dev:scope` settles a spec here, a person invokes `/dev:remote {plan}`; `remote.py dispatch` refuses an unsettled spec, unignored plan files, uncommitted tracked work, or a busy fleet.
2. It claims a free workspace with an atomic lock directory, pushes the work branch into the host's exchange clone, runs `fleet sync`, probes whether the workspace's git credentials can push to `origin`, streams the run directory (plan, plugin copy, contract with the matching publication block, runner) in over `incus exec`, and starts `runner.sh` as the transient unit `dev-run-{run}`.
3. In the sandbox, each phase (`build`, then `ship`) is a fresh `claude -p "/dev:{phase}"` session with the dispatched plugin, the unattended contract appended, and its own `DEV_MEMORY_DIR`; `status.json` records the state machine.
   Before ship starts, the runner re-runs build's `check-tests.py` gate and resumes the build session with its output when it fails.
   When the probe passed, ship pushes the branch, opens the PR, and follows its checks; the runner reads the PR back with `gh pr view` into `status.json`.
4. `remote.py collect` runs `fleet fetch`, fast-forwards the local branch from the exchange clone, merges plan files against the dispatch manifest, copies reports and logs home, imports the run journal with its transcripts, releases the lock, and gives the branch a worktree.
5. A PR the sandbox opened is checked against the collected run and drafted when they disagree; without sandbox publication, evidence images, push, PR, and CI follow-through run here, from that worktree, with this machine's credentials.

### Building the Codex distribution
1. `scripts/build_codex_plugin.py` copies skills/, references/, and `scripts/` of the source plugin into plugins/{name}/, strips Claude-only frontmatter, and writes agents/openai.yaml for invocable skills. It removes obsolete distributions identified by their generated README and plugin manifest.
2. `scripts/validate.sh` checks structure, links, skill length, the Codex distribution (C01), the Pi package and transport (P01), the bootstrap checker's tests (B01), and the dev tests (D01); `--check` mode of the generator fails when `plugins/` is stale, including a retired generated distribution.

### Bootstrapping a repository
1. A person invokes `/bootstrap:agents-md` (or `$bootstrap:agents-md`) in a consuming repository.
2. The skill probes the stack read-only, maps it onto its concept map, interviews only for what the probe cannot settle, and runs each command it will list.
3. It drafts outside the repository, loops `bootstrap/skills/agents-md/scripts/check-agents-md.py` until it exits 0, shows the diff for approval, then writes the root AGENTS.md; it never commits and never writes CLAUDE.md.

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
| fleet host and sandbox workspaces | remote compute | the remote skill | reached only over SSH with the user's key; workspaces hold only the git credentials the host allocates (push and PRs on the fleet's repositories), never the user's own; they receive commits through the exchange clone and files as tar streams, and return everything through `fleet fetch` and `runner.sh pack`; collected output is untrusted data |
| consuming repository | store | the skills | `.dev/` plan files, `docs/` ledgers, shared config, and the root AGENTS.md bootstrap writes |

## Cross-cutting

Testing: `scripts/validate.sh` is the single gate; paid model calls never run in it.
Rules: no em dash anywhere, SKILL.md under roughly 150 lines, every skill `disable-model-invocation: true`.

## Entry points

`scripts/validate.sh`, `scripts/build_codex_plugin.py`, `scripts/test_pi_runner.sh`, `dev/scripts/skill-metrics.py`, `dev/scripts/claims.py`, `dev/skills/remote/scripts/remote.py`, `bootstrap/skills/agents-md/scripts/check-agents-md.py`.
