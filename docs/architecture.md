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
| remote transport | `remote.py` starts a run on the fleet instance the user names, follows it, continues it, and pulls its branch and plan files home; `runner.sh` keeps one headless Claude Code yolo session going on the instance until its pull request is open, and `view.py` follows it in a terminal on the instance's desktop | `dev/skills/remote/` | the fleet host over SSH (`incus exec/list`, `fleet start/view`), git |
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
1. A person invokes `/dev:remote {n} {plan or task}`; `remote.py start` refuses only a missing plan or an instance that is already running a run.
2. It streams a git bundle of HEAD (without what the instance already has), the plan directory, the plugin, the contract, and the runner into `~/.dev-workflow/run/` on instance `{n}` over `incus exec`, moves the instance's earlier run to `~/.dev-workflow/history/`, and starts `runner.sh run` as the transient unit `dev-remote`.
3. The runner checks out the work branch and starts one `claude -p "/dev:yolo ..."` session with the plugin and the unattended contract; that session builds, ships, pushes, and opens the pull request.
4. Each time the session ends, the runner checks the deliverable (clean tree, a pull request for the branch whose head is the checkout's HEAD) and resumes the same session with what is missing; `status.json` ends as `done`, `blocked` (the session wrote `blocked.md`), or `failed` (attempts spent).
5. `remote.py continue` restarts the runner on the same checkout and session, with an optional instruction; `remote.py pull` fetches the work branch as a bundle, fast-forwards the local branch, and merges plan files against the hashes both sides last agreed on.
6. The laptop keeps no run record: state, logs, and history live on the instance and are addressed by its number.

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
| fleet host and its instances | remote compute | the remote skill | reached only over SSH with the user's key; instances hold only the git credentials the host allocates (push and PRs on the fleet's repositories), never the user's own; commits cross as git bundles and files as tar streams over `incus exec`; what comes back is untrusted data |
| consuming repository | store | the skills | `.dev/` plan files, `docs/` ledgers, shared config, and the root AGENTS.md bootstrap writes |

## Cross-cutting

Testing: `scripts/validate.sh` is the single gate; paid model calls never run in it.
Rules: no em dash anywhere, SKILL.md under roughly 150 lines, every skill `disable-model-invocation: true`.

## Entry points

`scripts/validate.sh`, `scripts/build_codex_plugin.py`, `scripts/test_pi_runner.sh`, `dev/scripts/skill-metrics.py`, `dev/scripts/claims.py`, `dev/skills/remote/scripts/remote.py`, `bootstrap/skills/agents-md/scripts/check-agents-md.py`.
