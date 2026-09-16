## Plugins

| Plugin | Use When | Tools |
| ------ | -------- | ----- |
| [dev](dev/) | A test-focused development workflow for Claude Code, Codex, opencode, and Pi. | `scope`, `commit`, `build`, `ship`, `to-pitch`, `to-quiz` |
| [factory](factory/) | Turning one interactively scoped change request into a ready pull request, with `scope-review`, `build`, and `ship` running unattended through the local `factory` CLI. Claude Code and Codex only. | `scope`, `scope-review`, `build`, `ship`, `factory` CLI |

## Claude Code

```bash
/plugin marketplace add tobrun/workflow
/plugin install dev@nurbot
/plugin install factory@nurbot
```

Invoke skills as `/scope`, `/build`, and so on.
With both plugins installed, use the namespaced form (`/dev:scope`, `/factory:scope`) to pick one.

## Codex

```bash
codex plugin marketplace add tobrun/workflow
codex plugin add dev@nurbot
codex plugin add factory@nurbot
```

Invoke skills as `$dev:scope`, `$dev:build`, and so on. Both
distributions are explicit-invocation only. The checked-in Codex packages under
`plugins/` are generated from `dev/` and `factory/`:

```bash
python3 scripts/build_codex_plugin.py
```

## Factory CLI

The factory runs the pipeline from your terminal. Install both plugins above,
then put the CLI on your `PATH`:

```bash
git clone https://github.com/tobrun/workflow ~/ws/workflow
ln -s ~/ws/workflow/factory/bin/factory ~/.local/bin/factory
factory doctor
factory new ~/ws/project "Add idempotency protection to webhook processing"   # or --file PATH, --jira KEY
```

`factory new` stays open after the handoff with a live view of the assembly line and keys to pause, note, cancel, and retry the run; the work itself runs in a detached worker.
See [factory/README.md](factory/README.md) for the operator guide.

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

Pi ships `dev` only: its flat skill namespace would collide `factory`'s
`scope`, `build`, and `ship` with `dev`'s.

```bash
pi install git:github.com/tobrun/workflow
```

Invoke skills as `/skill:scope`, `/skill:build`, and so on. Pi consumes
`dev/skills/` directly through the root `package.json`; no generated Pi copy is
needed.

`ship` preserves its independent-agent review panel on Pi by launching isolated
`pi --print` processes in parallel. Claude Code, Codex, and opencode continue
to use their native subagent facilities.
