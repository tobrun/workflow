# factory

An end-to-end software factory over the development skills.
One command turns a change request into a ready pull request:

```bash
factory new ~/ws/project "Add idempotency protection to webhook processing"
factory new ~/ws/project --file docs/requests/webhook-idempotency.md
factory new ~/ws/project --jira PROJ-42
```

The request is a quoted string, a UTF-8 text or Markdown file (up to 256 KB), or a Jira ticket fetched through the authenticated `acli` with its description and latest comments.
Whichever you give, the runner saves it as `request.md` in the run directory, and scope reads it from there.
A Jira run derives its plan and branch from the key and summary, such as `factory/proj-42-deduplicate-webhook-deliveries`, and the PR links back to the ticket.

You collaborate on scope in Claude Code, approve the handoff, and leave.
The handoff seals what you approved: the request, the non-goals, and every acceptance scenario with a stable id; unattended stages may refine the technical plan and add scenarios, but a gate fails any stage that drops or rewords an approved one.
The factory then runs `scope-review`, `build`, and `ship` unattended, each as a fresh Codex process in an isolated worktree, and parks the run with a precise reason when automation cannot safely continue.

```text
human + Claude Code
        |
        v
      scope
        |
        v
scope-review -> build -> ship -> ready pull request
     Codex       Codex    Codex
```

## Watching and controlling a run

After the handoff, `factory new` stays in your terminal and shows the assembly line live until the run finishes:

```text
19:42:10  scope-review attempt 1 done in 14m02s
19:42:10  build queued after scope-review
19:42:11  build attempt 1 started (openai.gpt-5.6-luna/medium)
  ✓ scope        done     3h06m
  ✓ scope-review done     14m02s
  ▶ build        running  attempt 1, 22m15s, 1.8M tokens
  · ship
  retries 0/5, 2.4M tokens, worker alive
  > $ pytest -q tests/unit/test_relevance.py
  keys: p pause  n note  c cancel  v verbose  d dashboard  q detach  ? help
```

Stage transitions, retries, notes, and warnings scroll above a status block that redraws in place: one row per stage, the running attempt's elapsed time and tokens, the latest Codex command or message, and the keys that apply right now.

| Key | Action |
| --- | --- |
| `p` | Pause before the next attempt starts; the running attempt finishes first. Press `p` again to withdraw a pause that has not taken effect. |
| `n` | Leave a note that the next attempt's prompt carries as `Operator note:`. |
| `c` | Cancel after a `[y/N]` confirmation: the running Codex process stops, and the worktree and any draft PR are kept. |
| `r` | Retry a `needs-human` or `cancelled` run (with an optional note), continue a `paused` one, or resume a dead worker. |
| `b` | Retry after resetting the shared retry budget. |
| `v` | Stream every Codex command and message into the history, or turn that off. |
| `d` | Regenerate and open the dashboard. |
| `q`, Ctrl-C | Leave the view; the run keeps going. |
| `?` | Show or hide the key help. |

When the run parks, pauses, or cancels, the view stays open on the blocker so you can act on it; it exits `0` with the PR link when the run is done.
`factory watch <id>` reattaches to any run, and `factory retry` and `factory resume` attach the same way.
`--detach` on `new`, `scope`, `retry`, and `resume` returns right after the handoff instead.

Without a terminal on stdin and stdout, such as in scripts or CI, the view prints plain timestamped lines, takes no keys, and exits `0` when done, `1` when the run parks, pauses, or cancels, and `2` when the worker dies.
Every key has a command for scripts: `factory pause <id> [--undo]`, `factory note <id> "..."`, `factory cancel <id>`, `factory retry <id>`, and `factory resume <id>`.

The work always runs in the detached worker, which stays the only writer of `run.json`.
While it is alive, pause, note, and cancel only leave intent files in the run directory that the worker applies at its next safe point; retry, continue, and resume act only on runs with no live worker.

## What is in this plugin

| Path | Purpose |
| ---- | ------- |
| `skills/` | Factory copies of `scope`, `scope-review`, `build`, and `ship`. They intentionally diverge from `dev`: they read `.dev/factory-run.json`, turn human questions into typed results, and write `.dev/{plan}/{stage}-result.json` last. |
| `references/` | Shared references, including [factory-run.md](references/factory-run.md), the runner protocol. |
| `scripts/` | Shared scripts used by the skills. |
| `runner/` | The stdlib-only Python runner behind the `factory` CLI. Not shipped to Codex. |
| `bin/factory` | CLI entry point. Not shipped to Codex. |
| `evals/` | Factory-specific skill evals. Not shipped to Codex. |

The skills are human-triggered only (`disable-model-invocation: true`).
The runner is the sanctioned invoker: it launches each stage as a fresh process and chains stages through artifacts on disk, and the skills never invoke each other.

## Installation

The factory needs the skills installed in both hosts and the CLI on your `PATH`.

Claude Code:

```bash
/plugin marketplace add tobrun/workflow
/plugin install factory@nurbot
```

Codex:

```bash
codex plugin marketplace add tobrun/workflow
codex plugin add factory@nurbot
```

The checked-in Codex package under `plugins/factory/` is generated from `factory/` by `python3 scripts/build_codex_plugin.py`.

CLI:

```bash
git clone https://github.com/tobrun/workflow ~/ws/workflow
ln -s ~/ws/workflow/factory/bin/factory ~/.local/bin/factory
factory doctor
```

The CLI resolves its own symlink, so it runs from any directory.
It needs Python 3, `git`, `gh` (authenticated), `codex`, and `claude`; `acli` (authenticated) only for `--jira`.

Pi ships `dev` only: Pi uses a flat skill namespace, so the factory's `scope`, `build`, and `ship` would collide with `dev`'s.

## Commands

| Command | Behavior |
| --- | --- |
| `factory new <repo> ("<request>" \| --file PATH \| --jira KEY) [--plan slug] [--base ref] [--remote name] [--yes] [--detach]` | Preflight, create run and worktree, run interactive scope, gate it, confirm handoff, and spawn the worker. |
| `factory scope <id> [--resume] [--yes] [--detach]` | Relaunch interactive scope (resuming the stored Claude session with `--resume`), gate it, and offer handoff again. |
| `factory watch <id>` | Attach to a run: live assembly line plus the control keys; `q` or Ctrl-C detaches. |
| `factory ls [--all] [--json]` | Show actionable runs first. Hide `done` runs unless `--all`. |
| `factory show <id> [--json]` | Show status, stage, retries, blocker, attempts, artifacts, PR, worktree, and exact next command. |
| `factory logs <id> [--stage name] [--attempt n] [-f] [--raw]` | Render Codex events compactly or stream raw JSONL. |
| `factory retry <id> [--reset-budget] [--note text] [--rescope] [--detach]` | Queue a new attempt for a `needs-human` or `cancelled` run and watch it; `--rescope` reopens interactive scope to change the approved intent. |
| `factory intent <id> [--json]` | Show the sealed approved intent (request, non-goals, scenario ids), scenarios added later, and each attempt's decision delta. |
| `factory inputs <id> --stage name [--attempt n] [--file name]` | Show which plan files an attempt started from, how they changed, or print one snapshotted file. |
| `factory resume <id> [--detach]` | Continue a `paused` run, or recover a queued or running run whose worker died, and watch it. |
| `factory pause <id> [--undo]` | Pause the run before its next attempt starts, or withdraw a pause that has not taken effect. |
| `factory note <id> "<text>"` | Leave a note for the next attempt's prompt. |
| `factory cancel <id>` | Write cancellation intent; the attempt's guard stops the agent, and with no live worker the command stops a surviving execution by its verified identity before cancelling. |
| `factory export <id> [--out DIR] [--comment]` | Write a portable evidence bundle (run records, approved intent, receipts, checkpoints, reports, evidence) with a checksummed `manifest.json` to `~/.factory/exports/{id}`; `--comment` links it from the PR by manifest digest. `factory export <bundle> --verify` checks a copy anywhere. |
| `factory outcome <id> [--annotate rework\|rejection\|regression\|intervention] [--note text] [--active-minutes n] [--eval-case path]` | Append the PR's current state (merged, closed-unmerged, open, unknown) or an operator annotation to `outcomes.jsonl`; the recorded completion is never rewritten. |
| `factory report [--json]` | Summarize success, intervention, rework, regressions, failure categories, and usage across runs, each rate with its sample size; active operator time counts only measured annotations. |
| `factory gc [--dry-run]` | Archive merged completed runs, exporting each bundle first; `exports/` is never removed. |
| `factory rm <id> --force` | Explicitly remove one run and worktree; refuses while an execution of the run is still live. |
| `factory dashboard [--open]` | Regenerate the static board and optionally open it. |
| `factory doctor [--json] [-v] [--repo PATH [--run-setup]] [--smoke]` | Check binaries, auth, that Codex loads exactly the generated skills, config, slots, runtime permissions, and worktree health; `--repo` checks a repository's contract, environment names, commands, token forwarding, and sandboxed worktree Git; `--run-setup` runs its setup commands in a throwaway worktree; `--smoke` makes one cached, paid Codex call per model; `-v` also lists passing checks. |

Run ids accept an exact id or an unambiguous prefix.
Every command prints the outcome first, then evidence and the next action.

## Stages

| Stage | Host | Model | Effort | Interactive | Timeout | Uses slot |
| --- | --- | --- | --- | --- | --- | --- |
| `scope` | Claude Code | `fable` | `medium` | yes | none | no |
| `scope-review` | Codex | `openai.gpt-5.6-sol` | `low` | no | 45 minutes | yes |
| `build` | Codex | `openai.gpt-5.6-luna` | `medium` | no | 3 hours | yes |
| `ship` | Codex | `openai.gpt-5.6-luna` | `high` | no | 3 hours | yes |

Deterministic gates decide whether a stage is complete, regardless of what the model says.
A stage's own result file is advisory: when the result and the gate disagree, the gate wins.

## Statuses

| Status | Meaning |
| --- | --- |
| `new` | Run state exists; the worktree is not ready yet. |
| `scoping` | The worktree exists and interactive scope is current. |
| `queued` | Waiting for a headless execution slot. |
| `running` | A headless stage holds a slot and has an active attempt. |
| `paused` | You paused the run; nothing starts until `r` in the view or `factory resume <id>`. |
| `needs-human` | Automation cannot safely continue without a decision or environmental repair. |
| `cancelled` | You cancelled the run, or the shared retry budget ran out. |
| `done` | Ship passed its gate and the ready PR exists. |

## Decisions

After the handoff nobody is asked anything.
Every unattended stage takes the recommended option for each decision, including one that changes a settled spec decision, and records it: `Answered: factory policy (auto-decided)` in the spec review, `auto-decided:` deviations in the implementation notes, and an Auto-decided section in the PR body.
A run parks only for a fixed list, reported as typed conditions (`premise.invalidated`, `scope.new_effort`, `secret.found`, `environment.missing_credentials`, `action.destructive`, `launch.unavailable`): an unresolved condition blocks the stage even when its gate passes, `factory show` prints each one with its evidence and exact next action, and the next attempt must report it resolved with evidence or the runner must observe the fix itself.
[references/factory-run.md](references/factory-run.md) owns the policy, and `scripts/validate.sh` check F03 fails on wording that routes a decision to a person.

## Retries

A run has five paid retries shared by `scope-review`, `build`, and `ship`.
The first attempt of each headless stage is free; every further attempt consumes one retry before it launches.
Interactive scope relaunches are free.
Every retry is a fresh process that reads the previous reason and the artifacts on disk; a failed headless session is never resumed.
Exhausting the budget cancels the run and keeps the worktree and any draft PR.
Non-retryable outcomes, such as an invalidated premise or a missing launch command, park the run in `needs-human`.
`factory retry <id> --note "..."` passes a note to the next attempt.
An operator retry is still an additional attempt, so it consumes a retry too; when the budget is spent, add `--reset-budget` to restore all five first.
`factory resume <id>` never starts a second agent next to a surviving one.
Every attempt runs under a small runner-owned guard process that holds the run's executor lease and the stage slot, records the agent's process identity before the agent may start, and writes an execution receipt.
When the worker died but the attempt's execution is still running or already finished, the new worker reattaches to it and gates its result, with no extra retry.
Only an execution that is gone without a receipt counts as orphaned: it is recorded as a retryable failure, which consumes a retry.
A process whose ownership cannot be verified is never killed; resume refuses and names it instead.
The finished attempt and the run's next status are saved together, so a crash between them cannot lose or repeat a transition or a retry charge.

Deterministic work never spends a paid retry.
The gates checkpoint their expensive subphases (mapped tests, the base-revision repro, the e2e driver, each validation command, each gauntlet command) under `checkpoints/`, keyed by a fingerprint of the tree, the contract fields, the command, and the runner itself.
A later gate reuses a checkpoint only when that fingerprint is identical and every published output still matches its hash; otherwise it recomputes and records why, so a code change reruns verification while the ship gate reuses what build already proved on the same tree.
When the agent's review and gauntlet are complete at the current head but its push or `gh pr create` failed, the ship gate publishes itself: it pushes, finds an existing PR for the branch before creating one, and updates the body, with at most three tries per operation.
Those operations are recorded in `run.json` under `operations` and shown by `factory show` apart from the paid retry budget, next to the current gate subphase and each reuse decision.

## Runtime layout

The runtime root is `~/.factory`, overridden by `FACTORY_HOME`.

```text
~/.factory/
  config.json          optional settings
  dashboard.html       static board, refreshed on every transition
  slots/{n}.lock       one flock per concurrent headless stage
  runs/{run-id}/
    run.json           current state, written only by the worker
    cancel, pause, note  operator intent files the worker applies at its next safe point
    request.md         the full change request, from a string, file, or Jira ticket
    intent/            the approved intent sealed at handoff: approved.json, versions/{v}/, scenarios.json, deltas/
    events.jsonl       transition history
    worker.log
    executor.lock      lease held by the guard of the run's one live execution
    executor.current   which execution took the lease last
    checkpoints/       reusable gate subphase results with their input fingerprints and output hashes
    worktree/          the run's isolated git worktree on branch factory/{plan}
    reports/           e2e, spec, and review HTML
    evidence/          PR evidence files
    scratch/{stage}-{attempt}/
    attempts/{stage}-{attempt}/prompt.txt, stdout.jsonl, stderr.log, last-message.md, gate.json,
                       request.json, executor.json, receipt.json (the guarded execution),
                       inputs/ (plan files the attempt started from), outputs.json, gate/ (gate command receipts),
                       subphase.json (the gate subphase running now and its reuse decisions)
  archive/{run-id}/    merged, garbage-collected runs without their worktree; plan/ keeps .dev/{plan}
```

## Configuration

`~/.factory/config.json` is optional; missing values use defaults.

| Key | Default | Meaning |
| --- | --- | --- |
| `max_concurrent_stages` | `4` | Headless stages that may run at once, at least 1. |
| `notify` | `true` | macOS notifications on `needs-human`, `cancelled`, and `done`. |
| `max_tokens_per_run` | `60000000` | Token ceiling for the run's reported usage (input plus output). The attempt's guard stops the agent as soon as a reported turn crosses it, so enforcement happens at Codex turn boundaries; crossing it parks the run. |
| `max_child_agents` | `6` | Concurrent sub-agents per Codex session, passed as `agents.max_concurrent_threads_per_session`, which Codex itself enforces. |
| `max_agent_depth` | `1` | Sub-agent nesting depth, passed as `agents.max_depth`. |
| `max_heavy_commands` | `2` | Runner-executed tests, e2e drivers, validation, gauntlet, and setup commands that may run at once across all runs; a lease the command's guard holds, so a worker crash cannot free it early. |
| `port_range` | `[20000, 29999]` | Ports leased to e2e services, one holder at a time across concurrent runs. |
| `stop_on_repeated_reason` | `false` | Park after two consecutive identical retryable blocks at one stage. Independently, a retry that fails with the same failure code and leaves the commit and plan files unchanged always parks as "no progress". |
| `stage_poll_seconds` | `10` | Slot wait interval. |
| `heartbeat_seconds` | `30` | Worker heartbeat interval. |
| `repos.{abs-path}.codex_sandbox` | `workspace-write` | Set to `bypass` to run Codex without its sandbox for that repository. |

Invalid JSON or invalid known values fail preflight; the runner never resets your config.

Usage is recorded per attempt by category exactly as Codex reports it: cached and cache-write input are parts of input, and reasoning output is part of output, so nothing is counted twice.
Sub-agent usage does not appear in the parent's stream and interactive scope usage is not exposed, so both are recorded as unknown rather than zero.
A cost estimate appears only with an explicit, versioned `~/.factory/pricing.json` (`factory.pricing/1`, see `runner/pricing.py`); otherwise it is unknown.
Each attempt also records queue, agent, gate, verification, and lease-wait seconds.
Model, effort, and timeout defaults live in `runner/pipeline.py`.

## Skill resolution

Headless prompts open with `$factory:{stage}`.
Codex loads an installed copy of the plugin from `$CODEX_HOME/plugins/cache/nurbot/factory/{version}/`, not the checkout path `codex plugin list` prints, so an edited checkout can run stale skills under the same version.
Every attempt compares a content hash of that copy with `plugins/factory`; when the plugin is missing or differs, the attempt reads the generated skill file by path instead, logs `skills.fallback`, and `factory doctor` warns, so a stale install never stops a run. Each attempt's `runtime.json` records the runner, skills identity, host versions, model, contract, effective configuration, and environment names (never values).
Settings that change what an attempt means (`max_tokens_per_run`, `stop_on_repeated_reason`) are read when the attempt starts, and a change is logged as `config.changed`; slots, polling, heartbeat, and notifications apply immediately.
To always read the generated skills by path, set `FACTORY_DIRECT_SKILL_PATH=1` before `factory new` or `factory retry`: prompts then name `plugins/factory/skills/{stage}/SKILL.md` instead of `$factory:{stage}`.

## Testing

All runner tests are offline and use stub `codex`, `claude`, `gh`, and `acli` binaries from `runner/tests/stubs/`, selected through `FACTORY_CODEX_BIN`, `FACTORY_CLAUDE_BIN`, `FACTORY_GH_BIN`, `FACTORY_ACLI_BIN`, and an isolated `FACTORY_HOME`.

```bash
cd factory && python3 -m unittest discover -s runner/tests -t .
bash scripts/test_factory_runner.sh
```

## Cleanup

`factory gc` archives only runs that are `done` and whose PR is merged, removing their worktree and local branch.
Plan files are never committed (the runner adds `.dev/` to the repository's local `.git/info/exclude`), so `gc` copies `.dev/{plan}/` into the archive's `plan/` first.
A repository whose base branch already tracks other plans under `.dev/` still works: `factory new` notes them and leaves them alone, refuses only a run whose own `.dev/{plan}/` is tracked (pick another `--plan`), and a stage that changes any tracked plan file fails its gate.
Nothing unmerged is removed automatically; `factory rm <id> --force` removes one exact run.

## Upgrading and older runs

`run.json` stays at schema 1; newer records carry their own versions (`factory.intent/1`, `factory.checkpoint/1`, `factory.export/1`, `factory.outcome/1`, and the rest in `references/`), and a runner refuses a version newer than it supports instead of guessing.
Runs from an older runner remain readable with `show`, `logs`, `export`, `outcome`, and `report`, which never rewrite them; fields the older runner did not record, such as usage categories or a completion observation, are shown as not recorded, and `report` counts such done runs as historical rather than verified.
Resuming or retrying an older run never reuses what cannot be verified: a run without a sealed approved intent parks with `intent.missing` and the repair `factory retry <id> --rescope`, and an unreadable or unsupported checkpoint is recomputed with the reason recorded.
No state is migrated in place, so there is no interrupted migration to recover from.
To finish a stopped run without rescoping, check out the factory revision that started it and resume it with that runner; the run directory is compatible because nothing was rewritten.

## Real dry run notes

Historical local observation, recorded before the reliability work in `research/improve.md`; it has not been reverified against the current runner.
First real run: 2026-09-15, a private service repository, started from a Jira ticket, `max_concurrent_stages` 4, Codex CLI 0.154.0.
Source: that run's retained `run.json`.

| Stage | Outcome | Duration | Tokens |
| --- | --- | --- | --- |
| scope | done, attempt 1 | 3h07m elapsed (interactive wall time, not active effort) | not tracked |
| scope-review | done, attempt 1 | 10m | 3.2M |
| build | done, attempt 1 | 6.5m | 7.7M |
| ship | blocked at attempt 1: no open pull request for the branch | 23m | 20.9M |
| ship | done, attempt 2 (operator retry after fixing `gh` credentials, PR #44) | 2.5m | 0.8M |

The run finished `done` using 1 of 5 retries.
The first ship attempt's missing PR is the failure the runner now handles itself: ship publication is runner-owned and retried without a paid attempt (`runner/tests/test_checkpoints.py`, `PublicationTests`), and the sandbox credential cause is covered by `runner/tests/test_hosts.py` and `test_worker.py` (`GH_TOKEN` forwarding).

Confirmed:

- `$factory:{stage}` resolves from the installed Codex plugin; `FACTORY_DIRECT_SKILL_PATH` was not needed.
- `openai.gpt-5.6-sol` and `openai.gpt-5.6-luna` are accepted, and the `workspace-write` sandbox runs the factory scripts, commits in the linked worktree, and pushes over ssh.
- Ship parked instead of burning retries when credentials were missing, as the decision policy requires.

Found and fixed:

- `gh` inside Codex failed with HTTP 401 even though the host was logged in: `gh` keeps its token in the macOS keychain, which the sandbox blocks. The runner now reads `gh auth token` on the host and passes it to every attempt as `GH_TOKEN`, unless `GH_TOKEN` or `GITHUB_TOKEN` is already set.
- The 20M token ceiling was below one first-try run (31.7M total); the default is now 60M.
- The repository already ignored `.dev/`, so plans were never committed; the runner now excludes `.dev/` itself so every repository behaves that way.

Setup notes:

- An existing `nurbot` Codex marketplace pointing at the Git remote must be removed and re-added from the local checkout to test unreleased factory changes (`codex plugin marketplace remove nurbot`, then `add ~/ws/workflow`).
- Headless Codex loads your global MCP servers; expired OAuth for them only adds errors to `stderr.log`.
