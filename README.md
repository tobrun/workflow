## Skills

| Plugin | Use when | Skills |
| ------ | -------- | ------ |
| [dev](dev/) | A test-focused development workflow, from a spec to a verified pull request. | `scope`, `build`, `ship`, `commit`, `yolo`, `bootstrap` |

The skills follow the [Agent Skills](https://agentskills.io) standard, so any harness that reads `SKILL.md` folders can run them: Claude Code, Codex, GitHub Copilot, Cursor, OpenCode, Amp, Antigravity, Pi, and more.
Install the whole set: `yolo` follows `scope`, `build`, and `ship` from the folders beside it.
Every skill is explicit-invocation only - ask for it by name; each one recommends the next instead of running it.

## Install

Pick the line for your harness. Each installs the same six skills.

| Harness | Install | Invoke |
| --- | --- | --- |
| Claude Code (plugin) | `/plugin marketplace add tobrun/workflow` then `/plugin install dev@nurbot` | `/dev:scope`, `/dev:yolo`, ... |
| Codex (plugin) | `codex plugin marketplace add tobrun/workflow` then `codex plugin add dev@nurbot` | `$dev:scope`, `$dev:yolo`, ... |
| Any Agent Skills harness | `npx skills add tobrun/workflow --skill '*' -a claude-code -a codex -a cursor -a opencode` | ask for the skill by name |
| GitHub CLI | `gh skill install tobrun/workflow --all --agent <harness>`, pinned with `--pin <tag or sha>` | ask for the skill by name |
| Pi | `pi install git:github.com/tobrun/workflow` | `/skill:scope`, ... |
| By hand | copy the folders under [skills/](skills/) into `.agents/skills/` (Codex, Copilot, Cursor, OpenCode, Amp, Antigravity) or `.claude/skills/` (Claude Code) | ask for the skill by name |

`npx skills` keeps one copy in `.agents/skills/`, links it into each harness's own folder, and records the install in `skills-lock.json`; `npx skills update` refreshes it.
`-g` installs for your user instead of the project.

Skills that are not invoked never help: in a project that uses them, add one line to its AGENTS.md naming when to reach for each, such as "Plan changes with the scope skill, then build and ship".

### Keeping them manual

The Claude Code plugin marks every skill `disable-model-invocation: true`, and the Codex plugin sets `allow_implicit_invocation: false`.
The portable copies keep to the six standard frontmatter fields, so other harnesses decide invocation themselves: in OpenCode, add a `permission.skill` rule set to `ask` (see [dev/README.md](dev/README.md#opencode-installation)); elsewhere, invoke them by name.

### Review agents

`ship`'s review panel runs on the harness's own subagents (Claude Code, Codex, OpenCode, Copilot); on Pi it launches isolated `pi --print` processes in parallel.

## Layout

- `dev/` is the source and the Claude Code plugin: the skills in `dev/source/`, plus the references and scripts several skills share. The folder is not called `skills/` on purpose: installers scan a repository for `skills/` folders, and must find only the portable copy.
- `skills/` is the portable tree, generated: each skill self-contained, with the shared files it uses copied in. `npx skills`, `gh skill`, Pi, and hand copies install from here.
- `.codex-plugin/plugin.json` makes the repository root the Codex plugin; it installs `skills/`, whose folders carry each skill's `agents/openai.yaml`.

Installers therefore find each skill exactly once. `dev/source/` stays separate because only the Claude Code plugin can mark its skills manual-only, with a frontmatter field the Agent Skills spec does not allow.

```bash
python3 scripts/build_distributions.py         # regenerate skills/ and .codex-plugin/
python3 scripts/check_skills.py skills          # the Agent Skills spec, and self-containment
```
