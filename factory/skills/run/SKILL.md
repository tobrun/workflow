---
name: run
description: Drive the pipeline declared in .factory/config.yaml, or the built-in scope, scope-review, build and ship when there is none - the interactive phases inline, then every other phase as an unattended subagent, judging each against its type's declared contract and repairing or relaunching on failure, ending in a pull request or a report. Use to run the factory end to end on Claude Code or Codex.
disable-model-invocation: true
---

# Run

You are the orchestrator. You launch phase subagents, judge their results against evidence, repair or relaunch on failure, and end in a pull request or a report. You never implement a phase yourself and never let a phase launch another. `.dev/{plan}/factory-run.json` is yours alone to write; every other file is a phase's.
The pipeline is data: an ordered list of typed phases in `.factory/config.yaml`, or the built-in default (`scope`, `scope-review`, `build`, `ship`) when that file is absent. You never infer what a phase means - everything you do with one comes from its type's declared axes, read via `factory-config.py show --resolved` ([references/pipeline-config.md](../../references/pipeline-config.md)).
`${plugin_root}` is `{run-skill-root}/../..`, and `factory-config.py` resolves it from its own location, the same directory.

## 1. Preflight

Confirm the host has a subagent tool per the transport ladder in [references/launch.md](references/launch.md); with none, stop before the interview so nobody pays for a scope that cannot run.
Run `python3 {run-skill-root}/../../scripts/factory-config.py check`. It is the single validator, so judge nothing about the file yourself: on exit 1 print its findings verbatim and stop before the interview. When a finding says a skill path does not resolve, also name the command that installs the defaults, `python3 {run-skill-root}/../../scripts/factory-config.py inject`.
Check that `.dev/` is git-ignored in the consuming repository (`git check-ignore -q .dev/`); when it is not, append the entry to `.gitignore` yourself, and once the run branch exists at handoff, record the write as a repair with the check output as evidence (D-plan-files-ignored). The two hard stops in section 5 bind this write too.
Resolve your own base directory from the host's injected "Base directory for this skill" line.

<!-- interactive-only -->
If two unfinished state files exist under `.dev/` with no branch match, or a recorded run branch no longer exists, list the candidates (plan, branch, last phase) or name the missing branch and ask which to resume - a person is at the keyboard invoking `/factory:run`, so this is not a post-go touchpoint.
<!-- /interactive-only -->

## 2. Init or resume

A request given (quoted text or a Markdown file path): derive a kebab-case plan slug from it and create `.dev/{plan}/` with `request.md` holding the request. Write `python3 {run-skill-root}/../../scripts/factory-config.py show --resolved --json` to `.dev/{plan}/pipeline.json`, read `show --resolved` for each phase's type, `attempts`, `checks`, `requires`, `seals`, class and skill path plus the `ceiling` and the `go`, then run `python3 {run-skill-root}/scripts/run-state.py init {plan} --request {request} --base {default-branch} --phases {ids in order, comma-separated} --attempts {phase}={n} ... --ceiling {ceiling}` (D-plan-slug, D-request-input).
No request and a run branch checked out: resume that plan.
No request and no branch: resume the single unfinished state file under `.dev/`. A run is finished when `run-state.py show {plan}` prints `finished: yes` - its `decisions` list ends in an `end` action, or in an `advance` on the last declared phase; nothing else marks a run finished, so every other state file is unfinished.

Resuming, launch nothing before reading the history. Write a fresh `show --resolved --json` to `.dev/{plan}/pipeline-now.json` and run `run-state.py diff-config {plan} --pipeline .dev/{plan}/pipeline-now.json` and `run-state.py diff-spec {plan}`; their output is evidence for the next judgment, not a block. Run `run-state.py show {plan}`: it prints each phase's attempts in order with their statuses, and those numbers are the run's only attempt numbers. Take the highest-numbered attempt of the earliest phase you have not accepted as `done`, and act on its status:

- `launched`, and `.dev/{plan}/results/{phase}-{attempt}.json` exists for that attempt's own number: the phase finished and the session died before its result was read. Pick section 5 up at step 4, `check-result` on that exact path.
- `launched`, and no result file at that attempt's own number: the attempt died mid-phase. Close it with `run-state.py attempt {plan} {phase} --status failed` and no `--result`, so it is a failure with the reason "no result file" and no other attempt's result is read into it. Then take a new attempt from section 5 step 1, its guidance saying to start from the artifacts already on disk and not redo finished work.
- `done`, `failed`, or `stopped`: the attempt is closed already. Pick section 5 up at step 5 for it.

A result file belongs to the attempt number in its name and to no other. Never infer an attempt number from what is in `results/`: after a crash, the newest file there is the last attempt that finished, not the one that was running.

## 3. The interactive prefix, inline

The phases whose type is `interactive` form a contiguous prefix of the pipeline; they run inline in this session, in order, because a subagent has no user-input tool.

<!-- interactive-only -->
For each one, bracket it the way a launched phase is: run `run-state.py attempt {plan} {phase} --type {type} --skill {skill path}` first, read its `SKILL.md` at its resolved skill path (telling it to read that path's parent directory as its skill root), let it keep its interview, and close it with `run-state.py attempt {plan} {phase} --status {done|failed} --result .dev/{plan}/results/{phase}-{attempt}.json`. The last phase of the prefix ends with one explicit go question you ask the user.
<!-- /interactive-only -->

A pipeline whose prefix is empty has no go: it branches and seals before its first launch.

## 4. Handoff

At the go - or before the first launch when there is no prefix - create branch `factory/{plan}` from the base branch. Run `run-state.py handoff {plan} --branch factory/{plan}` with one `--seal {path}` per path in the `seals` of each prefix phase (`${plan_dir}` is `.dev/{plan}`), plus `--config .factory/config.yaml --pipeline .dev/{plan}/pipeline.json`. It records each sealed file's hash and, for a spec in the dev notation, its scenario texts and `⊘` lines, the config hash with the resolved pipeline, and the checkout's dirty files (D-checkout, D-handoff-seal). With no `seals` it seals nothing and `diff-spec` says so.

## 5. Per phase: launch, judge, act

For each phase after the prefix, in declared order:

1. Take the attempt number first: `run-state.py attempt {plan} {phase} --type {type} --skill {skill path}` appends a `launched` entry and prints `{phase} attempt {N}: launched`. That `N` is the attempt number for the launch prompt, the result path, and the close below. The state file owns it alone - never count attempts yourself and never invent one.
2. Launch one fresh-context subagent per [references/launch.md](references/launch.md)'s prompt template, naming the phase's skill path, the plan, attempt `N`, and (on a relaunch) the previous reason with explicit guidance. The record is written before the launch on purpose: the launch call does not return until the phase is over, so an entry written after it would never exist for the crash it is there to expose.
3. Wait for the host's completion signal - a batch in flight is not over.
4. Run `run-state.py check-result .dev/{plan}/results/{phase}-{attempt}.json` with `{attempt}` = `N`; a missing or unparseable file is a failed attempt with that reason (exit 1 like any other failure, never a crash; exit 3 is a bad call, not a phase outcome). Close the attempt with `run-state.py attempt {plan} {phase} --status {done|failed|stopped} --result .dev/{plan}/results/{phase}-{attempt}.json`, the same path.
5. Judge the phase per [references/judgment.md](references/judgment.md): run its `checks`, then weigh its `requires`.
6. A `stopped` result with kind `secret.found` or `action.destructive` ends the run immediately; no decision overrides it. The same two rules bind your own repairs: never rewrite history, force-push, or delete outside the run branch.
7. Record the decision with `run-state.py record {plan}` before acting on it, then act per the failure ladder in [references/judgment.md](references/judgment.md): advance, repair it yourself and re-check, relaunch with guidance, or end the run. Follow the phase's budget and the run's ceiling as printed by `run-state.py show {plan}`; `attempt` never refuses one, so honouring it is yours.
8. Before advancing, run `run-state.py diff-spec {plan}` and `run-state.py diff-config {plan} --pipeline .dev/{plan}/pipeline-now.json`; a dropped or reworded sealed scenario, `⊘` line or phase is evidence for the next judgment, not an automatic block.

## 6. Closing report

The last decision is recorded before the report is printed - the `end`, or the `advance` that accepts the last declared phase - because that entry is the only thing that marks the run finished for a later resume.
Print, per phase: its type, the skill path that ran, its attempts with opened and closed times, and the decisions and repairs, then the PR link or the exact action a person must take. Read only the state file (`run-state.py show`), result files, check outputs and `git log` for this - never a subagent transcript, the full spec, or implementation notes unless a judgment needs a specific section (D-orchestrator-context).
