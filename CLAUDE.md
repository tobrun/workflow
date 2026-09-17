# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Repository Overview

This is a monorepo for Tobrun's Claude Code, Codex, and Pi skills. The root
`.claude-plugin/marketplace.json` references the Claude source plugins.
`.agents/plugins/marketplace.json` references generated Codex plugins under
`plugins/`. The root `package.json` exposes the `dev` source skills as a Pi
package. There are two plugins: `dev`, the hand-invoked development workflow,
and `factory`, factory copies of four `dev` skills plus a local runner that
chains them into an unattended pipeline.

## Repository Structure

```
.
├── .claude-plugin/
│   └── marketplace.json    # Marketplace manifest listing all plugins
├── .agents/plugins/
│   └── marketplace.json    # Codex marketplace manifest
├── {plugin-name}/          # Individual plugin directory (dev, factory)
│   ├── .claude-plugin/
│   │   └── plugin.json     # Plugin metadata (name, version, author)
│   ├── README.md           # Plugin documentation
│   ├── references/         # Shared references across the plugin's skills
│   ├── scripts/            # Shared scripts
│   ├── evals/              # Skill evals (not shipped to Codex)
│   └── skills/
│       └── {skill-name}/
│           ├── SKILL.md    # Skill definition with YAML frontmatter
│           ├── references/ # Deep-dive docs loaded on demand
│           └── examples/   # Worked examples
├── factory/
│   ├── bin/factory         # Factory CLI entry point (not shipped to Codex)
│   └── runner/             # Stdlib-only Python runner and its tests
├── plugins/
│   └── {plugin-name}/      # Generated Codex distribution
├── package.json             # Pi package manifest (dev only)
└── scripts/
    ├── build_codex_plugin.py
    ├── test_factory_runner.sh
    └── validate.sh
```

## Key Rules

- All changes must pass `scripts/validate.sh` before committing.
- Every plugin directory name must match its `plugin.json` name and marketplace entry name.
- Every `SKILL.md` must have YAML frontmatter with `name` and `description`.
- Every `SKILL.md` must set `disable-model-invocation: true`; all skills in this repo are human-triggered only, and skills recommend the next step instead of invoking each other.
  The one sanctioned invoker is `factory/runner/`: it launches each factory stage as a fresh process and chains stages through artifacts on disk, so factory skills still never invoke each other.
- `factory/skills/` are intentional, divergent copies of `dev` skills; do not sync them back or forward mechanically, and keep `dev` behavior unchanged when changing them.
- Every factory stage `SKILL.md` must mention `factory-run.json` and its own `{stage}-result.json`.
- Unattended factory material (`factory/skills/{scope-review,build,ship,foreman}/` and `factory/references/`) never routes a decision to a person: it takes the recommended option under the policy in `factory/references/factory-run.md` and parks only for that file's fixed list.
  `factory/skills/foreman/` is not a stage: it is the one long-lived Codex session the runner resumes after every attempt to choose the next step (`factory/runner/foreman.py`, decision record `factory.decision/1` in `factory/scripts/factory_records.py`); its parks carry an exact `operator_action`, and the runner alone enforces hard stops and caps.
  Text only interactive `scope` uses goes between `<!-- interactive-only -->` markers; `validate.sh` F03 enforces this.
- In `factory/runner/`, the detached worker holding `worker.lock` is the only writer of `run.json`.
  Operator controls (`runner/control.py`, shared by the CLI and the attached view) leave intent files for a live worker and take the lock only for runs with no live worker; plan files under `.dev/` are never committed.
- Do not edit `plugins/` directly. Run `python3 scripts/build_codex_plugin.py`
  after changing `dev/` or `factory/`; the generator builds every configured
  plugin, removes Claude-only frontmatter, and writes Codex
  `agents/openai.yaml` invocation policy. It never copies `runner/`, `bin/`,
  or `evals/`.
- Keep the root Pi package version equal to `dev/.claude-plugin/plugin.json`.
- Preserve native subagent behavior in `ship`; Pi-specific orchestration
  belongs in its subprocess transport and bundled runner.
- Skill descriptions must state both what the skill does and when to use it.
- No file in the repo may contain an em dash.
- Keep `SKILL.md` body under roughly 150 lines; move detail to `references/`.
- Keep skill instructions minimal: models treat long rule lists as guidelines and lose the middle of a grown context.
  When a skill needs to enforce quality, prefer a deterministic check it loops against over adding more prose instructions.

## Key Commands

- `scripts/validate.sh` - Validate the entire repository structure.
- `python3 scripts/build_codex_plugin.py [--check] [--plugin dev|factory]` - Regenerate or check the Codex distributions.
- `cd factory && python3 -m unittest discover -s runner/tests -t .` - Factory runner unit and integration tests.
- `python3 factory/evals/foreman/run.py` - Paid foreman evals against captured real-run cases; never part of `validate.sh`.
- `bash scripts/test_factory_runner.sh` - Offline end-to-end factory runner test with stub hosts.
- Codex testing: `codex plugin marketplace add .` then
  `codex plugin add {name}@nurbot`.
- Pi testing: `pi install .` then `pi list`. Pi ships `dev` only; its flat skill namespace would collide with `factory`.
- Local testing: `/plugin marketplace add ~/ws/workflow` then `/plugin install {name}@nurbot`.
- If changes aren't picked up after reinstall, bump the version with a `-devN` suffix in `plugin.json`.
