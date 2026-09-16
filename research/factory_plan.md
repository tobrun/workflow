# Factory: an end-to-end software factory over the development skills

Date: 2026-09-15

## Executive decision

Build `factory` as a new plugin beside `dev`. It owns copied, intentionally divergent versions of `scope`, `scope-review`, `build`, and `ship`, plus a small Python runner that turns those skills into one durable workflow:

```text
human + Claude Code
        |
        v
      scope
        |
        v
scope-review -> build -> ship -> ready pull request
     Codex       Codex    Codex
      sol         luna     luna
      low         mid      high
```

The workflow has one interactive boundary: scope. After the human confirms the settled spec, every later stage runs unattended in a fresh process. Artifacts on disk are the interface between stages. Deterministic gates decide whether a stage is complete, regardless of what the model says in chat.

Each run gets its own branch, git worktree, state directory, logs, reports, scratch space, and detached worker. Up to 30 runs may exist at once, while a configurable semaphore limits expensive headless stages to four concurrent processes by default.

Use a stdlib-only Python runner for the first version. LangGraph does not solve the current hard problems, which are process supervision, worktree isolation, deterministic gates, durable local state, and a clean operational UX. Keep each stage behind a small object interface so a different scheduler can drive the same stages later. Reconsider a workflow framework only when the product needs multi-machine scheduling, multi-day remote durability, distributed leases, or a hosted control plane.

## Amendments after implementation

These changes were made after the plan was written; the implementation and `factory/README.md` follow them where they differ from sections below.

- **Decision policy.** Unattended stages take the recommended option for every decision and record it as auto-decided; a run parks only for a fixed list (invalidated premise or new effort, secrets or missing credentials, destructive actions, no safe launch command). `factory/references/factory-run.md` owns the policy and `validate.sh` F03 enforces the wording.
- **Plans are never committed.** The runner excludes `.dev/` through the common Git directory's `info/exclude`; scope and scope-review commits carry only `docs/` changes, and `factory gc` archives `.dev/{plan}/` before removing the worktree.
- **Request sources.** `factory new` accepts a string, `--file PATH`, or `--jira KEY` (fetched through `acli`); every source is written to `runs/{id}/request.md` and named by `request_file` and `source` in the run file.
- **Attached CLI with controls.** This replaces the non-goal of not streaming activity: `factory new`, `scope`, `retry`, and `resume` stay attached after the handoff with a live assembly-line view and keys to pause, note, cancel, retry, resume, stream Codex activity, and open the dashboard; `--detach` and `factory watch <id>` cover the other cases. The worker stays detached and the only writer of `run.json`; controls aimed at a live worker are intent files (`cancel`, `pause`, `note`).
- **Paused status.** `queued -> paused` (worker exits before the next attempt), `paused -> queued` through `factory resume`, `paused -> cancelled`; `factory pause` and `factory note` are new commands.
- **Build gate baseline.** "Commits after `base_sha`" became commits after the build stage's first attempt started, because scope commits already follow `base_sha`.
- **Result files.** A malformed result file fails the gate; a non-zero host exit with a passing gate is `done` with a warning.
- **Operator retries.** `factory retry` and `factory resume` of an orphaned attempt consume a retry like any additional attempt; an exhausted budget needs `--reset-budget`.
- **GitHub token.** Every Codex attempt receives the host's `gh auth token` as `GH_TOKEN`, because the sandbox blocks the macOS keychain.
- **Token ceiling.** `max_tokens_per_run` defaults to 60M after the first real run used 31.7M.

## Product promise

One command should turn a change request into a pull request:

```bash
factory new ~/ws/project \
  "Add idempotency protection to webhook processing"
```

The operator collaborates on scope, approves the handoff, and can then leave. The factory:

1. reviews and refines the spec;
2. implements every change set;
3. proves the requested behavior;
4. hardens and reviews the result;
5. opens a pull request with evidence;
6. retries recoverable failures up to five times across the run;
7. parks the run with a precise reason when automation cannot safely continue.

The dashboard and CLI report outcomes, blockers, and required human action. They do not try to imitate a terminal multiplexer or stream agent activity.

## Goals

- Preserve the quality contracts of the existing development skills.
- Keep `dev` unchanged while `factory` evolves independently.
- Make scope interactive and every later stage unattended.
- Give every stage a deterministic, machine-evaluated exit predicate.
- Retry from durable artifacts in a fresh process, never by resuming a failed headless agent.
- Isolate each run with its own worktree and branch.
- Support 30 active runs on one machine without running 30 expensive model processes at once.
- End successful runs with exactly one ready pull request for the run branch.
- Make interrupted and blocked runs inspectable, resumable, and safe to clean up.
- Preserve human authorship. Do not add model co-authors. The PR author is the person whose authenticated CLI launched the factory.

## Non-goals for version 1

- A distributed scheduler or hosted service.
- Running one workflow across multiple machines.
- Exactly-once execution across machine loss.
- A graphical live terminal for every model process.
- Automatic merging.
- Automatic deletion of unmerged work.
- Updating the existing `dev` skill behavior.
- Shipping `factory` through Pi. Pi uses a flat skill namespace, so its `scope`, `build`, and `ship` names would collide with `dev`.
- A general workflow language. The runner supports this fixed four-stage pipeline first.

## Design principles

1. **Fresh context per attempt.** Each headless attempt starts a new process with a short prompt. The worktree and run artifacts carry context.
2. **Artifacts are the API.** Skills communicate through `spec.md`, review files, implementation notes, reports, result files, git commits, and the pull request.
3. **Gates decide completion.** Model prose is advisory. A deterministic gate produces the authoritative outcome.
4. **Retries re-enter from disk.** A retry receives the previous reason and reads existing artifacts. It does not resume a dead headless session.
5. **One writer owns run state.** The detached worker is the only writer to `run.json` while holding `worker.lock`. CLI commands record intent or take the lock when no worker is alive.
6. **The runner owns workflow-level Git operations.** It creates the branch and worktree, commits scope and scope-review artifacts, detects push state, and performs cleanup. The copied `build` and `ship` skills retain their existing change-set, hardening, push, and PR responsibilities inside the isolated branch.
7. **Concurrency is bounded at the expensive edge.** Thirty runs may be active, but only the configured number of headless stages may hold execution slots.
8. **Questions become typed outcomes.** In unattended stages, a question for a human becomes a result with an honest reason and retryability. It never waits on stdin.
9. **Operational state is reconstructable.** `run.json`, `events.jsonl`, attempt directories, git, and GitHub are enough to explain every visible status.
10. **Cleanup is conservative.** The factory automatically removes only merged, completed runs. Everything else requires an explicit force command.

## Repository layout

```text
factory/
  .claude-plugin/
    plugin.json
  README.md
  bin/
    factory
  skills/
    scope/
    scope-review/
    build/
    ship/
  references/
    factory-run.md
    ...copied shared references...
  scripts/
    architecture-check.py
    skill-metrics.py
  evals/
    README.md
    scope.json
    scope-review.json
    build.json
    ship.json
  runner/
    __init__.py
    __main__.py
    cli.py
    config.py
    dashboard.py
    events.py
    gates.py
    hosts.py
    model.py
    notify.py
    pipeline.py
    slots.py
    supervise.py
    worker.py
    worktree.py
    tests/
      stubs/
      test_cli.py
      test_dashboard.py
      test_gates.py
      test_hosts.py
      test_model.py
      test_slots.py
      test_worker.py
      test_worktree.py

plugins/
  dev/
  factory/

scripts/
  build_codex_plugin.py
  test_factory_runner.sh
  validate.sh
```

The generated `plugins/factory/` distribution contains only:

```text
.codex-plugin/
skills/
references/
scripts/
README.md
```

It never contains `runner/`, `bin/`, or `evals/`. The runner is a local operational tool, while the generated Codex plugin contains the skill material that `codex exec` resolves.

Only the four pipeline skills are copied from `dev`. Their internal links must remain within the copied set or shared factory references. In particular:

- `scope-review` may link to `ship/references/orchestration.md`;
- the ship gauntlet may link to `build/SKILL.md`;
- no factory skill may require `commit`, `to-pitch`, or `to-quiz`.

## Runtime layout

The default runtime root is `~/.factory`. Tests override it with `FACTORY_HOME`.

```text
~/.factory/
  config.json
  dashboard.html
  dashboard.lock
  slots/
    0.lock
    1.lock
    2.lock
    3.lock
  runs/
    {run-id}/
      run.json
      events.jsonl
      worker.log
      worker.pid
      worker.heartbeat
      worker.lock
      cancel
      worktree/
      reports/
      evidence/
      scratch/
        {stage}-{attempt}/
      attempts/
        {stage}-{attempt}/
          prompt.txt
          stdout.jsonl
          stderr.log
          last-message.md
          gate.json
  archive/
    {run-id}/
      run.json
      events.jsonl
      worker.log
      reports/
      evidence/
      attempts/
```

`run-id` is `{YYYYMMDD-HHMM}-{plan}`. If it collides, append `-2`, `-3`, and so on.

The runner writes these paths into the worktree's private Git excludes:

```text
.dev/factory-run.json
.dev/*/*-result.json
.dev/.metrics/
```

The patterns go in the target repository's common Git directory under `info/exclude`, resolved with `git rev-parse --git-common-dir`. This matters for linked worktrees, where assuming `<repo>/.git` is a directory is incorrect.

## Configuration

`~/.factory/config.json` is optional. Missing values use defaults.

```json
{
  "schema": 1,
  "max_concurrent_stages": 4,
  "notify": true,
  "max_tokens_per_run": 20000000,
  "stop_on_repeated_reason": false,
  "stage_poll_seconds": 10,
  "heartbeat_seconds": 30,
  "repos": {
    "/absolute/path/to/repo": {
      "codex_sandbox": "workspace-write"
    }
  }
}
```

Rules:

- `max_concurrent_stages` must be at least 1.
- `codex_sandbox` is `workspace-write` by default and may be set to `bypass` per repository.
- Repository keys are normalized absolute paths.
- Unknown keys are retained when rewriting config and ignored by this version.
- Invalid JSON or invalid known values fail preflight. The runner never silently resets config.
- Model, effort, and timeout defaults live in `pipeline.py`, not in user config for version 1. This keeps the tested pipeline reproducible. Configurability can be added after the first real runs.

## Pipeline definition

`pipeline.py` exposes one `Stage` object per entry:

```python
@dataclass(frozen=True)
class Stage:
    name: str
    host: str
    model: str
    effort: str
    timeout_s: int | None
    slot_limited: bool
    build_prompt: Callable[[Run, int], str]
    gate: Callable[[Run, Path], GateResult]
```

Initial stage defaults:

| Stage | Host | Model | Effort | Interactive | Timeout | Uses slot |
| --- | --- | --- | --- | --- | --- | --- |
| `scope` | Claude Code | `fable` | `medium` | yes | none | no |
| `scope-review` | Codex | `openai.gpt-5.6-sol` | `low` | no | 45 minutes | yes |
| `build` | Codex | `openai.gpt-5.6-luna` | `medium` | no | 3 hours | yes |
| `ship` | Codex | `openai.gpt-5.6-luna` | `high` | no | 3 hours | yes |

The model identifiers are defaults validated by `factory doctor`. If a host no longer recognizes one, preflight reports the exact failing command before a run is created.

## Workflow lifecycle

### Statuses

Run status is one of:

- `new`: durable run state exists, but the worktree is not yet ready;
- `scoping`: the worktree exists and interactive scope is current;
- `queued`: waiting for a headless execution slot;
- `running`: a headless stage owns a slot and has an active attempt;
- `needs-human`: automation cannot safely continue without a decision or environmental repair;
- `cancelled`: the operator cancelled the run or the shared retry budget was exhausted;
- `done`: ship passed its gate and the ready PR exists.

Archived runs retain status `done`; archive location is storage state, not a workflow status.

Attempt outcome is one of:

- `done`: the stage gate passed;
- `blocked`: the process completed, but the stage is not complete;
- `failed`: the process exited unsuccessfully or produced an invalid execution result;
- `timeout`: the runner terminated it after the stage deadline;
- `cancelled`: the run cancellation interrupted it.

Outcome source is one of `gate`, `skill`, or `runner`.

### Transition table

| From | Event | To | Required side effects |
| --- | --- | --- | --- |
| `new` | worktree created | `scoping` | write run context, launch or await scope |
| `scoping` | scope gate passes and operator confirms | `queued` at `scope-review` | commit scope artifacts, spawn worker |
| `scoping` | scope gate fails | `scoping` | print exact gate failures; retain session id |
| `queued` | execution slot acquired | `running` | record slot and start attempt |
| `running` | `scope-review` gate passes | `queued` at `build` | commit reviewed scope artifacts |
| `running` | `build` gate passes | `queued` at `ship` | preserve build commits and reports |
| `running` | `ship` gate passes | `done` | record PR and notify |
| `running` | retryable `blocked`, `failed`, or `timeout`, budget remains | `queued` at same stage | consume one retry and increment attempt |
| `running` | retryable outcome, budget exhausted | `cancelled` | preserve draft PR and notify |
| `running` | non-retryable outcome | `needs-human` | preserve artifacts and notify |
| any non-terminal | cancel flag observed | `cancelled` | terminate child process group and notify |
| `needs-human` | `factory retry` | `queued` at same stage | increment attempt; optionally reset budget |
| `cancelled` | `factory retry` | `queued` at same stage | clear cancel flag; increment attempt |
| run with dead worker | `factory resume` | current status or `queued` | reconcile orphan attempt, then spawn worker |
| `done` | `factory gc` confirms PR merged | `done` in archive | remove worktree and branch; archive state |

Invalid transitions raise a typed error and do not modify `run.json`.

### Retry semantics

The run has five paid retries shared by `scope-review`, `build`, and `ship`.

- Attempt 1 of each headless stage is free.
- Every additional headless attempt consumes one retry before launch.
- The retry counter is global across the run.
- Interactive scope relaunches do not consume this budget because a human is still in the loop and no unattended retry occurred.
- `factory retry --reset-budget` resets `retries.used` to zero before queueing one new attempt.
- A retry always launches a fresh process.
- A timeout and an orphaned live attempt count as retryable failures unless cancellation caused them.
- Gate exit 2 means a required precondition artifact is absent and is non-retryable.
- Missing binaries, an invalidated premise, and inability to determine a safe launch command are non-retryable.
- `stop_on_repeated_reason`, when enabled, compares normalized reason hashes. Two consecutive retryable blocks at the same stage with the same reason park the run in `needs-human` before the full budget is spent.
- `max_tokens_per_run` is evaluated after each attempt. Exceeding it parks the run in `needs-human` before another stage or retry starts.

With all stages passing first try, a run has four stage attempts total, one interactive and three headless. With every retry spent, it has at most eight headless attempts across the three unattended stages.

## Durable run model

`run.json` uses schema 1:

```json
{
  "schema": 1,
  "id": "20260915-1420-webhook-idempotency",
  "repo": "/abs/path/to/project",
  "remote": "origin",
  "base": "main",
  "base_sha": "abc123",
  "branch": "factory/webhook-idempotency",
  "plan": "webhook-idempotency",
  "request": "Add idempotency protection to webhook processing",
  "created_at": "2026-09-15T14:20:00+02:00",
  "updated_at": "2026-09-15T14:20:00+02:00",
  "status": "queued",
  "stage": "scope-review",
  "retries": {
    "used": 0,
    "budget": 5
  },
  "scope_session_id": "uuid",
  "stages": {
    "scope": {
      "outcome": "done",
      "attempts": 1,
      "finished_at": "..."
    }
  },
  "attempts": [],
  "artifacts": {
    "spec": ".dev/webhook-idempotency/spec.md",
    "spec_review": null,
    "notes": null,
    "e2e_report": null,
    "review": null,
    "pr_body": null
  },
  "pr": {
    "number": null,
    "url": null,
    "draft": null,
    "checks": null,
    "merged": null
  },
  "human": {
    "reason": null,
    "since": null,
    "note": null
  },
  "tokens_total": 0
}
```

Each attempt records:

```json
{
  "stage": "build",
  "n": 2,
  "host": "codex",
  "model": "openai.gpt-5.6-luna",
  "effort": "medium",
  "started_at": "...",
  "ended_at": "...",
  "exit_code": 0,
  "session_id": "thread-id",
  "outcome": "blocked",
  "reason": "Validation command failed: npm test",
  "reason_hash": "sha256:...",
  "retryable": true,
  "source": "gate",
  "tokens": 128400,
  "seconds": 1820
}
```

Persistence rules:

- Save atomically with a temporary file in the run directory, `fsync`, then `os.replace`.
- Append events as one JSON object per line and flush after each transition.
- Use timezone-aware ISO 8601 timestamps in UTC internally. The CLI renders local time.
- Never infer state by parsing `worker.log`.
- Store repository and run paths as absolute paths.
- Store artifact paths relative to the worktree or run directory where practical.
- Reject a newer schema with a clear upgrade error.

## Event log

`events.jsonl` entries have:

```json
{
  "ts": "2026-09-15T12:20:00Z",
  "run": "20260915-1420-webhook-idempotency",
  "event": "stage.finished",
  "stage": "build",
  "attempt": 2,
  "data": {}
}
```

Required event names:

- `run.created`
- `worktree.created`
- `run.scoping`
- `run.queued`
- `slot.acquired`
- `stage.started`
- `process.spawned`
- `process.exited`
- `gate.evaluated`
- `stage.finished`
- `retry.scheduled`
- `slot.released`
- `run.needs_human`
- `run.cancel_requested`
- `run.cancelled`
- `run.resumed`
- `run.done`
- `notify.sent`
- `notify.failed`
- `gc.archived`

Events explain history; `run.json` is the current-state projection.

## Per-attempt run context

Before every attempt, the runner writes `.dev/factory-run.json` in the worktree:

```json
{
  "schema": 1,
  "run_id": "20260915-1420-webhook-idempotency",
  "run_dir": "/home/user/.factory/runs/...",
  "plan": "webhook-idempotency",
  "stage": "build",
  "attempt": 2,
  "previous": {
    "outcome": "blocked",
    "reason": "Validation command failed: npm test"
  },
  "operator_note": "The flaky test was fixed upstream",
  "report_dir": "/home/user/.factory/runs/.../reports",
  "evidence_dir": "/home/user/.factory/runs/.../evidence",
  "scratch_dir": "/home/user/.factory/runs/.../scratch/build-2",
  "branch": "factory/webhook-idempotency",
  "base": "main",
  "base_sha": "abc123",
  "interactive": false
}
```

The file is rewritten atomically before launch and excluded from Git. Skills must read it before applying fallback branch or `/tmp` heuristics.

## Stage result contract

Each copied factory skill writes `.dev/{plan}/{stage}-result.json` as its last action:

```json
{
  "schema": 1,
  "stage": "build",
  "status": "done",
  "reason": "All change sets and validation commands passed",
  "retryable": true,
  "artifacts": [
    ".dev/webhook-idempotency/implementation-notes.md"
  ],
  "next": "ship",
  "counts": {
    "change_sets": 4,
    "scenarios": 17
  },
  "human_calls": [
    {
      "kind": "decision",
      "summary": "..."
    }
  ]
}
```

Status is `done`, `blocked`, or `failed`. Result files are excluded from Git because they are runtime protocol, not project artifacts.

The merge rule is deterministic:

| Skill result | Gate result | Authoritative outcome |
| --- | --- | --- |
| `done` | pass | `done` |
| `done` | fail | gate outcome and reason |
| `blocked` or `failed` | pass | `done`, plus warning event |
| missing | pass | `done`, plus warning event |
| missing or malformed | fail | gate outcome and reason |

Retryability comes from the skill result when present. Otherwise it defaults to true, except gate exit 2 and runner preflight failures, which are non-retryable.

## Prompts and process launch

Every unattended prompt stays short:

```text
$factory:{stage}

Factory run {run-id}. Read .dev/factory-run.json first. It names the plan
directory, report directory, evidence directory, and scratch directory.
No human is available in this session. This is attempt {n} of the {stage}
stage.

{If retrying: Attempt {n-1} ended {outcome}: {reason}.}
{If supplied: Operator note: {note}.}

Start from the artifacts on disk. Do not redo finished work.
Write .dev/{plan}/{stage}-result.json as your last action.
```

If `$factory:{stage}` does not resolve in a real Codex dry run, prepend:

```text
Follow the skill at {absolute-path-to-generated-skill}/SKILL.md.
```

That fallback is explicit and tested rather than discovered silently during a production run.

### Codex launch

`hosts.codex_argv()` produces:

```text
codex exec "<prompt>"
  -m <model>
  -c model_reasoning_effort="<effort>"
  -c approval_policy="never"
  -s workspace-write
  -c sandbox_workspace_write.network_access=true
  -c shell_environment_policy.inherit="all"
  --add-dir <git-common-dir>
  --add-dir <run-dir>
  -C <worktree>
  --skip-git-repo-check
  --json
  -o <attempt-dir>/last-message.md
```

For a repository configured with `codex_sandbox: "bypass"`, replace sandbox and approval flags with:

```text
--dangerously-bypass-approvals-and-sandbox
```

Spawn contract:

- `stdin=subprocess.DEVNULL`
- `start_new_session=True`
- stdout to `stdout.jsonl`
- stderr to `stderr.log`
- working directory is the run worktree
- inherited environment plus `FACTORY_RUN_DIR`, `FACTORY_PLAN`, `FACTORY_STAGE`, and `FACTORY_ATTEMPT`

`parse_codex_stream`:

- captures the thread id;
- sums usage from `turn.completed`;
- records `turn.failed`;
- ignores unknown event types without failing;
- tolerates a truncated final line after forced termination;
- never treats the last assistant message as the stage gate.

### Claude launch

Interactive scope runs in the foreground:

```text
claude "/factory:scope {request}"
  --model fable
  --effort medium
  --plugin-dir <factory-source-root>
  --add-dir <run-dir>
  --session-id <uuid>
```

The runner resolves `<factory-source-root>` from the real path of
`factory/bin/factory`, so the command still works when the CLI is reached
through a symlink. Claude's working directory is the run worktree.
`factory scope <id> --resume` adds the host's resume argument for the stored session. A fresh relaunch creates and stores a new session id.

### Timeouts and cancellation

For headless stages:

1. send `SIGTERM` to the child process group;
2. wait up to 30 seconds while continuing heartbeat updates;
3. send `SIGKILL` to the process group if still alive;
4. record the final process status and gate whatever durable artifacts exist.

User cancellation wins over timeout classification if the cancel flag existed before termination began.

## Stage contracts

### 1. Scope

`factory new`:

1. validates the repository, host binaries, auth, config, base branch, runtime directories, and plugin visibility;
2. derives a plan slug from the request unless `--plan` is supplied;
3. creates run state in `new`;
4. fetches the remote and creates an isolated worktree;
5. writes `.dev/factory-run.json`;
6. launches interactive Claude Code in the worktree;
7. evaluates the scope gate;
8. if the gate passes, asks `Hand this run to the factory? [Y/n]`, unless `--yes`;
9. commits the scope output as the authenticated user;
10. moves the run to `queued` at `scope-review`;
11. spawns the detached worker;
12. prints the same summary as `factory show`.

Scope gate:

- `.dev/{plan}/spec.md` exists;
- `lint-spec.py` exits 0;
- the spec contains zero `⚑` marks;
- the spec contains zero `[open]` markers;
- `scope-result.json`, when present, is valid and says `done`.

Gate failure leaves the run in `scoping` and prints each failing condition. `factory scope <id>` relaunches it. Scope has no timeout and consumes no headless slot or retry.

Commit:

```text
docs(scope): spec for {plan}
```

No co-author trailer is added. If there is nothing to commit because the exact artifact is already committed, record that fact and continue.

### 2. Scope review

Baseline before launch is the highest existing `spec-review_N.md` index.

Gate:

1. a new `spec-review_N.md` with a higher index exists, otherwise `failed`;
2. `lint-spec.py` passes;
3. the report contains a parseable `Verdict:` line;
4. `APPROVED` is `done`;
5. `APPROVED WITH DEFERRALS` inspects every `## Deferred` item;
6. every deferred item must contain `Kind: premise | new-effort | unanswered`;
7. `premise` and `new-effort` are blocked and non-retryable;
8. `unanswered` is blocked and retryable.

On success the runner commits spec, ledger, contract, and review refinements:

```text
docs(scope-review): refine spec for {plan}
```

The runner commits only paths allowed by the stage contract. Unexpected modified paths make the gate fail with a path list.

### 3. Build

The build gate reports the first failing check in this order:

1. the repository has commits after `base_sha`;
2. the tracked worktree is clean;
3. `check-tests.py .dev/{plan}` exits 0;
4. exit 2 from `check-tests.py` is non-retryable;
5. when the spec contains `[e2e]` scenarios, `{report_dir}/{plan}-e2e-report.html` exists;
6. the e2e report parses through `load_e2e_data`;
7. `summary.failed == 0`;
8. `summary.total` is at least the count of `[e2e]` scenarios in the spec;
9. every command in the spec's `### Validation` block exits 0, each with a 20 minute timeout;
10. `build-result.json`, when present, is valid and compatible with the checks above.

Validation commands run without a shell when they parse as a simple argv. Commands that require shell syntax run through the repository's configured shell and are logged verbatim. The plan parser must preserve command order and code-block contents.

Build is slot-limited and has a three-hour attempt timeout.

### 4. Ship

The ship gate reports the first failing check in this order:

1. `gh pr list --head <branch> --state open --json ...` returns exactly one PR;
2. `.dev/{plan}/pr.md` exists;
3. `pr-evidence.py check` passes;
4. the highest `review_N.md` has a parseable verdict;
5. `PASS` or `CONCERNS` may continue;
6. `BLOCK` is blocked and retryable, with blocker titles in the reason;
7. a blocked PR must remain draft;
8. the PR is ready, not draft, for a passing verdict;
9. `gh pr checks` contains no failing check;
10. pending checks are recorded and polled within the stage timeout;
11. the tracked worktree is clean;
12. the branch has no unpushed commits;
13. `ship-result.json`, when present, is valid and compatible with GitHub state.

On success, record the PR number, URL, draft state, checks, and merge state, mark the run `done`, regenerate the dashboard, and notify with the URL.

Ship is slot-limited and has a three-hour attempt timeout.

## Worktree and branch lifecycle

`worktree.py`:

1. resolves the repository with `git -C <repo> rev-parse --show-toplevel`;
2. requires a configured remote, defaulting to `origin`;
3. fetches the selected remote;
4. detects the default branch from `refs/remotes/<remote>/HEAD`, then falls back to the repository's current default;
5. accepts `--base` as an explicit override;
6. records the immutable `base_sha`;
7. creates branch `factory/{plan}`, suffixing `-2`, `-3`, and so on on collision;
8. creates the worktree at `<run-dir>/worktree`;
9. installs private exclude patterns through the common Git directory;
10. never checks out another branch inside that worktree.

Creation must be rollback-safe. If any step after run creation fails, keep `run.json` with `needs-human` and the exact repair command. Remove only an empty or provably unregistered partial worktree.

`factory gc` archives only runs that are `done` and whose PR is confirmed merged:

1. acquire the dead worker lock;
2. run `git worktree remove --force <worktree>`;
3. delete the local factory branch;
4. run `git worktree prune`;
5. copy or move retained runtime artifacts into `archive/{run-id}`;
6. omit the worktree;
7. append `gc.archived`.

No unmerged run is removed automatically.

`factory rm <id> --force` removes a selected run regardless of PR state after resolving the exact run id and worktree path. It never accepts a path or glob as the target.

## Concurrency and supervision

### One worker per run

After scope handoff:

```python
subprocess.Popen(
    [sys.executable, "-m", "runner", "_worker", run_id],
    cwd="/",
    stdin=subprocess.DEVNULL,
    stdout=worker_log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
    env=worker_env,
)
```

`worker_env` contains an absolute `PYTHONPATH` for the factory source root, so
the detached process does not depend on the caller's working directory.

The worker:

- acquires an exclusive non-blocking `flock` on `worker.lock`;
- writes its PID atomically;
- updates `worker.heartbeat` every 30 seconds during slot waits and process supervision;
- owns all state transitions while alive;
- exits at `done`, `needs-human`, or `cancelled`;
- releases the stage slot in a `finally` block;
- releases `worker.lock` automatically on process death.

`factory ls` determines liveness by attempting the lock. PID existence is supporting information only because PIDs are reusable.

### Stage slots

`slots.py` implements a cross-process semaphore with one `flock` file per slot:

```text
~/.factory/slots/0.lock
...
~/.factory/slots/{max-1}.lock
```

Workers scan slots in a run-id-derived rotating order to avoid always favoring slot 0. If no slot is free, the run remains `queued`, updates its heartbeat, checks cancellation, sleeps for ten seconds plus small jitter, and retries.

Changing `max_concurrent_stages` affects new acquisitions. Existing holders finish normally. Extra lock files are harmless and may be pruned only when unlocked.

Thirty active runs therefore means at most:

- 30 small detached Python workers;
- 4 headless model processes by default;
- 30 isolated worktrees;
- one interactive scope process per operator terminal currently scoping.

### Orphan recovery

`factory resume <id>`:

1. checks whether `worker.lock` is available;
2. refuses if a worker is alive;
3. inspects the last attempt;
4. if state says `running`, records that attempt as orphaned `failed`, retryable, and consumes a retry;
5. evaluates cancellation and budget limits;
6. returns the run to `queued`;
7. spawns a new worker.

If the host process still exists but the lock is available, it is not considered the factory worker. The CLI reports the PID mismatch for diagnosis and does not kill an unrelated process.

## CLI and UX

`factory/bin/factory` resolves symlinks to its own plugin root, sets
`PYTHONPATH` to the absolute `factory/` directory, and execs:

```text
python3 -m runner "$@"
```

Every command prints the outcome first, then evidence and next action.

### Commands

| Command | Behavior |
| --- | --- |
| `factory new <repo> "<request>" [--plan slug] [--base ref] [--yes]` | Preflight, create run and worktree, run interactive scope, gate it, confirm handoff, and spawn the worker. |
| `factory scope <id> [--resume]` | Relaunch interactive scope, gate it, and offer handoff again. |
| `factory ls [--all] [--json]` | Show actionable runs first. Hide `done` runs unless `--all`. |
| `factory show <id> [--json]` | Show status, stage, retries, blocker, attempts, artifacts, PR, worktree, and exact next command. |
| `factory logs <id> [--stage name] [--attempt n] [-f] [--raw]` | Render Codex events compactly or stream raw JSONL. |
| `factory retry <id> [--reset-budget] [--note text]` | Queue a new attempt for a parked run. |
| `factory resume <id>` | Recover a run whose worker died. |
| `factory cancel <id>` | Write cancellation intent and let the worker terminate its child. |
| `factory gc [--dry-run]` | Archive merged completed runs. |
| `factory rm <id> --force` | Explicitly remove one run and worktree. |
| `factory dashboard [--open]` | Regenerate the static board and optionally open it. |
| `factory doctor [--json]` | Check binaries, auth, plugin visibility, config, slots, runtime permissions, and worktree health. |

Run id lookup accepts an exact id or an unambiguous prefix. Ambiguous prefixes list matches and make no change.

### `factory ls`

Default ordering:

1. `needs-human`;
2. `cancelled`;
3. stale or dead-worker runs;
4. `running`;
5. `queued`;
6. `scoping`.

Example:

```text
! 20260915-1420-webhook-idempotency  build  cancelled  retries 5/5
  npm test still fails in webhook_retry_test.py  PR #41 draft  2h10m

> factory retry 20260915-1420-webhook-idempotency --note "..."
```

### `factory show`

Show:

- outcome sentence;
- run id, repository, branch, worktree, base SHA;
- stage and status;
- retry use and token total;
- current blocker or operator note;
- worker liveness and heartbeat age;
- PR link, draft state, checks, and merge state;
- per-stage attempts with outcome, reason, duration, model, effort, and tokens;
- artifact paths;
- exact next command.

### Logs

Default log rendering recognizes:

- agent messages;
- command start and completion;
- command exit codes;
- model errors;
- token usage;
- process termination.

Unknown Codex event types are shown only in `--raw`. `-f` follows the current attempt, switches automatically when a retry starts, and exits when the run becomes terminal.

## Dashboard

The dashboard is a self-contained static file at `~/.factory/dashboard.html`.

It follows the repository's template convention:

```text
<!-- FACTORY_DATA_START -->
<script id="factory-data" type="application/json">...</script>
<!-- FACTORY_DATA_END -->
```

The renderer below the data block is stable. Regeneration replaces only the data block when possible.

Three groups:

1. **Needs you:** `needs-human`, `cancelled`, dead worker, stale heartbeat.
2. **In flight:** `scoping`, `queued`, `running`.
3. **Done:** completed runs, newest first.

Columns:

- Run
- Repository
- Stage
- Status
- Retries
- Blocker
- Pull request
- Updated
- Duration

The page refreshes every 30 seconds with a meta refresh. It has no activity feed. Attempt details remain in `factory show` and `factory logs`.

Every state transition regenerates the dashboard under `dashboard.lock`. Failure to render is logged and never fails the run.

## Notifications

When enabled, use:

```text
osascript -e 'display notification ...'
```

Notify on:

- `needs-human`, with stage and reason;
- `cancelled`, with cancellation or retry exhaustion reason;
- `done`, with PR URL.

Escape notification text without shell interpolation. Notification errors append `notify.failed` and never change workflow status.

## Factory skill changes

All four copied `SKILL.md` files gain a concise `## Factory context` section:

- read `.dev/factory-run.json` when present;
- use its `plan` instead of branch heuristics;
- use `report_dir`, `evidence_dir`, and `scratch_dir`;
- when `interactive` is false, no human is available;
- turn an unresolved human question into a typed blocked result;
- write `.dev/{plan}/{stage}-result.json` as the last action;
- never invoke the next pipeline skill.

Keep each `SKILL.md` near the repository's 150-line guidance by moving protocol detail into `factory/references/factory-run.md`.

### Scope copy

- Use the run file's fixed plan name.
- Keep the interview interactive.
- A question the operator cannot answer resolves to a `⊘` line with a reopening condition, because the scope gate rejects `⚑` and `[open]`.
- Render reports under `report_dir`.
- Recommend handoff in prose if useful, but never invoke `scope-review`. The runner performs the handoff.
- Write `scope-result.json` last.

### Scope-review copy

When no interactive channel exists:

- apply the panel's recommendation when repo evidence and settled intent make one defensible;
- record `Answered: factory policy (auto-decided) - {recommendation}`;
- defer only premise-invalidating, genuinely new-effort, or still-unanswerable findings;
- add `Kind: premise | new-effort | unanswered` to each deferred item;
- use the run file's scratch root;
- write `scope-review-result.json` last.

### Build copy

Convert its three human ask points into outcomes:

- no spec: blocked, non-retryable;
- no safe launch command after documented discovery attempts: blocked, non-retryable, naming what was tried;
- three e2e failures with the same root cause: log the deviation, leave the scenario failed, and return blocked, retryable.

Also:

- use run-provided report and scratch paths;
- keep committing completed change sets as the current skill already requires;
- never push or open a PR;
- write `build-result.json` last.

### Ship copy

Update `SKILL.md`, `references/pull-request.md`, `references/orchestration.md`, and gauntlet material:

- remove confirmation prompts for tiny or huge diffs during a factory run;
- convert unsafe or intent-changing cases into typed results;
- use run-provided report, evidence, and scratch paths;
- add one PR body line naming the factory run, stage models, and count of auto-decided escalations;
- preserve the authenticated human as commit and PR author;
- write `ship-result.json` last with `pr_url`, `draft`, and blockers in `human_calls`.

### Shared references

`factory/references/plan-layout.md`:

- the run file is authoritative for plan location;
- branch and commit heuristics apply only when the run file is absent;
- add `factory-run.json` and `{stage}-result.json` to the ownership table.

`factory/references/reporting.md`:

- use `report_dir` from the run file;
- fall back to the existing `/tmp/{project-slug}/reports` convention outside a factory run.

`factory/references/factory-run.md`, new:

- define both JSON contracts;
- require the result file as the final action;
- define non-interactive question handling;
- state that the runner gate is authoritative;
- give one done and one blocked example;
- stay short enough to load in every stage.

`factory/scripts/skill-metrics.py`:

- store state under `repo_root()/.dev/.metrics`;
- avoid collisions among 30 worktrees with the same final directory name;
- leave Codex token display as `n/a`; `run.json` is the cross-stage token authority.

### Evals

Add one factory-specific eval per skill asserting:

- fixed plan name from the run file;
- result file written last;
- unattended questions follow factory policy;
- reports use run-provided paths;
- the skill does not launch the next stage.

## Repository tooling changes

### Marketplaces

`.claude-plugin/marketplace.json` gains:

```json
{
  "name": "factory",
  "source": "./factory",
  "description": "..."
}
```

`.agents/plugins/marketplace.json` gains the generated `./plugins/factory` entry.

### Codex plugin generator

Refactor `scripts/build_codex_plugin.py` around:

```python
PLUGINS = {
    "dev": PluginConfig(...),
    "factory": PluginConfig(...),
}
```

Each config owns:

- source and destination;
- manifest display fields;
- allowed copied directories;
- per-skill UI metadata;
- default prompts and capabilities.

CLI:

```text
python3 scripts/build_codex_plugin.py
python3 scripts/build_codex_plugin.py --check
python3 scripts/build_codex_plugin.py --plugin dev
python3 scripts/build_codex_plugin.py --plugin factory
```

Without `--plugin`, build or check every configured plugin. Build each destination in a temporary sibling and replace it only after the full plugin succeeds.

### Validation

Generalize `scripts/validate.sh`:

- `R15` scans links under every Claude marketplace source directory.
- `C01` compares Claude and Codex marketplace plugin-name sets.
- For each Codex entry, require `./plugins/{name}`, matching manifest name, generated parity, and `$name:{skill}` in each `openai.yaml` default prompt.
- Keep `P01` scoped to `dev`, with a comment explaining the intentional Pi limitation.
- Add `F01`: run `python3 -m unittest discover -s factory/runner/tests -q` and `bash scripts/test_factory_runner.sh`.
- Add `F02`: every factory stage `SKILL.md` mentions `factory-run.json` and its own `{stage}-result.json`.
- Continue rejecting em dashes across the repository.

### Documentation and ignore rules

`README.md`:

- add a `factory` plugin row;
- document Claude and Codex installation;
- document the local CLI symlink;
- state that Pi ships `dev` only.

`CLAUDE.md`:

- expand the structure diagram for multiple plugins, `runner/`, and `bin/`;
- state that the generator builds every configured plugin;
- state that factory skills intentionally diverge from dev skills;
- preserve `disable-model-invocation: true`;
- add the explicit carve-out that `factory/runner/` is the sanctioned invoker, launching each stage as a fresh process and chaining through disk artifacts;
- add runner test commands.

`.gitignore`:

```text
__pycache__/
```

`package.json` stays unchanged.

## Doctor and preflight

`factory doctor` checks:

- Python version and stdlib features required by the runner;
- `git`, `codex`, `claude`, and `gh` binaries, honoring test overrides;
- `codex` and `claude` model visibility where the host offers a non-destructive check;
- Codex plugin marketplace contains `factory@nurbot`;
- Claude can resolve the factory plugin directory;
- `gh auth status`;
- runtime root creation and atomic rename;
- `flock` support;
- config validity;
- slot-file health;
- stale worker locks and heartbeats;
- registered worktrees versus run records;
- writable Git common directories for excludes;
- dashboard renderability.

Doctor prints concrete repair commands and exits nonzero when a new run would be unsafe. It does not modify auth, install plugins, delete worktrees, or rewrite config.

`factory new` runs the subset required for its selected repository and hosts before creating durable state.

## Testing strategy

All tests use the stdlib and local temporary repositories. No network is required.

Binary overrides:

- `FACTORY_CODEX_BIN`
- `FACTORY_CLAUDE_BIN`
- `FACTORY_GH_BIN`
- `FACTORY_HOME`

### Stubs

`factory/runner/tests/stubs/codex`:

- records argv and environment;
- reads a JSON scenario keyed by stage and attempt;
- writes fixture files into the worktree;
- can run scripted Git commands;
- can sleep, exit with a chosen code, omit or corrupt the result file;
- emits `thread.started`, command events, `turn.completed`, and unknown events;
- writes the `-o` last-message file.

`factory/runner/tests/stubs/claude`:

- records argv;
- writes a fixture spec and optional result;
- simulates fresh and resumed session behavior;
- exits with a chosen code.

`factory/runner/tests/stubs/gh`:

- answers `auth status`, `pr list`, `pr view`, `pr checks`, and PR creation or editing from scenario data;
- records mutations;
- supports draft, ready, failing, pending, and merged states.

### Unit and integration tests

`test_model.py`:

- every valid transition;
- every invalid transition;
- shared retry accounting;
- scope retries remain free;
- budget exhaustion;
- token ceiling;
- repeated-reason policy;
- atomic-save recovery.

`test_gates.py`:

- absent and flagged specs;
- `[open]` and `⚑`;
- scope-review verdict and `Kind` variants;
- no new review index;
- `check-tests.py` pass, fail, and exit 2;
- e2e report missing, malformed, failed, and under-counted;
- validation command failure and timeout;
- no PR, multiple PRs, draft, ready, failing checks, pending checks;
- `BLOCK`, `CONCERNS`, and `PASS`;
- dirty tree and unpushed commits;
- skill and gate result merge matrix.

`test_worktree.py`:

- temporary bare origin;
- default branch detection;
- explicit base;
- worktree creation and removal;
- branch suffixing;
- common Git directory excludes;
- rollback after partial failure.

`test_slots.py`:

- `N+1` contenders for `N` slots;
- release after exception;
- cancellation while queued;
- changed slot count;
- no starvation in a bounded contention test.

`test_hosts.py`:

- exact argv builders;
- workspace-write and bypass modes;
- environment contract;
- Codex event parsing and token totals;
- unknown and truncated events;
- process-group timeout classification.

`test_worker.py`:

- full happy path;
- block then pass;
- cross-stage retry budget;
- exhaustion to `cancelled` with draft PR retained;
- non-retryable result to `needs-human`;
- timeout;
- cancel while queued;
- cancel mid-stage;
- token ceiling after an attempt;
- orphan recovery;
- duplicate worker exclusion.

`test_dashboard.py`:

- all three groups;
- actionable ordering;
- HTML escaping;
- data-block replacement;
- lock contention;
- malformed run isolation.

`test_cli.py`:

- exact and prefix run lookup;
- ambiguous id;
- JSON output stability;
- retry note;
- reset budget;
- refusal to resume a live worker;
- dry-run garbage collection.

Every temporary repository sets its own Git user name and email.

### End-to-end runner test

`scripts/test_factory_runner.sh` follows the stub-binary pattern in `scripts/test_pi_runner.sh`.

Scenario 1:

1. create a temporary target repository and bare origin;
2. run `factory new --yes` with a Claude fixture spec;
3. let scope-review pass;
4. let build write commits and a green report;
5. let ship create a ready PR in the GitHub stub;
6. assert run status `done`, retry use 0, event ordering, artifact paths, and PR data.

Scenario 2:

1. create another run;
2. make one headless stage return the same retryable block;
3. allow all five retries;
4. assert six attempts at that stage, status `cancelled`, retries 5/5, precise reason, released slot, retained worktree, and retained draft PR data.

The script runs with no network and cleans only its own temporary directory.

## Milestones

### M0: multi-plugin repository tooling

Deliver:

- generalized plugin generator;
- both marketplace entries;
- generalized validation;
- root documentation and rules;
- factory source skeleton;
- untouched copies of the four selected skills and required references;
- generated Codex factory plugin.

Exit criteria:

- `python3 scripts/build_codex_plugin.py --check` passes for both plugins;
- existing dev output is byte-for-byte unchanged except where generalized metadata intentionally requires a documented change;
- `scripts/validate.sh` passes;
- Pi validation still targets dev only.

### M1: durable core and interactive scope

Deliver:

- config, model, events, worktree, pipeline, and CLI foundations;
- `new`, `scope`, `ls`, `show`, `cancel`, and `doctor`;
- interactive Claude scope launch;
- scope gate and handoff;
- detached worker spawn that stops queued before headless execution.

Exit criteria:

- a temporary repository can be scoped and handed off;
- scope artifacts are committed without co-authorship;
- invalid specs remain in `scoping` with exact failures;
- model, scope gate, and worktree tests pass.

### M2: unattended pipeline

Deliver:

- Codex host integration;
- slots and supervision;
- scope-review, build, and ship gates;
- retry, resume, cancellation, and logs;
- all stubs and remaining runner tests;
- `scripts/test_factory_runner.sh`;
- validation checks `F01` and `F02`.

Exit criteria:

- stubbed happy path reaches a ready PR;
- retry exhaustion is deterministic;
- cancel and timeout release slots and terminate process groups;
- an orphaned run resumes without duplicate workers;
- full runner test suite passes offline.

### M3: factory skill contracts

Deliver:

- factory context in all four skills;
- non-interactive policies;
- stage result files;
- `factory-run.md`;
- report, plan-layout, pull-request, orchestration, and metrics changes;
- factory eval additions;
- regenerated Codex factory plugin.

Exit criteria:

- every skill reads the run file and writes its result;
- no factory skill invokes another stage;
- evals cover factory-specific behavior;
- `F02` and link validation pass.

### M4: operational UX

Deliver:

- dashboard;
- notifications;
- garbage collection;
- force removal;
- stale-worker and stale-heartbeat reporting;
- complete README operator guide.

Exit criteria:

- dashboard remains readable with fixtures for 30 active runs;
- transitions update it atomically;
- notification failure is non-fatal;
- only merged completed runs are automatically archived;
- cleanup tests prove exact-target behavior.

### M5: real dry run

Use a small GitHub-backed repository with one real validation command.

Procedure:

1. run `factory doctor`;
2. set `max_concurrent_stages` to 1;
3. create a small change through real interactive scope;
4. confirm `$factory:scope-review` resolves in `codex exec`;
5. if needed, validate the direct-skill-path fallback;
6. confirm Codex can commit from the worktree with the selected sandbox and Git common-dir access;
7. inspect `factory show`, logs, dashboard, commits, reports, and PR evidence;
8. intentionally break one validation command;
9. observe one retry carrying the prior gate reason;
10. repair or let the next attempt repair it;
11. reach a ready PR;
12. merge it and verify `factory gc --dry-run`, then `factory gc`.

Record host quirks, required setup, observed token use, durations, and any fallback in `factory/README.md`.

Exit criteria:

- one real request reaches a ready PR without human input after scope handoff;
- one real blocked attempt retries from disk and preserves completed work;
- doctor catches every setup issue discovered during the run;
- no manual state-file edits are needed.

## Acceptance criteria

The first version is complete when all of the following are true:

- `dev` behavior is unchanged.
- Claude and Codex marketplaces install both `dev` and `factory`.
- `factory new` creates an isolated branch and worktree and starts interactive scope.
- The handoff starts a detached unattended workflow.
- Every headless attempt runs in a fresh process with no stdin.
- Deterministic gates, not chat text, control all transitions.
- Five shared retries are enforced exactly.
- Non-retryable conditions park in `needs-human`.
- User cancellation and retry exhaustion preserve artifacts and any draft PR.
- Thirty fixture runs can be listed and rendered while only the configured number hold slots.
- A dead worker is detected by lock state and can be resumed safely.
- Successful ship creates exactly one ready PR, records it, and marks the run done.
- No model co-author is added.
- The dashboard prioritizes runs needing action.
- Automatic cleanup touches only merged completed runs.
- Unit, integration, end-to-end runner, generator, and repository validation all pass.
- A real dry run reaches a ready PR without intervention after scope.

## Risks and explicit mitigations

| Risk | Consequence | Mitigation |
| --- | --- | --- |
| Host CLI flags or model names change | New attempts fail before useful work | Centralize argv creation, validate with doctor, keep direct skill-path fallback. |
| A model claims success without producing valid work | Pipeline advances incorrectly | Gate every stage from files, Git, commands, reports, and GitHub state. |
| A model waits for a human in headless mode | Slot is wasted until timeout | `stdin=DEVNULL`, explicit prompt, copied skill policy, typed blocked result, timeout. |
| Thirty runs overload the machine | Poor reliability and high cost | Slot-limit headless stages, keep workers small, expose queued state and token ceiling. |
| Two processes mutate one run | Corrupt state or duplicate PR activity | Exclusive worker lock and one state writer. |
| Process death leaves misleading `running` state | Run appears alive forever | Lock-based liveness, heartbeat age, orphan reconciliation on resume. |
| Worktree Git layout differs from a normal clone | Sandbox or excludes fail | Resolve `--git-common-dir`; test linked worktrees against a bare remote. |
| Retry loops repeat the same mistake | Cost burns without progress | Carry exact prior reason, optional repeated-reason stop, global retry and token budgets. |
| Generated plugin drifts from source | Host behavior differs by installation | Multi-plugin generator, `--check`, marketplace parity validation. |
| Reports or evidence collide across runs | One run overwrites another | Run-specific report, evidence, and scratch directories from the run file. |
| Cleanup deletes useful work | Irrecoverable loss | Auto-archive only merged done runs; exact-id `rm --force` for everything else. |
| Scope-review auto-decides beyond its authority | Intent changes silently | Narrow auto-decision rule, typed deferral kinds, non-retryable premise and new-effort outcomes. |
| Shell validation commands are unsafe or ambiguous | Wrong commands or injection | Parse and preserve spec commands, log verbatim, use argv when possible, isolate in the worktree. |

## Framework revisit triggers

Do not add LangGraph, Temporal, or another orchestration framework because the pipeline has retries. Revisit the runner architecture when one of these becomes a real requirement:

- workers must execute on multiple machines;
- runs must survive loss of the local runtime directory;
- a central service must schedule work for several users;
- stages can wait days for external events;
- distributed leases and idempotent remote activities are required;
- pipeline topology becomes user-defined rather than fixed;
- operational history needs a queryable service rather than local files.

Until then, the local state machine is smaller, easier to test, and closer to the actual failure boundaries.

## Implementation order

Build vertical slices through the runner:

1. one run, one worktree, interactive scope, durable status;
2. one headless stage with a slot, gate, retry, and logs;
3. all three headless stages to a stubbed PR;
4. cancellation, orphan recovery, and budget limits;
5. copied skill contracts;
6. dashboard and cleanup;
7. real dry run.

Avoid implementing every module as a disconnected layer before the first full path works. The earliest valuable proof is:

```text
factory new -> interactive scope -> queued worker
```

The next is:

```text
queued worker -> one Codex attempt -> gate -> retry or next stage
```

The decisive proof is:

```text
real scope handoff -> unattended ready pull request
```
