## Plugins

| Plugin | Use When | Tools |
| ------ | -------- | ----- |
| [dev](dev/) | A test-focused development workflow for Claude Code, Codex, opencode, and Pi. | `scope`, `scope-quick`, `scope-review`, `commit`, `build`, `ship`, `ship-quick`, `yolo`, `reflect`, `to-pitch`, `to-quiz` |
| [factory](factory/) | Take a request from scope to a shipped pull request unattended, on Claude Code or Codex. | `run` |
| [bootstrap](bootstrap/) | Prepare any repository for agent work: probe its stack and write its root AGENTS.md from the workflow's SDLC lessons, on Claude Code or Codex. | `agents-md` |

## Claude Code

```bash
/plugin marketplace add tobrun/workflow
/plugin install dev@nurbot
/plugin install factory@nurbot
/plugin install bootstrap@nurbot
```

Invoke skills as `/scope`, `/build`, and so on, or namespaced as `/dev:scope`; factory's skill is `/factory:run` and bootstrap's is `/bootstrap:agents-md`.

## Codex

```bash
codex plugin marketplace add tobrun/workflow
codex plugin add dev@nurbot
codex plugin add factory@nurbot
codex plugin add bootstrap@nurbot
```

Invoke skills as `$dev:scope`, `$dev:build`, `$factory:run`, `$bootstrap:agents-md`, and so on.
factory needs `agents.max_depth` of at least 2; see [factory/README.md](factory/README.md#install).
The distribution is explicit-invocation only.
The checked-in Codex packages under `plugins/` are generated from each source plugin:

```bash
python3 scripts/build_codex_plugin.py
```

## opencode

opencode reads the Claude-format source skills directly.
Clone the repository anywhere and, from its root, symlink the skills into opencode's global skill directory:

```bash
git clone https://github.com/tobrun/workflow
cd workflow
mkdir -p ~/.config/opencode/skills
for skill in "$PWD"/dev/skills/*/; do
  ln -sfn "${skill%/}" ~/.config/opencode/skills/"$(basename "$skill")"
done
ln -sfn "$PWD/dev/references" ~/.config/opencode/references
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
