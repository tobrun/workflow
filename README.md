## Plugins

| Plugin | Use When | Tools |
| ------ | -------- | ----- |
| [dev](dev/) | A test-focused development workflow for Claude Code, Codex, opencode, and Pi. | `scope`, `commit`, `build`, `ship`, `to-pitch`, `to-quiz` |

## Claude Code

```bash
/plugin marketplace add tobrun/workflow
/plugin install dev@nurbot
```

Invoke skills as `/scope`, `/build`, and so on.

## Codex

```bash
codex plugin marketplace add tobrun/workflow
codex plugin add dev@nurbot
```

Invoke skills as `$dev:scope`, `$dev:build`, and so on. Both
distributions are explicit-invocation only. The checked-in Codex package under
`plugins/dev/` is generated from `dev/`:

```bash
python3 scripts/build_codex_plugin.py
```

## opencode

opencode reads the Claude-format source skills directly; symlink them into its
global skill directory:

```bash
git clone https://github.com/tobrun/workflow ~/ws/workflow
mkdir -p ~/.config/opencode/skills
for skill in ~/ws/workflow/dev/skills/*/; do
  ln -sfn "$skill" ~/.config/opencode/skills/"$(basename "$skill")"
done
ln -sfn ~/ws/workflow/dev/references ~/.config/opencode/references
```

Ask the agent for a skill by name, for example "run the scope skill".
Add a `permission.skill` rule set to `ask` to keep the skills human-triggered;
see [dev/README.md](dev/README.md#opencode-installation) for the full setup.

## Pi

```bash
pi install git:github.com/tobrun/workflow
```

Invoke skills as `/skill:scope`, `/skill:build`, and so on. Pi consumes
`dev/skills/` directly through the root `package.json`; no generated Pi copy is
needed.

`ship` preserves its independent-agent review panel on Pi by launching isolated
`pi --print` processes in parallel. Claude Code, Codex, and opencode continue
to use their native subagent facilities.
