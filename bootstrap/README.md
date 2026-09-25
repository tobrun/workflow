# bootstrap

Prepare a repository for agent work.
The `agents-md` skill probes the repository's stack, maps what it finds onto the SDLC concepts the [dev](../dev/) workflow is built on, and writes or refreshes the root `AGENTS.md`.

The file it writes encodes those lessons as short outcome rules any agent can follow, with or without the dev plugin installed:
- tests on the cheapest layer that can fail for the right reason, and mocks only at boundaries the repo does not own;
- e2e against the built artifact in a stubbed, seeded environment, never production or shared staging;
- done means the merge gate passes locally, and findings are fixed at the source, never suppressed or retried away;
- conventional commits with a what and a why, plans kept out of git, and pull requests that carry evidence.

It fills those rules in with the repository's exact commands (setup, build, check, unit, integration, e2e, and the merge gate), verified by running them, and records every missing concept as an open item instead of scaffolding it.

## What it touches

Only the root `AGENTS.md`.
An existing AGENTS.md or CLAUDE.md is merged and pruned: repo-specific gotchas are kept, stale facts are updated, and a rule that contradicts a lesson becomes a question rather than a silent overwrite.
CLAUDE.md is read but never written; Claude Code reads AGENTS.md directly, so the closing report recommends removing a now-redundant CLAUDE.md.
Nothing is committed; the diff is shown for approval before the file is written.

Every draft loops against `skills/agents-md/scripts/check-agents-md.py` until it exits clean: required sections in order, every Commands slot filled or backed by an open item, no stale path, no command whose script, target, or file does not exist, and the stated default branch matching `origin/HEAD`.

## Install

### Claude Code

```bash
/plugin marketplace add tobrun/workflow
/plugin install bootstrap@nurbot
```

Invoke it as `/bootstrap:agents-md`.

### Codex

```bash
codex plugin marketplace add tobrun/workflow
codex plugin add bootstrap@nurbot
```

Invoke it as `$bootstrap:agents-md`.
The distribution is explicit-invocation only.
