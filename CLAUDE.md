# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## Repository Overview

This is a monorepo for Tobrun's Claude Code, Codex, and Pi skills. The root
`.claude-plugin/marketplace.json` references the Claude source plugins.
`.agents/plugins/marketplace.json` references generated Codex plugins under
`plugins/`. The root `package.json` exposes the source skills as a Pi package.

## Repository Structure

```
.
├── .claude-plugin/
│   └── marketplace.json    # Marketplace manifest listing all plugins
├── .agents/plugins/
│   └── marketplace.json    # Codex marketplace manifest
├── {plugin-name}/          # Individual plugin directory
│   ├── .claude-plugin/
│   │   └── plugin.json     # Plugin metadata (name, version, author)
│   ├── README.md           # Plugin documentation
│   └── skills/
│       └── {skill-name}/
│           ├── SKILL.md    # Skill definition with YAML frontmatter
│           ├── references/ # Deep-dive docs loaded on demand
│           └── examples/   # Worked examples
├── plugins/
│   └── {plugin-name}/      # Generated Codex distribution
├── package.json             # Pi package manifest
└── scripts/
    └── build_codex_plugin.py
```

## Key Rules

- All changes must pass `scripts/validate.sh` before committing.
- Every plugin directory name must match its `plugin.json` name and marketplace entry name.
- Every `SKILL.md` must have YAML frontmatter with `name` and `description`.
- Every `SKILL.md` must set `disable-model-invocation: true`; all skills in this repo are human-triggered only, and skills recommend the next step instead of invoking each other.
- Do not edit `plugins/dev/` directly. Run `python3 scripts/build_codex_plugin.py`
  after changing `dev/`; the generator removes Claude-only frontmatter and
  writes Codex `agents/openai.yaml` invocation policy.
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
- `python3 scripts/build_codex_plugin.py` - Regenerate the Codex distribution.
- Codex testing: `codex plugin marketplace add .` then
  `codex plugin add {name}@nurbot`.
- Pi testing: `pi install .` then `pi list`.
- Local testing: `/plugin marketplace add ~/ws/workflow` then `/plugin install {name}@nurbot`.
- If changes aren't picked up after reinstall, bump the version with a `-devN` suffix in `plugin.json`.
