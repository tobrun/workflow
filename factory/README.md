# factory

An orchestrator skill that takes a request through an interactive `scope`, then runs `scope-review`, `build`, and `ship` as unattended phases on the current checkout - judging each phase's completion itself, repairing or relaunching it on failure, and ending in a pull request or a report. No runner process to install: the whole thing is one skill, `run`, that launches subagents through the host's own subagent tool.

## Install

- **Claude Code**: `/plugin marketplace add tobrun/workflow` then `/plugin install factory@nurbot`, or locally `/plugin marketplace add ~/ws/workflow` then `/plugin install factory@nurbot`.
- **Codex**: `codex plugin marketplace add .` then `codex plugin add factory@nurbot`; set `agents.max_depth` to at least 2, since scope-review's and ship's own panels run one level inside the phase subagent.

## The run flow

Invoke `/factory:run "a quoted request"` or `/factory:run path/to/request.md`. `run` checks the transport, scopes the change with you inline (the only interactive phase), then ends with one go question. From there it launches `scope-review`, `build`, and `ship` in order, each as a fresh-context subagent, judging every result against the phase's own deterministic checks before advancing. A found secret or a destructive action outside the run branch stops the run immediately; anything else gets repaired, relaunched, or - after three attempts of one phase - ends the run with a report naming what a person could do.

Invoke `/factory:run` with no request to resume: the plan on the checked-out branch, or the single unfinished state file under `.dev/`.

## Prerequisites

- Every host: a secret scanner ship's gauntlet can run offline - `gitleaks`, or the repo-fitted grep `references/tools.md` allows - since the gauntlet's acquire-a-missing-tool step needs a network a factory run may not have.
- Codex additionally: `GH_TOKEN` (or `GITHUB_TOKEN`) exported before launching Codex, because the workspace-write sandbox blocks the macOS keychain `gh` normally uses; `gh auth setup-git` so `git push` over HTTPS uses the same token; `sandbox_workspace_write.network_access=true`, since the sandbox has no network by default.

Ignoring `.dev/` in the consuming repository is not a prerequisite: the run's own preflight checks `git check-ignore -q .dev/` and appends the entry to `.gitignore` itself, recorded as a repair, when it is missing.

## Clear and resume

The run lives as long as the session; `.dev/{plan}/factory-run.json` is what survives a closed one. Clear the session and invoke `/factory:run` again with no request to pick up where it left off, with a fresh context.
