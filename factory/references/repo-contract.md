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

## Checking a repository before a run

`factory doctor --repo PATH` validates the contract, reports unset required environment names and commands that are not on `PATH` or not committed, confirms a GitHub token reaches sandboxed attempts, and commits in a throwaway linked worktree under the narrowed sandbox grants.
`--run-setup` also runs the `setup` commands there through the runner's executor, and `--smoke` makes one real, paid Codex call per headless model to confirm `$factory:{stage}` resolves, cached by Codex version, skills identity, and model.
`factory new` refuses to hand off while a required environment name is unset.

## What the runner records

Every command runs under the runner's guarded executor: it starts only when the run is not cancelled and the attempt deadline has not passed, cancellation stops it and its process group, and it leaves `request.json`, `executor.json`, `receipt.json`, `stdout.log`, and `stderr.log` under `attempts/{stage}-{n}/gate/`.
The receipt holds the argv hash, working directory, boundary, timestamps, classification (`succeeded`, `failed`, `timeout`, `cancelled`, `spawn_failed`), exit code, and output hashes.
