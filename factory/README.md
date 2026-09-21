# factory

An orchestrator skill that drives a pipeline of typed phases on the current checkout - the interactive ones inline, the rest as unattended subagents - judging each phase's completion itself, repairing or relaunching it on failure, and ending in a pull request or a report. The pipeline is declared in `.factory/config.yaml` in your repository; with no config it runs our default, `scope`, `scope-review`, `build` and `ship`, from the installed plugin. No runner process to install: the whole thing is one skill, `run`, that launches subagents through the host's own subagent tool, so the plugin adds one skill to your namespace and no more.

## Install

- **Claude Code**: `/plugin marketplace add tobrun/workflow` then `/plugin install factory@nurbot`, or locally `/plugin marketplace add ~/ws/workflow` then `/plugin install factory@nurbot`.
- **Codex**: `codex plugin marketplace add .` then `codex plugin add factory@nurbot`; set `agents.max_depth` to at least 2, since scope-review's and ship's own panels run one level inside the phase subagent.

## The run flow

Invoke `/factory:run "a quoted request"` or `/factory:run path/to/request.md`. `run` checks the transport and validates the pipeline with `factory-config.py check`, runs the interactive phases with you inline (`scope` in the default), then ends with one go question. From there it launches the remaining phases (`scope-review`, `build`, and `ship` in the default) in order, each as a fresh-context subagent, judging every result against its type's declared checks and outcome contract before advancing. A found secret or a destructive action outside the run branch stops the run immediately; anything else gets repaired, relaunched, or - after three attempts of one phase - ends the run with a report naming what a person could do.

Invoke `/factory:run` with no request to resume: the plan on the checked-out branch, or the single unfinished state file under `.dev/`.

## Prerequisites

- Every host: a secret scanner ship's gauntlet can run offline - `gitleaks`, or the repo-fitted grep `references/tools.md` allows - since the gauntlet's acquire-a-missing-tool step needs a network a factory run may not have.
- Codex additionally: `GH_TOKEN` (or `GITHUB_TOKEN`) exported before launching Codex, because the workspace-write sandbox blocks the macOS keychain `gh` normally uses; `gh auth setup-git` so `git push` over HTTPS uses the same token; `sandbox_workspace_write.network_access=true`, since the sandbox has no network by default.

Ignoring `.dev/` in the consuming repository is not a prerequisite: the run's own preflight checks `git check-ignore -q .dev/` and appends the entry to `.gitignore` itself, recorded as a repair, when it is missing.

## Clear and resume

The run lives as long as the session; `.dev/{plan}/factory-run.json` is what survives a closed one. Clear the session and invoke `/factory:run` again with no request to pick up where it left off, with a fresh context.

## The pipeline config

`python3 factory/scripts/factory-config.py init` writes a starter `.factory/config.yaml`; `show --resolved`, `check`, `set` and `unset` read and edit it, and you normally never edit the file by hand. Each phase names a `type` declared under `types`, and a type declares exactly five things: `interactive`, `requires` (the outcome, in prose), `checks` (argv commands that verify it), `seals` (artifacts watched for drift) and `attempts`. The full schema is in [references/pipeline-config.md](references/pipeline-config.md).

A phase is one of three classes. A built-in phase runs from the installed plugin. An injected phase is a copy under `.factory/skills/` whose hash still matches `.factory/.inject.json`. Anything else is foreign, and a foreign phase must declare `unattended_safe: true`: nothing can detect a phase that blocks on a person, so that declaration is the word of whoever read the skill.
