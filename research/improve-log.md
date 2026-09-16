# Execution log: factory reliability plan

Companion to [improve.md](improve.md).
It records the baseline, research decisions, deviations, and evidence per work package, as section 2 of the plan asks.

## Phase 0

### Baseline (2026-09-16)

- Checkout: `81cb1ca feat(docs): keep a high-level system overview in docs/architecture.md` on `main`.
- Pre-existing working-tree changes: modified `.agents/plugins/marketplace.json`, `.claude-plugin/marketplace.json`, `.gitignore`, `CLAUDE.md`, `README.md`, `scripts/build_codex_plugin.py`, `scripts/validate.sh`; untracked `factory/`, `plugins/factory/`, `research/`, `scripts/test_factory_runner.sh`, `todo/factory-evaluation.md`.
  The whole factory implementation is uncommitted, so every change below lands on top of that uncommitted tree.
- `bash scripts/validate.sh`: `All checks passed.`
- Runner suite: `Ran 187 tests in 71.924s OK` on Python 3.14.7, macOS 15 (Darwin 24.6.0).
  The count still matches the evaluation.

### How the runtime pieces relate

- A run directory `~/.factory/runs/{id}/` owns `run.json`, `events.jsonl`, intent files, `attempts/`, `reports/`, `evidence/`, `scratch/`, and the linked `worktree/`.
- The worktree is a linked Git worktree of the operator's repository on branch `factory/{plan}`; its Git metadata lives in the repository's common Git directory.
- A stage attempt is one fresh host process (`codex exec` for headless stages, `claude` for scope) with `attempts/{stage}-{n}/` holding its prompt, streams, and `gate.json`.
- The detached worker holds `worker.lock` (flock) for its lifetime and is the only writer of `run.json`.
- Headless stages also hold one `~/.factory/slots/{n}.lock` flock, taken by the worker before the attempt.
- The host child is spawned with `start_new_session=True`, so it is its own session and process-group leader, independent of the worker's group.
- Gates run inside the worker after the child exits: validation commands, `gh`, and Git calls are plain subprocesses of the worker.

### What survives a worker crash

- Survives: `run.json` as last saved, `events.jsonl`, attempt files, the worktree, and the host child process group (its own session, not killed with the worker).
- Released: `worker.lock` and the slot flock (both are fds of the worker process), so the slot looks free while the orphaned host keeps working.
- Lost: the in-memory decision between saving a finished attempt and saving its transition, and any running validation command's supervision.
- `factory resume` warns that the orphaned group is alive, records the open attempt as failed, and launches a replacement: this is R8.

### Places the runner trusts agent-authored files

1. `{stage}-result.json`: `retryable: false` can downgrade a failing gate to non-retryable, and a blocked status on a passing gate becomes only a warning (R2).
2. `spec.md` after scope: scope-review may edit it, and the build gate executes its `### Validation` commands on the host without a sandbox.
3. `implementation-notes.md` test names: `check-tests.py` searches file text for them (R1).
4. The e2e report data block: parsed through Node `eval` (R4) and trusted for its summary counters (R1).
5. `spec-review_N.md` verdict and deferral kinds, and `review_N.md` verdict and blocker titles.
6. Local `pr.md`: checked by `pr-evidence.py check` instead of the published PR body (R7).
7. The review panel's result files: missing lenses aggregate to PASS (R5).
8. The run directory itself: Codex gets `--add-dir {run_dir}`, so an agent can write `run.json`, `events.jsonl`, and intent files the worker reads.
9. The repository's common Git directory: also `--add-dir`, so `info/exclude` (which hides `.dev/`) is agent-writable.
10. The Codex event stream: token usage is read from the agent process's stdout.

### R1-R8 reproductions

Reproduced with `scratchpad/probes/test_probes.py` (session scratchpad; each case is rebuilt as a permanent test with its fix).
Each probe asserts the post-fix behavior, so all eight fail on the baseline:

| Case | Original failing assertion |
| --- | --- |
| R1 | `'done' == 'done' : R1: run completed (new exit 0) without executed tests` |
| R2 | `'done' == 'done' : R2: missing-credentials condition was reduced to a warning` |
| R3 | `'done' == 'done' : R3: scope-review committed src/rogue.py and passed` |
| R4 | `True is not false : R4: parsing created a marker file (data={'kind': 'non-frontend', 'scenarios': []}, error=None)` |
| R5 | `'PASS' == 'PASS' : R5: empty panel aggregated to {'verdict': 'PASS', 'panel': [], 'failedLenses': ['correctness', 'security'], ...}` |
| R6 | `True is not false : R6: validation started after cancel/deadline (gate passed=True)` |
| R7 | `True is not false : R7: ship passed with a review older than HEAD` |
| R8 | `True is not false : R8: old host and replacement host were alive together` |

Why each fails today:

- R1: `check-tests.py` only greps for the name, and `build_gate` compares `summary.total` with the spec's e2e count without reading scenario records.
- R2: `gates.merge` returns `done` whenever the gate passed, turning any skill result into a warning.
- R3: `scope_review_gate` inspects `git status`, and a commit empties it.
- R4: `load_e2e_data` builds its candidate tuple eagerly, so `node_json` runs `eval` even before JSON is tried.
- R5: `aggregate-findings.py` computes the verdict only from findings and reports missing lenses beside it.
- R6: `build_gate` never consults `ctx.deadline` or `ctx.cancel_requested`, and each command gets its own 20-minute timeout.
- R7: `ship_gate` takes the highest `review_N.md` without a revision and checks only that nothing is unpushed.
- R8: the host group outlives the worker, and `control.resume` launches a replacement after warning about it.

## W01: parse artifacts as data only

Research:

- Producers of `E2E_DATA`: build step 5 (rendered by the agent into the HTML template), the ship gauntlet's e2e refresh, and ship remediation.
- Readers: `gates.build_gate` (through `pr-evidence.load_e2e_data`), `pr-evidence.py extract`, and the ship `tests` and `spec-conformance` lenses.
- Confirmed the eager call: `for candidate in (literal, node_json(literal), js_to_json(literal))` builds the tuple first, so Node `eval` ran even for valid JSON, with no timeout.
- Existing format inventory: one shape, a JS object literal with `planName`, `summary`, `screenshots[].dataUri`, `dataModelState`, `logsOrOutput`; test fixtures used strict JSON inside it.

Decision:

- One shared data-only module, `factory/scripts/factory_records.py`, shipped with the plugin and loaded by the runner through `runner/records.py`.
  Skill scripts import it with `parents[3] / "scripts"`, which resolves in both `factory/` and `plugins/factory/`.
- The e2e record is the `factory.e2e/1` sidecar `{report_dir}/{plan}-e2e.json`: strict JSON (no duplicate keys, no non-finite numbers), an 8 MB size check before reading, unknown keys rejected, derived counts, and screenshots as files with SHA-256 instead of inline data URIs.
- `skills/build/scripts/render-e2e.py` renders the HTML from the validated sidecar and embeds the data as inert JSON with `<` escaped.
- Legacy policy: `legacy_e2e_from_html` reads an old report only when its block is strict JSON (for inspection and `pr-evidence.py extract`); a JS literal fails with `record.legacy_literal` and a regeneration instruction.
  The gate never reads HTML and names a legacy report it finds with the regeneration path.
- `node_json` and `js_to_json` are deleted rather than bounded: no converter executes or rewrites a literal.

Evidence:

- `runner/tests/test_records.py`: `test_r4_expression_in_legacy_report_is_rejected_without_side_effects` (no marker, `subprocess.Popen` never called), `test_gate_loader_never_runs_a_javascript_runtime`, `test_gate_failures_are_bounded_and_actionable` (missing, malformed, unsupported, oversized), `test_size_is_checked_before_reading`, `test_source_layout` and `test_generated_layout` (render plus extract from both trees), `test_legacy_strict_json_report_stays_inspectable`.
- `test_gates.BuildGateTests.test_e2e_report_variants` covers the legacy regeneration hint.
- Full fixture path: `test_worker.HappyPathTests.test_full_happy_path` and `scripts/test_factory_runner.sh` run build to ship on the sidecar.
- Suite: 201 tests OK; `Factory runner e2e passed.`

## W04: preserve one active executor and make completion recoverable

Research:

- Process tree before: worker (own session) spawned the host with `start_new_session=True`; worker death released `worker.lock` and the slot flock while the host group kept running.
- Spawn window: `on_spawn` saved the PID only after `Popen` returned, so a crash there left an unrecorded host.
- Completion window: `finish()` saved the finished attempt, then `apply_decision` mutated and saved again; a crash in between left no open attempt, and `reconcile_orphan` requeued the stage.
- Process-start identity: Linux exposes `/proc/{pid}/stat` field 22; macOS has no `/proc`, but `ps -o stat=,lstart= -p PID` works on both (second resolution, plus zombie state), so identity is `proc:<starttime>` or `ps:<lstart>`.
- flock semantics: an inherited fd shares the lock's open file description, and `LOCK_UN` from any holder releases it for all, so the worker must close (not unlock) its copy on death; normal release happens only after the guard exited.

Decision:

- `runner/executor.py`: every supervised command runs under a guard process in its own session.
  The guard takes `runs/{id}/executor.lock` (refusing a second execution), inherits the stage slot fd from the worker, writes `executor.json` with its own identity, spawns the child in a new session behind a pipe barrier, records the child's PID and start identity, and only then writes `go`.
  A guard that dies before `go` closes the pipe, and the barrier exits 125 without executing the command.
  The guard enforces the attempt deadline (`deadline_at`, epoch seconds persisted on the attempt) and the cancel file, then writes `receipt.json` (`factory.receipt/1`: argv hash, cwd, boundary, timestamps, classification, exit code, stdout/stderr hashes).
- Deviation from the plan's "keep the lease until descendants stop": the child does not inherit the lease or slot fds.
  A background process an agent leaves behind (for example a dev server started with `setsid`) would otherwise hold them forever and wedge the run.
  After guard loss, liveness comes from the recorded child identity and its process group instead; a verified group is waited on or stopped, a mismatched identity is treated as not ours, and an unverifiable one raises `Ambiguous`.
- The worker records the execution token and directory in `run.json` before launching the guard, so recovery always knows what to look for.
- `Worker.recover` reattaches: it waits for a live execution (enforcing cancel and deadline once the guard is gone), gates a finished receipt, closes a lost one as an orphaned retryable failure, and parks on `Ambiguous`.
- `finish()` computes outcome, retry charge, and next status, stores the transition with its planned events on the attempt, and saves once; `emit_pending` then writes events not yet in `events.jsonl`, keyed by `transition#index`, so a crash after the save repairs the log without repeating anything.
- Controls: `resume` reports live or finished executions and lets the worker reattach; `cancel` stops a surviving execution by verified identity; `pause` reattaches before pausing; `retry` and `rm --force` refuse while an execution is live.
- Side effects: the scope-review commit is idempotent (`commit_paths` commits nothing when already committed), and PR creation is reconciled by the ship stage against the existing PR; the runner-owned publication checkpoint is W13.
- Retry accounting: an orphaned (lost) attempt still consumes one retry, as documented before; the charge happens inside the single transition save, so it is recorded exactly once.

Evidence (`runner/tests/test_executor.py`, `runner/tests/test_recovery.py`, real detached workers and `FACTORY_FAULT` crash points):

- R8: `test_r8_resume_reattaches_to_the_surviving_host_instead_of_starting_another` samples `ps` throughout and observes a maximum of 1 host process, one `codex exec`, one attempt, and no retry.
- Spawn window: `test_a_host_registered_but_never_released_never_starts_work` (guard dies before `go`; attempt 1 never executes codex) and `test_worker_crash_before_launch_starts_no_host`.
- Slot: `test_worker_death_keeps_guard_supervising_and_slot_held`.
- PID reuse and ambiguity: `test_unrelated_process_with_a_reused_pid_is_left_alone`, `test_unverifiable_ownership_blocks_the_replacement_and_kills_nothing`, plus the executor-level equivalents.
- Exactly once: `test_crash_before_the_transition_is_saved_completes_each_stage_once`, `test_crash_after_the_transition_is_saved_repairs_the_log_without_rerunning`.
- Side effects: `test_crash_after_the_scope_review_commit_keeps_one_commit`, `test_worker_crash_during_ship_keeps_one_pull_request`, `test_lost_ship_guard_retries_without_a_duplicate_pull_request`.
- Retry accounting: `test_retry_charge_is_neither_lost_nor_duplicated`.
- Cancel and cleanup: `test_cancel_stops_a_surviving_host_when_no_worker_is_alive`, `test_rm_refuses_a_live_execution`.
- Suite: 224 tests OK; `Factory runner e2e passed.`

## W08: supervise validation under the same attempt limits

Research (blocking subprocesses and waits in gates, before):

| Call | Where | Deadline | Cancellation |
| --- | --- | --- | --- |
| `lint-spec.py` | scope and scope-review gates | own 600 s timeout | none |
| `check-tests.py` | build gate | own 600 s timeout | none |
| each Validation line | build gate | own 20 min timeout each | none; `SIGKILL` of the group only on timeout |
| `gh pr list`, `gh pr checks` | ship gate | own 120 s timeout | only between polls |
| check polling sleep | ship gate | stage deadline (monotonic) | between polls |
| `pr-evidence.py check` | ship gate | own 600 s timeout | none |
| `git fetch` | ship gate via `wt.unpushed` | own 600 s timeout | none |
| `git status`, `rev-list`, `rev-parse`, `add`, `commit` | all gates, scope-review commit | 300 s default | none |

Decision:

- Every gate command that runs repository code, a skill script, the network, or `gh` is a guarded execution (`gates.execute`): the W04 guard checks cancellation and the remaining deadline before launch, stops the process group on cancel or deadline, and leaves a receipt and raw logs under `attempts/{stage}-{n}/gate/NN-label/`.
  Its deadline is `min(command cap, attempt deadline)`; the attempt deadline is persisted as epoch seconds so a reattached worker keeps the same one.
- Local Git plumbing (`status`, `rev-list`, `rev-parse`) stays a direct call: it touches no network and no repository code, and the gate checks `stop_reason()` between steps.
- The agent's own allowance ends `min(30 min, 25%)` before the attempt deadline, so the gate has time inside the same deadline instead of a new one.
- Line-by-line Markdown execution is gone.
  Validation is the committed repository contract `.factory/contract.json` (`factory.repo-contract/1`): argv records or one repository script, `cwd` inside the repo, declared `env` names and literal `set` values, a `timeout_s` cap, and a `boundary`.
  Commands see a small baseline environment plus declared inputs only; an unset declared variable fails before launch with the variable named.
  The scope gate requires a valid contract and a spec `### Validation` block that names every command id.
- Boundaries: `host` (the runner's privileges) or `workspace-write` (writes limited to the worktree, the execution directory, and system temp; network open), enforced with `sandbox-exec` on macOS and `bwrap` on Linux, and failing with a repair step when the tool is missing.
  This mirrors Codex `workspace-write` with network access, which is how the factory launches agents.
- Timing: attempts record `host_seconds`, `gate_seconds`, and `gate_executions`; `seconds` is the whole attempt's elapsed time.
- Scope's interactive gate ignores cancel intent (the handoff check afterwards handles it), as before.

Evidence:

- `test_gates.ValidationTests`: `test_r6_nothing_starts_after_cancel_or_deadline`, `test_cancelling_active_validation_stops_the_command_and_its_descendants`, `test_commands_share_one_deadline_instead_of_each_getting_their_own`, `test_command_cap_failure_output_and_receipts_stay_on_disk`, `test_a_script_keeps_directory_exports_and_multiline_control_flow`, `test_declared_environment_only`, `test_workspace_write_boundary_limits_writes_to_the_worktree` (verified on macOS 15: the write outside the worktree is denied, the host boundary allows it), `test_missing_or_malformed_contract_is_not_retryable`.
- `test_recovery.ValidationSupervisionTests`: `test_gate_time_is_recorded_apart_from_the_host_and_inside_the_attempt_total`, `test_cli_cancel_during_validation_stops_it_and_keeps_its_output`.
- Ship polling: `test_cancel_stops_pending_check_polling`, `test_pending_checks_at_deadline`.
- `bash scripts/validate.sh`: `All checks passed.`

## W06: enforce park conditions in the authoritative outcome

Research:

- The park list lived only in `factory/references/factory-run.md` as prose; stages reported it in `human_calls` (`kind`: premise, environment, blocker) and `retryable: false`.
- Emission points: build (no spec, no launch command), scope-review (lint failure at start, premise and new-effort deferrals), ship (secret, missing credentials or `gh`, no reviewable files), and the Jira reference (auth failures).
- `gates.merge` returned `done` for any passing gate, so a non-retryable, credential-blocked result became a warning (R2); on a failing gate, a bare `retryable: false` could downgrade any failure to a park.
- Distinctions drawn: an unresolved prerequisite is a typed condition; a failed verification is a gate failure with a code; an invalid artifact is `result.invalid`; a model's advisory `blocked` without a condition is only a warning when the gate passes.

Decision:

- Result schema 2 (`validate_result` in the shared records module): known keys only, `conditions` required, `human_calls` and `retryable` removed; schema 1 fails explicitly.
- Condition codes are a fixed table (`CONDITION_CODES`) mapping the five park-list items to `premise.invalidated`, `scope.new_effort`, `secret.found`, `environment.missing_credentials`, `action.destructive`, `launch.unavailable`, `decision.verification_failed`, plus `input.unusable` for unusable handed-off input.
  Category, retryability, and the operator's next action come from the code, never from the stage.
- `runner/conditions.py`: the worker records unresolved conditions as `C1`, `C2`, ... in `run.json`; the raising stage cannot complete while one is open; a later attempt must report `resolution: resolved` with evidence and the id, or the runner re-checks objectively (`requires_env` names in the runner's environment), and a failing runner re-check overrides a claimed resolution.
- `gates.merge(gate, result, result_error, blocking)`: invalid result fails (`result.invalid`); unresolved conditions block even a passing gate; a claimed success never overrides a failed gate; bare blocked statuses stay warnings.
- Gate failures carry stable codes (`validation.failed`, `e2e.failed`, `attempt.deadline`, `ci.pending`, ...), stored on the attempt and in `stage.finished`.
- Surfaces: `factory show` lists each condition with status, evidence, re-checks, resolution, or next action; the watch view prints the codes, evidence, and next action when the run parks; the next attempt's `factory-run.json` and prompt list the open conditions.

Evidence:

- `test_worker.ParkConditionTests.test_r2_unresolved_condition_parks_despite_a_passing_gate` (gate passed, run parked with `environment.missing_credentials (C1)`, no retry charged, `show` and watch output checked).
- `test_clearing_a_real_prerequisite_permits_progress_after_the_runner_re_checks_it` (a claimed fix with the variable still unset stays parked with a warning; after exporting it and retrying, the runner resolves C1 and the run completes).
- `test_a_condition_the_runner_cannot_observe_needs_an_evidenced_resolution` (an unaddressed condition keeps blocking; an evidenced resolution completes).
- `test_unknown_condition_codes_and_decision_failures` (unknown code fails as `result.invalid`; `decision.verification_failed` retries).
- `test_gates.MergeTests` covers the success, failure, and condition matrix; `test_malformed_result_fails_gate` covers schema 1 and malformed schema 2.
- `bash scripts/validate.sh`: `All checks passed.` (F03 wording check included.)

## Phase 1 exit

- R2, R4, R6, and R8 have permanent passing tests (named above).
- Parsing cannot execute report content: the only e2e parser is strict JSON.
- Every attempt command that runs repository code, skill scripts, `gh`, or the network runs under the guard with the shared deadline and cancel file; local Git plumbing between steps is bounded and preceded by a stop check.
- Recovery preserves executor ownership and committed transitions (W04 evidence).
- The happy path completes on fixtures with the contract, the e2e sidecar, and schema-2 results.

## W07: enforce stage boundaries across the complete Git delta

Research:

- Permitted writes per stage: scope writes `.dev/{plan}` (untracked), `docs/`, and now `.factory/contract.json`; scope-review writes `.dev/{plan}`, `docs/decisions.md`, `docs/contracts.md`; build and ship write code, tests, docs, `.dev/{plan}`, reports, evidence, and scratch.
- A linked worktree's Git writes land in the common directory's `objects/`, `refs/`, `logs/`, and its own `worktrees/<name>/` (index, HEAD, per-worktree logs); `git push -u` also writes the shared `config`.
- The previous `--add-dir {common git dir}` let an agent write `config` (`core.fsmonitor`, `core.hooksPath`), `hooks/`, and `info/exclude`; the runner's own later `git status` and `git commit` would then run that code outside any sandbox.
- Probe (macOS 15 Seatbelt via `sandbox-exec`, outside the temp directories): with only `objects`, `refs`, `logs`, and `worktrees/<name>` granted, `git add`, `git commit`, and `git fetch` succeed, while `git config`, writing `hooks/pre-commit`, and appending to `info/exclude` are denied.
  A push to a local bare origin outside the grants fails in the receiving process; a network remote writes nothing locally.

Decision:

- Each attempt records its starting `head` and `branch`; `gates.boundary` compares the complete delta from that commit (`git diff --no-renames <start>` for committed, staged, unstaged, and deleted paths, so both rename sides appear, plus untracked files) with the stage's path contract.
- It also requires the run branch, requires the starting commit to remain an ancestor of HEAD, and rejects `.dev/` paths that became tracked.
  Committed violations, branch switches, and rewritten history park (undoing them would need a history rewrite); uncommitted ones retry.
- The scope, scope-review, build, and ship gates all run the check; the contract is locked after scope.
- Codex gets `--add-dir` for the run's reports, evidence, and this attempt's scratch directory, and for the four Git metadata directories above, never the run directory or the common Git root.
  The runner records the upstream at worktree creation so `git push {remote} HEAD` needs no config write, and the ship reference now says so.
- The runner's own Git calls pass `-c core.hooksPath=/dev/null -c core.fsmonitor=false`, which also protects bypass mode.
- Attempts record `sandbox` capabilities: workspace-write lists its writable roots and what it protects; bypass records `enforced: false` and protects nothing.
- `factory new` preflight reports plan files the base already tracks, with a repair, and changes nothing.

Evidence:

- `test_gates.BoundaryTests`: `test_r3_a_committed_unauthorized_source_file_is_named`, `test_every_kind_of_uncommitted_change_is_seen` (staged, unstaged, deleted, untracked), `test_both_sides_of_a_rename`, `test_branch_switch_and_history_replacement` (including detached HEAD), `test_force_added_plan_files_are_rejected`, `test_the_contract_is_locked_after_scope`, `test_legitimate_changes_pass`, and `test_narrowed_git_grants_commit_and_fetch_but_protect_runner_owned_git_files` (real linked worktree under the OS sandbox).
- `test_worker.BoundaryTests.test_r3_scope_review_committing_a_source_file_parks_with_the_path` and `test_attempts_record_their_start_and_sandbox_capabilities` (no run-directory or common-Git grant; bypass recorded as unenforced).
- `test_cli.NewAndScopeTests.test_preflight_reports_plan_files_the_base_already_tracks`.
- Suite: 249 tests OK before the preflight test; `Factory runner e2e passed.`
- Real Codex sandbox (codex-cli 0.154.0, macOS): `codex sandbox -P :workspace` with the worktree as its writable root denies writes to the run's `run.json` and intent files, to the common Git `config`, `hooks/`, and `info/exclude`, and allows the worktree; captured as the opt-in `test_real_codex_sandbox_denies_writes_to_runner_records_and_git_control_files` (`FACTORY_REAL_CODEX_SANDBOX=1`, no model call), which passed.
- Not yet verified against Codex itself: that `codex exec --add-dir` with the narrowed Git directories commits and fetches (proven with `sandbox-exec`, the same Seatbelt mechanism); `codex sandbox` exposes no `--add-dir` equivalent, so this needs the W11 real-host smoke run, and Linux (Landlock) is unverified.

## W09: version approved intent and stage handoffs

Research:

- What the operator approves at the handoff: the gated `spec.md` (its research decisions, scope with `⊘` non-goals and the Validation rendering, and the change plan's layer-tagged `tests:` scenarios), the committed `.factory/contract.json`, and the request saved as `request.md`.
- User-visible requirements are the request, the non-goals, and each scenario's layer and requirement text; decisions (`D-` entries), file lists, and change-set wording are technical and may be refined unattended.
- Overwritten between attempts before this change: `spec.md` (scope-review edits it in place), `implementation-notes.md`, `spec-review_N.md` and `review_N.md` (append new indexes, but a stage can rewrite old ones), `pr.md`, and `.dev/factory-run.json`; attempt directories kept only prompts, streams, and gate results.
- Scenario identity: there were no ids; `check-tests.py` counted scenarios per change set.
  Matching by position breaks when a scenario is inserted, so ids are assigned by `(layer, normalized requirement)` and never renumbered.
- Bug fixes had no machine-readable marker; the scope skill now tags them `[repro]` after the layer, which `lint-spec.py`'s tag check already accepts.

Decision:

- `runner/intent.py` seals `intent/approved.json` (`factory.intent/1`: request and its hash, non-goals, scenarios with `S` ids and `repro` flags, spec and contract hashes, base, version, content hash) at the existing handoff, plus `intent/versions/{v}/` copies of the approved spec, contract, and request; `run.json` records the content hash.
- `intent/` lives in the run directory, which W07 keeps out of every stage's sandbox grant.
- Gates for scope-review, build, and ship verify the seal (content hash, the hash in `run.json`, and the versioned spec and contract hashes) and compare the locked fields mechanically: an approved scenario missing by layer and text (removed, reworded, or retagged, which covers weakening e2e to unit) or a missing non-goal fails with `intent.scenario_changed` and names the sealed spec to restore from.
  This is retryable: the spec is an untracked file the next attempt can restore; a stage that disagrees defers it as a premise.
- A missing or tampered snapshot fails non-retryably (`intent.missing`, `intent.tampered`) with the repair `factory retry <id> --rescope`, which reopens interactive scope; the next handoff seals version 2 and keeps the ids of unchanged scenarios.
- Build validation now reads the sealed contract snapshot, not the worktree file.
- Added scenarios are registered in `intent/scenarios.json` with their origin (`scope-review attempt 1`).
- Each attempt copies only the plan files it starts from (`spec.md`, notes, scenario map, `pr.md`, spec reviews, reviews) and `factory-run.json` into `attempts/{stage}-{n}/inputs/` with a manifest (source revision, intent and contract hashes, file hashes), and records `outputs.json` after the gate; the repository itself is never copied.
- `intent/deltas/{stage}-{n}.json` records changed, added, and removed decisions, added and removed scenarios, scenarios whose change sets link a changed decision, and the attempt's auto-decided lines.
- Read paths: `factory intent <id>` and `factory inputs <id> --stage --attempt [--file]`, both working from the archive.

Evidence (`runner/tests/test_intent.py`):

- `test_handoff_seals_request_non_goals_and_scenarios_with_stable_ids` (through `factory new`).
- `test_removing_weakening_or_renaming_an_approved_scenario_is_detected` (removed, e2e weakened to unit, reworded) and `test_removing_an_approved_non_goal_is_detected`.
- `test_refinement_within_the_intent_continues_with_an_audit_record` (a changed decision and an added integration scenario complete the run; the delta names `D-dedup-store`, `S3`, affected scenarios, and one auto-decision).
- `test_earlier_attempt_inputs_remain_after_a_later_attempt_rewrites_the_files`.
- `test_tampered_or_missing_snapshots_block_with_a_repair`, `test_rescope_reseals_a_new_version_and_keeps_scenario_ids`, `test_snapshots_survive_archiving`.
- `bash scripts/validate.sh`: `All checks passed.`

## W11: make repository execution and runtime provenance reproducible

Research:

- Skill resolution on the real host (codex-cli 0.154.0): `codex plugin list` prints the checkout path `/Users/nurbot/ws/workflow/plugins/factory` as the plugin's source, but Codex loads its installed copy under `~/.codex/plugins/cache/nurbot/factory/0.1.0/`.
  That cache already differed from the checkout here (older `factory-run.md`, `build/SKILL.md`, no `factory_records.py`) while both reported version 0.1.0: runs from this machine would silently use stale skills.
- The direct-path fallback (`FACTORY_DIRECT_SKILL_PATH`) names the generated `plugins/factory` files in the prompt, so its identity is the generated bundle.
- Checkable without inference: binaries, versions, plugin listing, content hashes of the generated and cached bundles, generated-bundle currency (`build_codex_plugin.py --check`), environment names, command presence, linked-worktree Git under the sandbox, token forwarding (`gh auth token`), contract setup commands.
  Needs a real call: whether a model name is accepted and `$factory:{stage}` resolves inside a session.
- Repository inventory the contract must carry: setup, validation, the test runner and result format, the e2e driver with its services and ports, required environment names, and the pull-request checks CI requires (or a reasoned statement that there are none).
- Configuration: the worker reloads `config.json` every loop, so a `max_tokens_per_run` edit changed the decision for an attempt already running.

Decision:

- The contract (`factory.repo-contract/1`) now requires `ci` (`required_checks`, optional `allow_skipped`, or `none` with a reason) and defines `tests` (runner command, `junit` or `factory.test-results/1`, layer globs), `ports`, `services` (with a `ready` command), `e2e` (driver and services, or `none` with a reason), and `gauntlet`; placeholders are validated.
  `contract_gaps` reports scenario layers the contract cannot execute; W02 wires it into the scope gate with its consumers.
- `runner/provenance.py` identifies the skills by content hash of the files that execute: the cached copy for an installed plugin, the generated bundle for direct-path mode, `unverified` when neither can be read, so different bundles can never share a label.
- Preflight (`factory new`) and `factory doctor` fail when `plugins/factory` is stale or when the bundle Codex loads differs from it, with the reinstall repair; every headless attempt re-checks and stops before launch with `runtime.skills_mismatch` or `runtime.skills_missing`.
- Every attempt writes `attempts/{stage}-{n}/runtime.json` (`factory.runtime/1`): runner version, content hash, Git revision, and whether it has uncommitted changes; skills bundles and resolution; codex, git, gh, Python, and platform versions; model, effort, and timeout; the approved contract hash; the effective configuration and its hash; required environment names as `set`/`unset` and how the GitHub token arrives, never a value.
- Configuration boundary: `max_tokens_per_run` and `stop_on_repeated_reason` are recorded on the attempt at start and used for its decision; slots, polling, heartbeat, and notifications stay live; a change between attempts emits `config.changed`.
- `factory new` refuses the handoff while a contract-required environment name is unset.
- `factory doctor --repo PATH [--remote] [--run-setup] [--smoke]`: contract validity, unset environment names, commands missing from `PATH` or the repository, token forwarding, a sandboxed commit in a throwaway linked worktree (removed afterwards), optional setup commands through the executor, and an explicit smoke mode that makes one real Codex call per headless model, cached in `~/.factory/capabilities.json` by Codex version, skills identity, model, effort, and sandbox.

Evidence (`runner/tests/test_provenance.py`):

- `test_every_attempt_records_its_effective_runtime_without_secret_values` (the secret value is absent from the manifest).
- `test_an_uncommitted_runner_edit_changes_the_runner_identity`.
- `test_a_stale_codex_plugin_cache_stops_the_attempt_before_launch` (the real-host shape: listed source current, cache stale; no `codex exec`), `test_preflight_and_doctor_name_the_drift`, `test_source_and_installed_bundles_never_share_a_label`.
- `test_a_configuration_change_applies_from_the_next_attempt_and_is_recorded` (a ceiling lowered mid-attempt does not park that attempt; the next one records and applies it).
- `test_existing_configuration_stays_readable`.
- `test_an_unset_required_environment_name_stops_the_handoff`, `test_doctor_repo_checks_contract_environment_commands_and_worktree_git`, `test_doctor_repo_rejects_an_incomplete_contract`, `test_smoke_resolves_skills_once_and_is_cached`.
- Real host (this machine): `provenance.skill_bundles()` reports `installed_matches_generated: false` for the stale cache, and the W07 `codex sandbox` receipt covers macOS; no Linux host was available for a Landlock receipt.
- `bash scripts/validate.sh`: `All checks passed.`
- Not run: `factory doctor --smoke` against real models (paid, and it would currently fail on the stale cache until the plugin is reinstalled).

## W02: require execution-backed scenario evidence

Research:

- Before: `check-tests.py` counted `Tests added:` names per change set and grepped each name in its file, so a comment satisfied it; the build gate compared the report's `summary.total` with the spec's `[e2e]` count; the happy fixture's tests were `pass` bodies and its e2e record had no scenarios.
- The fixture's framework (Python `unittest`) has no machine-readable result format, and real repositories mostly emit JUnit XML (pytest `--junitxml`, Jest, Go, Gradle, and others), so the adapter surface is two formats: JUnit, and a minimal `factory.test-results/1` JSON for anything else.
- Browser-driven and non-frontend scenarios both need the application launched and driven by code the runner can rerun, so the e2e evidence producer is a committed driver declared in the contract, not the agent's session.
- Scenario ids are run-specific, so repository code must not embed them: e2e scenarios map to stable driver case names.

Decision:

- Build writes `.dev/{plan}/scenario-map.json` (`factory.scenario-map/1`): every present scenario id at its own layer, unit and integration to test ids, e2e to a driver case; unknown ids, unmapped scenarios, wrong layers, and a test listed twice for one scenario are rejected, and one test may still cover several scenarios explicitly.
- `runner/verification.py` runs at the build gate, all through the guarded executor in the contract's boundary:
  - the contract's `tests.run` with exactly the mapped ids and a runner-owned results path in a fresh execution directory; a mapped test that is absent from the results, skipped, failed, or errored fails, and optional `tests.layers` globs enforce layer placement;
  - for each `[repro]` scenario, a detached worktree at the run's base with only the mapped test files overlaid; the tests must fail on an assertion there (passing or erroring is rejected), and the worktree is removed;
  - the contract's services and e2e driver as one process group (`runner/servicehost.py`: start, readiness command, driver, teardown) with free ports and `FACTORY_E2E_OUT`/`FACTORY_REVISION`; the record must name the tested revision, cover every mapped case with a passing status and assertions, carry a screenshot (frontend) or a before/after state (non-frontend), and have hash-matching evidence files.
- Totals come from the scenario records; the runner publishes the verified record to `{report_dir}/{plan}-e2e.json` and renders the HTML, so the agent never authors the evidence the PR shows.
- The scope gate now reports contract gaps before handoff (unit or integration scenarios without a `tests` runner, e2e scenarios without a driver).
- `check-tests.py` is rewritten to validate the scenario map with the same shared validator, so the build agent's loop and the gate agree.
- The fixture is a tiny real application: a buggy `webhook.py` at the base, the fix, a `unittest` assertion, a repository test runner that reports by id, an e2e driver that captures real before/after state, and a `.gitignore` for bytecode.

Evidence:

- `test_gates.BuildGateTests`: `test_happy_path`, `test_r1_names_in_comments_and_an_empty_e2e_record_are_not_evidence`, `test_the_scenario_map_must_cover_every_scenario_exactly` (missing, unmapped, unknown, wrong layer, duplicate), `test_skipped_failing_and_wrong_layer_tests_are_rejected`, `test_results_come_from_a_fresh_run_every_time`, `test_a_broken_implementation_fails_despite_a_green_looking_agent_report` (buggy code plus an agent sidecar and a `done` result; then inert tests, caught by the real driver), `test_e2e_evidence_is_checked_against_the_revision_and_its_files` (stale revision, forged evidence hash, missing state, crashing driver).
- `test_gates.ReproTests`: `test_a_real_reproduction_is_red_on_base_and_green_on_the_change`, `test_an_inert_or_setup_failing_reproduction_is_rejected`.
- `test_worker.EvidenceTests.test_r1_comment_only_tests_and_an_empty_e2e_record_never_complete_through_the_cli` (`factory new` exits 1, build retries exhaust with `evidence.tests_failed`, no PR).
- `test_worker.HappyPathTests.test_full_happy_path` and `scripts/test_factory_runner.sh` complete the whole pipeline on the real fixture application.
- `test_provenance.ReadinessTests.test_a_plan_the_contract_cannot_launch_is_refused_at_handoff`.
- Finding fixed along the way: runner-executed Python tests leave `__pycache__` in the worktree, which a later `git add -A` commits; the fixture ignores bytecode, and W03 re-checks worktree state after verification.
- `bash scripts/validate.sh`: `All checks passed.`
- Limitation, as the plan notes: execution proves a test ran and passed, not that it asserts the right thing; W10's independent acceptance checks cover that.

## W05: require complete review and gauntlet evidence

Research:

- Expected lenses: ship selects lenses per diff and always includes `simplify` and, with a spec, `spec-conformance` (always true in a factory run); scope-review always runs `feasibility`, `completeness`, `consistency`, and `testability`.
- Findings flow: lens JSON results, `plan` numbers BLOCK and CONCERN findings, verifiers return CONFIRMED, PLAUSIBLE, or REFUTED per id, `aggregate` applied verdicts and merged by file:line, and the skill transcribed the result into `review_N.md` and `review_N.html`.
- Before: a missing verifier made a BLOCK a CONCERN, missing lenses only filled `failedLenses`, the verdict ignored both, and the runner parsed only the Markdown `Verdict:` line.
- PLAUSIBLE versus missing: PLAUSIBLE is a verifier's explicit uncertainty after checking the repository, a legitimate verified outcome; a missing verifier is an execution failure that says nothing about the finding.
- Gauntlet applicability: `dependency-rules` is legitimately skipped when `docs/dependencies.md` is absent (objective); any other skip needs a repository-grounded reason, and an unavailable tool or failed run is not a reason.

Decision:

- `aggregate-findings.py aggregate` writes the `factory.review/1` record (`--revision`, `--diff`, `--out`, `--kind`): strictly validated lens and verifier results, invalid results named per task, `completeness` separate from `verdict`, no verdict unless every expected lens returned a valid result and every BLOCK and CONCERN has a verifier outcome, a missing verifier never demoting anything, PLAUSIBLE blocks kept as uncertain concerns, REFUTED findings kept in `refuted` for audit, and per-task prompt and result hashes.
- Batches use `prompts/{task}.md` and `results/{task}.json`; `pending` lists only tasks with no result, an invalid result, or a result recorded for a prompt that changed, and the orchestration reference allows one rerun of exactly those; `render` prints the Markdown verdict sections from the record.
- `load_review` in the shared records module rejects incomplete records, missing mandatory lenses, and verdicts that do not follow from the findings.
- Scope-review's gate requires `spec-review_N.json` with all four lenses; ship's gate requires `review_N.json` with `simplify` and `spec-conformance`, at the current HEAD, and a Markdown verdict equal to the record's.
- Ship writes `gauntlet.json` (`factory.gauntlet/1`) with all eight checks; the gate re-executes each applicable check's command (a contract-pinned command wins) through the guarded executor on the final revision, requires success and no surviving violations, and accepts inapplicability only from the contract or the objective dependency-rules rule.

Evidence:

- `runner/tests/test_review.py`: `test_r5_missing_lenses_make_the_review_incomplete_with_no_verdict`, `test_a_missing_verifier_never_demotes_a_blocker`, `test_verifier_outcomes_decide_the_documented_verdicts` (BLOCK, CONCERNS, PASS), `test_malformed_and_duplicate_results_are_rejected_by_task`, `test_pending_reruns_only_missing_invalid_or_stale_tasks`, `test_render_transcribes_the_record`.
- `test_gates.ReviewEvidenceTests`: `test_r5_an_incomplete_review_cannot_make_the_run_ready`, `test_markdown_must_transcribe_the_record`, `test_a_missing_or_unjustified_gauntlet_blocks`, `test_surviving_violations_and_failing_checks_block`, `test_a_pinned_contract_command_wins_over_the_agent`, `test_the_dependency_rule_is_inapplicable_only_without_its_rules_file`; `test_gates.ScopeReviewGateTests.test_an_incomplete_spec_review_panel_fails`.
- `test_worker.ReviewCompletenessTests.test_an_incomplete_review_or_a_missing_gauntlet_never_reaches_done` (full runs cancel at ship after exhausting retries, never done).
- `bash scripts/validate.sh`: `All checks passed.`

## W03: verify the exact final PR revision and published evidence

Research:

- `gh pr view --json` exposes `headRefOid`, `headRefName`, `baseRefName`, `body`, `isDraft`, `state`, `url`, and `isCrossRepository` (checked against `gh pr view --help` field list on gh 2.94.0); `gh pr checks --json name,state,bucket` reports the checks of the PR's current head, with buckets `pass`, `fail`, `pending`, `skipping`, and `cancel`.
- Right after a push, `gh pr checks` can report no checks at all until workflows are queued, so an empty list is not evidence of "no CI"; which checks are required is repository configuration, now declared as `ci.required_checks` in the contract (W11).
- Before: `wt.unpushed` fetched with `check=False`, so a failed fetch compared against a stale remote-tracking ref, and it counted only local commits missing remotely, so a remote that was ahead or had diverged still read as synchronized.

Decision:

- The ship gate verifies one candidate in a bounded loop (three rounds): read the PR; require a same-repository PR (`isCrossRepository` false and the PR URL's repository matching the remote, or `gh repo view` for a non-GitHub remote URL), the run's base, and the run branch; fetch the branch explicitly and fail on any fetch error; require local HEAD, the refreshed remote branch, and the PR head to be one commit, naming unpushed, behind, or diverged states precisely.
- The published body's Evidence section must equal `pr.md`'s, and `pr-evidence.py check` runs on the published body.
- The review record and gauntlet record must name that revision; the gauntlet commands, the W02 scenario evidence (tests, `[repro]`, e2e driver), and contract validation all run again on it; afterwards HEAD must be unchanged and the tracked tree clean.
- Required checks are waited for until they appear and finish under the attempt deadline; failing and cancelled checks fail, a skipped required check needs `ci.allow_skipped`, unknown states fail, and no checks at all passes only with `ci.none`.
- While polling, and once more before recording completion, the PR is re-read; a moved head, a draft, a closed PR, or changed Evidence restarts verification, and the restarts are kept in the gate data.
- A passing gate stores `completion` (observation time, revision, PR number, base, head ref and SHA, Evidence hash, checks, restarts) in `run.json`, so later PR changes cannot rewrite what satisfied completion.
- The GitHub stub now models revision-bound state: live `headRefOid` from the remote, stored bodies from `--body-file`, delayed check appearance, and a configurable repository name.

Evidence (`test_gates.ShipGateTests`):

- `test_r7_a_change_after_review_and_evidence_cannot_ship_on_the_old_verification` (the old review is stale; a fresh review of the changed revision still fails because the runner's re-run of the mapped test catches the regression).
- `test_wrong_repository_or_base_is_not_this_run`, `test_remote_ahead_diverged_unpushed_and_failed_refresh_are_not_synchronized`, `test_evidence_must_be_the_published_evidence`.
- `test_expected_checks_that_have_not_appeared_yet_are_waited_for`, `test_pending_checks_poll_until_green`, `test_pending_checks_at_deadline`, `test_failing_checks`, `test_a_skipped_required_check_needs_explicit_permission`, `test_a_repository_without_ci_can_complete_only_when_its_contract_says_so`.
- `test_a_head_that_moves_while_checks_are_pending_restarts_verification`, `test_ready_pass_verifies_the_final_candidate_and_records_the_observation`; `test_worker.HappyPathTests.test_full_happy_path` checks the recorded completion revision.
- Suite: `Ran 298 tests OK (skipped=1)`; `Factory runner e2e passed.`
- Not yet retained: a real-host PR-state receipt; the plan places it in W10's real-host campaign.

## W10: add behavioral factory evals and platform coverage

Research:

- The existing evals are comprehension questions; the runner tests use stub hosts with scripted outcomes; neither grades the produced code independently.
- Supported platforms: the runner uses `fcntl` flock, POSIX sessions, and `sandbox-exec` (macOS) or `bwrap` (Linux); `factory doctor` declares Python 3.10 as the minimum.
- Local environments available: macOS 15 with Python 3.10.17 (uv), 3.13, and 3.14; Docker with `python:3.10-slim` (Debian) for Linux.

Decision:

- `factory/evals/bench/` holds a versioned corpus (`manifest.json`, `factory.bench/1`): `webhook-dedup` (bug fix with `[repro]`), `webhook-dedup-interrupted` (a worker crash injected at build), `webhook-stats` (feature), `retention-ambiguous` (auto-decided requirement), `payment-credentials` (dependency problem: missing credentials), and `dashboard-ui` (real hosts only).
- Every task has its request, the spec the harness seals as the approved intent, and `acceptance/` tests that stay outside the target repository and run against a `git archive` of the produced revision.
- `bench.py run --mode offline` scripts the stub hosts: the correct behavior for every task, and for the bug fix five negative controls (broken implementation; inert tests with a fabricated driver; agent-written evidence; a missing review; review and gauntlet records made stale by a later commit).
  `--mode real` needs a config naming a sandbox remote, `max_tokens_per_run`, and `max_runs`, and refuses to exceed them.
  `grade` scores a run a person scoped; results say `scope: seeded` or `scope: interactive`.
- Each result keeps the task version, variant, role, mode, status, acceptance output, revision, attempts with codes and timings, interventions, fault and resumes, auto-decisions, conditions, tokens, runtime identities, and retained run records.
- `bench.py report` gives task success, false-green rate, negative controls rejected, intervention rate, recovery success, per-task variance, resource totals, and the runner and skills identities, each rate with numerator and denominator, optionally against a baseline.
- `scripts/validate.sh` check F04 runs the offline corpus and fails on any false green, accepted negative control, or failed correct task; runner test output now goes to a kept log directory with failing tests printed instead of being discarded.
- CI runs on `ubuntu-latest` and `macos-latest` with Python 3.10 and 3.13, weekly as well as on pushes, and uploads the logs.

Evidence:

- Offline corpus (macOS, Python 3.14): 10 runs, task success 5/5 after fixing auto-decision grading, false green 0/5, negative controls rejected 5/5, recovery 1/1, one intended intervention (the credentials task); 37 seconds.
- Linux (`python:3.10-slim` container): offline corpus identical (report exit 0), `Factory runner e2e passed.`, runner suite 296/298 on the first run; both failures fixed (a doctor assertion assumed a sandbox tool Linux lacks; a pause test did not reproduce in isolation or in two repeated Linux runs of the control, watch, and worker modules).
- macOS with Python 3.10.17: `Ran 298 tests OK (skipped=1)`.
- R1-R8 regression tests (all in the fast suite): R1 `test_gates.BuildGateTests.test_r1_...` and `test_worker.EvidenceTests.test_r1_...`; R2 `test_worker.ParkConditionTests.test_r2_...`; R3 `test_gates.BoundaryTests.test_r3_...` and `test_worker.BoundaryTests.test_r3_...`; R4 `test_records.E2eRecordTests.test_r4_...`; R5 `test_review.AggregatorTests.test_r5_...` and `test_gates.ReviewEvidenceTests.test_r5_...`; R6 `test_gates.ValidationTests.test_r6_...`; R7 `test_gates.ShipGateTests.test_r7_...`; R8 `test_recovery.SingleExecutorTests.test_r8_...`.
- Not run: the real-host campaign (at least one uninterrupted headless run and one interruption and recovery run, with a baseline comparison). It spends model tokens and needs a sandbox GitHub repository, so it waits for the operator's go-ahead; GitHub Actions results for the new matrix exist only once the workflow runs remotely.

## W12: budget nested work and measure actual usage

Research (real host records and binaries):

- A recorded Codex `turn.completed` event carries `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, and `reasoning_output_tokens`; in the recorded ship attempt `cached_input_tokens` (20,497,239) and `cache_write_input_tokens` (314,814) fit inside `input_tokens` (20,818,437) and `reasoning_output_tokens` (18,240) inside `output_tokens` (62,441), so they are parts, not additions.
- Sub-agents appear as `collab_tool_call` items (`spawn_agent`, `wait`, `close_agent`: 38 spawns started, 27 completed, 11 failed in that attempt) with no usage fields, and the stream has a single `turn.completed`, so child usage is not observable separately.
- Codex 0.154.0 has enforceable settings: `codex sandbox -c agents.max_concurrent_threads_per_session=0` fails with `agents.max_concurrent_threads_per_session must be at least 1`, and `=3` with `agents.max_depth=1` is accepted, so the cap is enforced by the host rather than by prompt text.
- Shared resources for concurrent runs: runner-executed test suites, e2e drivers with services, and fixed ports; parallel build children each running the broad suite on one mutable worktree.

Decision:

- Usage per attempt by category (`input`, `cached_input`, `cache_write_input`, `uncached_input`, `output`, `reasoning_output`, `total`), unreported usage as unknowns, `child_usage` and scope usage as `unknown`, child agent counts, and a run total; `tokens_total` stays the raw input plus output for compatibility.
- Cost only from an explicit `~/.factory/pricing.json` (`factory.pricing/1`), pricing cached and cache-write input as parts of input; otherwise the estimate is unknown with the reason.
- Attempts record queue, agent, gate, verification, and lease-wait seconds; `factory show`, the watch footer, and the dashboard summary show cached usage and waits.
- New settings: `max_child_agents` and `max_agent_depth` passed to Codex as the agents settings and recorded on the attempt with their enforcement; `max_heavy_commands` leases for runner-executed tests, e2e, validation, gauntlet, and setup, inherited by the guard so a worker crash cannot release them early; `port_range` leases for e2e services plus a per-execution `FACTORY_SERVICE_DATA` directory.
- Build's parallel reference: children run only their own change set's tests and start no subagents; the orchestrator runs the broad validation once after the wave settles.
- No-progress stop: each attempt records a fingerprint of HEAD and its plan files; a retry that fails with the same code and fingerprint parks with that evidence instead of spending the rest of the budget.
- In-flight ceiling: the attempt's guard tails the Codex stream and stops the agent once reported usage crosses the remaining ceiling, parking with `budget.tokens`; the reason says enforcement happens at turn boundaries because usage arrives with `turn.completed`.

Evidence (`runner/tests/test_resources.py`):

- `test_categories_are_parts_of_their_totals_and_child_usage_stays_unknown`, `test_the_recorded_real_ship_attempt_is_not_priced_as_uncached_input` (32,536,837 tokens shown as 31.8M cached input and 638K uncached input; priced cached input is far below an all-uncached price).
- `test_show_displays_categories_unknowns_and_durations`.
- `test_four_concurrent_runs_obey_nested_agent_and_heavy_command_limits` (four concurrent runs, peak one heavy command, every Codex invocation capped at two sub-agents).
- `test_a_heavy_lease_survives_worker_death_and_is_released_after_the_command`, `test_concurrent_worktrees_get_distinct_ports_and_service_data`.
- `test_no_progress_stops_retries_with_the_evidence`, `test_a_changing_retry_is_not_stopped_as_no_progress`, `test_the_token_ceiling_stops_an_attempt_at_its_turn_boundary`.
- The no-progress stop changed three existing tests that relied on identical retries to exhaust the budget; their stubs now vary each attempt's review like a real re-review, and a test with a 1-second deadline for every attempt got a realistic one.
- Suite: 307 tests, the one failure fixed and stable across three repeated runs.

## Record ownership (Phase 0 follow-up)

| Record | Written by | Agents may |
| --- | --- | --- |
| `run.json`, `events.jsonl`, `outcomes.jsonl` (operator CLI only) | worker (single writer); `outcome` appends only | never |
| `intent/` sealed snapshot, `checkpoints/`, `attempts/*/{receipt,runtime,gate,outputs,subphase}.json`, `gate/` receipts | runner | never |
| published `reports/{plan}-e2e.json`, test results, gauntlet and validation receipts used for completion | runner, from its own executions | never; agent copies are inputs only |
| `.dev/{plan}/` spec, scenario map, notes, review and gauntlet records, `pr.md`, `{stage}-result.json` | agent | write; the gate validates them strictly and never treats them as completion |
| PR body Evidence, push, PR creation | agent or, when its analysis is complete at head, the runner (`runner/publication.py`) | write; completion reads the live PR state |

## W13: checkpoint expensive subphases

Research:

- Traced the successful ship (`happy_scenario`) and the recorded real blocked ship (`ship-1`: 23 minutes, 20.9M tokens, blocked only because `gh pr create` returned HTTP 401 after the review, gauntlet, and push succeeded).
- Model judgment: review lenses, verification of findings, remediation, the PR text. Deterministic: mapped tests, repro, the e2e driver, validation and gauntlet commands, push, PR create/edit, CI observation.

Decision:

- `runner/checkpoints.py` (`factory.checkpoint/1`) with the invalidation table in its docstring; checkpoints under `{run}/checkpoints/` record the input fingerprint (tree, contract fields, command, runner content hash), revision, output hashes, and the receipt.
- Reuse needs an identical fingerprint and unchanged outputs; every decision, reused or computed with the reason, is recorded on the attempt (`checkpoints`) and live in `subphase.json`.
- `runner/publication.py`: when the agent's review and gauntlet records are complete at head and `pr.md` exists, the ship gate pushes, finds an existing branch PR before creating one, and edits the body, with a stable operation id, three tries, and backoff; operations land in `run.json` `operations`, separate from the paid retry budget.
- CI observation stays the existing bounded runner poll in `verify_candidate`; review panels are not checkpointed by the runner because they are model work, and the ship aggregator's `pending` already reruns only missing, invalid, or stale lens tasks (W05).

Evidence (`runner/tests/test_checkpoints.py`):

- `test_a_transient_publication_failure_is_retried_by_the_runner_without_another_model_attempt`: one ship Codex exec, 0 paid retries, `pr-create done after 2 tries`, one PR; `factory show` prints `retries    0/5 paid; 1 deterministic operation retry (not charged)`.
- `test_a_crash_after_the_pr_is_created_never_creates_a_second_one`: `FACTORY_FAULT=gate.after_pr_create` with a create whose response was lost; after resume, one PR and one ship exec.
- `test_ship_reuses_build_verification_for_unchanged_code_and_recomputes_after_a_code_change`: tests, e2e, and validation `reused` at ship; after a ship commit, `computed: inputs changed (tree)`.
- `test_tool_contract_and_output_changes_invalidate_exactly_their_checkpoints`: runner change invalidates all, contract command change only that validation, an altered published e2e record and a corrupt checkpoint recompute with their reasons.
- Two W12 concurrency tests now expect ship to reuse build's validation and e2e on the unchanged tree (8 heavy commands instead of 16).

## W14: exports and post-PR outcomes

Research:

- Destination: the runs hold private repository evidence, so the bundle stays on the operator's machine under `~/.factory/exports/{run}/` (never touched by `gc`), and the PR carries a comment with the run id, completion revision, file count, and manifest SHA-256, so a reviewer can verify any copy they are handed; uploading to an external store is left to the operator's access model.
- `gh pr view --json state,mergedAt,headRefOid,reviewDecision` gives merged, closed, open, and review decision; rework, rejection, regression, intervention causes, and active operator time need explicit annotation.
- Retention: exports are kept until the operator deletes them; raw host streams (`stdout.jsonl`) are excluded because they are large and may hold unredacted tool output.

Decision and evidence (`runner/outcomes.py`, `factory export|outcome|report`, `runner/tests/test_outcomes.py`, `test_compat.py`):

- `test_an_export_is_verifiable_elsewhere_and_survives_gc`: bundle copied elsewhere verifies, a tampered file fails with `run.json: checksum mismatch`, the PR comment carries the digest, no `.dev/` is committed, and after `gc` removes the run the export still verifies.
- `test_outcomes_append_observations_without_erasing_completion`: open, then annotation, then merged with a moved head; `run.json` completion is unchanged; `report` prints every rate as count/n and operator time only from measured annotations.
- Feedback link: the recorded real ship failure (PR creation blocked after complete analysis) is now the regression `PublicationTests` in `runner/tests/test_checkpoints.py`, named in the README's real-run notes; `factory outcome --eval-case` links later reviewer corrections to their case.
- README real-run notes now come from the retained run's `run.json`: ship-1 blocked, ship-2 done with 1 of 5 retries, labeled a historical observation not reverified, with scope time labeled elapsed wall time.

## Section 10: compatibility

- `run.json` stays schema 1 (no persisted semantics of existing fields changed; new fields are additive); new records carry their own versions and newer versions are refused (`test_newer_schema_is_rejected`, `test_a_newer_checkpoint_version_is_recomputed_not_trusted`).
- No migration rewrites a run, so there is no interrupted migration or active-worker window; the inspection commands never write `run.json` (`test_a_legacy_done_run_is_inspectable_exportable_and_reported_as_historical`, which also shows the legacy run counted as historical in `report`).
- A resumed legacy run parks with `intent.missing` and the `--rescope` repair instead of reusing unverifiable intent (`test_resuming_a_legacy_run_refuses_unverifiable_intent_with_a_repair`); legacy strict-JSON e2e reports stay inspectable and are never evidence (`test_legacy_strict_json_report_stays_inspectable`).
- The retained real run from 2026-09-15 renders with `factory show` under the current runner, with usage categories shown as not recorded.
- Plugin parity: `build_codex_plugin.py --check` in `validate.sh`, and preflight refuses a Codex skill cache that differs from the generated skills.

## Final verification (2026-09-16)

- macOS, Python 3.13: `bash scripts/validate.sh` prints `All checks passed.` (316 runner tests, offline CLI E2E, bench F04, plugin parity).
- Linux container `python:3.12-slim` with git only: 316 tests OK (3 skipped).
  A first container pass exposed that the R8 process counter used `ps`, which minimal images lack, so its sampling thread died and reported 0 hosts; it now reads `/proc` where present and asserts that it sampled.
  `test_help_verbose_and_dashboard_keys` and `test_tty_view_redraws_stage_rows_in_place` failed once in that loaded first pass and did not reproduce in isolation, in their modules, or in the full rerun.
- Platform matrix, run locally: macOS Python 3.10.17 (316 OK), Linux `python:3.10-slim`, `3.12-slim`, and `3.13-slim` (316 OK each after the fix below), macOS Python 3.13 through `validate.sh`.
- The watch flake reproduced on Linux 3.10 under load and was a real viewer bug: when an attempt ended between two polls, `watch` replaced its stream tail without reading the attempt's final lines, so `v` never showed them.
  It now reads the finished attempt's file once more before letting go and keeps the last activity line; `test_the_last_lines_of_an_attempt_that_ends_between_polls_are_still_shown` fails without the fix (`'| $ git add -A' not found`) and passes with it.
- Still open, needing the operator: the real-host campaign (paid Codex and a sandbox GitHub repository) and the remote CI matrix run (ubuntu/macos x Python 3.10/3.13), which closes the W10 real-host box and the W10 final-checklist item; the remote matrix repeats the local platform evidence.
  The installed Codex plugin cache is stale against this checkout and must be reinstalled before that campaign.

## Operator waiver (2026-09-16)

- The operator waived the real-host campaign (one uninterrupted headless run and one interruption and recovery run with real Codex and GitHub): "Skip the testing, we can assume it works else we will patch later".
- The W10 real-host box and the W10 final-checklist item are ticked as waived, not as verified; no real-host evidence exists for the new runner, and the remote CI matrix has not run.
- Before relying on it, reinstall the Codex plugin cache from this checkout, since preflight refuses the stale one.
