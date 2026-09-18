# Repository Contract

How the factory runs a repository's own checks.
The contract is the committed file `.factory/contract.json` in the target repository, with schema `factory.repo-contract/1`.
Interactive `scope` creates or updates it with the operator; the runner validates it at the scope gate and executes it in the build gate.
The spec's `### Validation` block is its human-readable rendering and must name every validation command `id`.

```json
{
  "schema": "factory.repo-contract/1",
  "boundary": "workspace-write",
  "environment": {"required": ["DATABASE_URL"], "passthrough": ["NODE_OPTIONS"]},
  "setup": [{"id": "deps", "run": ["npm", "ci"], "timeout_s": 900}],
  "validation": [
    {"id": "typecheck", "run": ["npm", "run", "typecheck"]},
    {"id": "unit", "run": ["python3", "-m", "pytest", "-q", "tests/unit"], "cwd": "backend", "timeout_s": 1200},
    {"id": "ci", "script": "scripts/ci-local.sh", "shell": "bash", "set": {"CI": "1"}}
  ],
  "tests": {"run": {"run": ["python3", "-m", "pytest", "-q", "--junitxml", "{junit}", "{tests}"]}, "results": "junit",
            "layers": {"unit": ["tests/unit/**"], "integration": ["tests/integration/**"]}},
  "ports": ["api"],
  "services": [{"id": "api", "run": ["python3", "-m", "app", "--port", "{port_api}"],
                "ready": {"run": ["curl", "-sf", "http://127.0.0.1:{port_api}/health"]}}],
  "e2e": {"driver": {"script": "e2e/run.py", "args": ["--out", "{out}"]}, "services": ["api"]},
  "ci": {"required_checks": ["test", "lint"]}
}
```

`ci` is required: the pull-request checks that must finish green, or `{"none": "why this repository has no CI"}`.
`e2e` is a driver, or `{"none": "why"}` for a change with no application to launch; a plan with `[e2e]` scenarios needs a driver.
`tests`, `services`, and `ports` describe how the runner itself executes mapped tests and the e2e driver; placeholders are `{tests}`, `{junit}`, `{out}`, `{run_dir}`, and `{port_<name>}` for each declared port.
Scenario maps name tests from the worktree root; when `tests.run` has a `cwd`, the runner passes `{tests}` relative to that directory and reads the results back the same way, so a `path::name` id works whichever directory the test runner reports from.
A test mapped outside its layer's globs is reported as a warning on the attempt, not a failure: the runner still executes it and judges its outcome.

## Command records

- `run` is an argv list, executed without a shell: `["npm", "test"]`, never `"npm test"`.
- `script` is one repository script run as a whole, so `cd`, `export`, line continuations, and `if` blocks keep their meaning; `shell` picks `sh` (default for a non-executable file) or `bash`.
- `cwd` is relative to the repository root and stays inside it.
- `env` names the environment variables the command reads; `set` gives literal values.
  A command sees only a small baseline (`PATH`, `HOME`, `USER`, `SHELL`, `LANG`, `TMPDIR`, `TERM`, `TZ`, and locale variables) plus what the contract declares, never the runner's whole environment.
  A declared variable that is unset fails the gate before anything starts, naming the variable.
- `timeout_s` caps one command (default 1200); the attempt's shared deadline caps it further, so several commands never each get a fresh allowance.
- `boundary` overrides the contract-wide `boundary` for one command.

## Execution boundaries

| Boundary | Filesystem | Network | Enforced by |
| --- | --- | --- | --- |
| `host` (default) | everything the runner's user can write | open | nothing: the runner's own privileges |
| `workspace-write` | the worktree, the command's execution directory, and the system temporary directories | open | `sandbox-exec` on macOS, `bwrap` on Linux |

`workspace-write` matches the Codex sandbox the factory launches agents with (`workspace-write` with network access), so a check that passed for the agent behaves the same for the gate.
A host without the enforcement tool fails the command with a repair step instead of running it unconfined.
The runner points `npm_config_cache`, `UV_CACHE_DIR`, `PIP_CACHE_DIR`, `YARN_CACHE_FOLDER`, `BUN_INSTALL_CACHE_DIR`, and `AGENT_BROWSER_SOCKET_DIR` at shared directories under `~/.factory/cache/` (a value already in the environment is kept and made writable).

## The hosted browser

Chrome cannot start inside the Codex sandbox: its policy is closed by default and denies the Mach services Chrome needs at startup, so a browser the agent launches exits before it listens, whatever `--no-sandbox` says.
The runner therefore hosts one headless Chrome per build and ship attempt and per e2e gate, outside that sandbox but confined by the `workspace-write` boundary to its own profile directory and the temporary directories, on a leased loopback port.
Agents, the e2e driver, and its services receive `AGENT_BROWSER_CDP` (every `agent-browser` session attaches to the hosted browser) and `FACTORY_BROWSER_CDP_URL` (for Playwright's `chromium.connectOverCDP`), so a driver behaves the same for the agent and at the gate.
Closing an `agent-browser` session leaves the browser running; the runner stops it when the attempt or gate ends.
The browser is the newest Chrome the host already has: `FACTORY_BROWSER_BIN` or `AGENT_BROWSER_EXECUTABLE_PATH`, agent-browser's download, Playwright's Chromium, a system Chrome or Chromium, then `PATH`; `factory doctor` reports which.
A host without one records `browser: unavailable` on the attempt and the run proceeds; the runner's own sandboxed commands still carry `AGENT_BROWSER_ARGS=--no-sandbox` so a driver that launches its own Chrome there works.
`"browser": "off"` in `~/.factory/config.json` disables hosting.

## Setup

The runner runs `setup` in the run's worktree before the build and ship agents start, and again at those gates and in the `[repro]` base worktree.
It reruns only when a dependency file (`package.json`, lockfiles, `pyproject.toml`, `uv.lock`, `requirements*.txt`, and similar) or the setup records change, or when something a command installed is gone; a failure parks the run with `setup.failed` and the path to the full output.
A setup command may declare `produces`, the worktree-relative paths it installs (`["node_modules", "omr-ui/node_modules"]`); a known package-manager install implies its output when the record is silent (`npm ci`, `npm install`, `yarn`, `pnpm install`, and `bun install` imply `node_modules` in the command's `cwd`, `uv sync` implies `.venv`).
A declared path that is missing after the command succeeds is reported as a warning on the attempt, never a failure.
Every produced path is added to the repository's `info/exclude`, so an installed dependency directory never appears in Git status or in an agent's `git add -A`, and its later disappearance reruns setup instead of failing the gate's tests with a missing runner.

## Checking a repository before a run

`factory doctor --repo PATH` validates the contract, reports unset required environment names and commands that are not on `PATH` or not committed, confirms a GitHub token reaches sandboxed attempts, and commits in a throwaway linked worktree under the narrowed sandbox grants.
`--run-setup` also runs the `setup` commands there through the runner's executor, and `--smoke` makes one real, paid Codex call per headless model to confirm `$factory:{stage}` resolves, cached by Codex version, skills identity, and model.
`factory new` refuses to hand off while a required environment name is unset.

## What the runner records

Every command runs under the runner's guarded executor: it starts only when the run is not cancelled and the attempt deadline has not passed, cancellation stops it and its process group, and it leaves `request.json`, `executor.json`, `receipt.json`, `stdout.log`, and `stderr.log` under `attempts/{stage}-{n}/gate/`.
The receipt holds the argv hash, working directory, boundary, timestamps, classification (`succeeded`, `failed`, `timeout`, `cancelled`, `spawn_failed`), exit code, and output hashes.
