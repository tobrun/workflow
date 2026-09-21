---
name: run
description: Take a request through an interactive scope, then run an unattended scope-review, build, and ship as phases, judging each phase's completion itself and repairing or relaunching it on failure, ending in a pull request or a report. Use to run the factory end to end on Claude Code or Codex.
disable-model-invocation: true
---

# Run

You are the orchestrator. You launch phase subagents, judge their results against evidence, repair or relaunch on failure, and end in a pull request or a report. You never implement a phase yourself and never let a phase invoke another. `.dev/{plan}/factory-run.json` is yours alone to write; every other file is a phase's.

## 1. Preflight

Confirm the host has a subagent tool per the transport ladder in [references/launch.md](references/launch.md); with none, stop before the interview so nobody pays for a scope that cannot run.
Check that `.dev/` is git-ignored in the consuming repository (`git check-ignore -q .dev/`); when it is not, append the entry to `.gitignore` yourself, and once the run branch exists at handoff, record the write as a repair with the check output as evidence (D-plan-files-ignored). The two hard stops in section 5 bind this write too.
Resolve your own base directory from the host's injected "Base directory for this skill" line.

<!-- interactive-only -->
If two unfinished state files exist under `.dev/` with no branch match, or a recorded run branch no longer exists, list the candidates (plan, branch, last phase) or name the missing branch and ask which to resume - a person is at the keyboard invoking `/factory:run`, so this is not a post-go touchpoint.
<!-- /interactive-only -->

## 2. Init or resume

A request given (quoted text or a Markdown file path): derive a kebab-case plan slug from it, create `.dev/{plan}/` with `request.md` holding the request, then run `python3 {run-skill-root}/scripts/run-state.py init {plan} --request {request} --base {default-branch}` (D-plan-slug, D-request-input).
No request and a run branch checked out: resume that plan.
No request and no branch: resume the single unfinished state file under `.dev/`. A run is finished when its `decisions` list ends in an `end` action, or in an `advance` on `ship`; nothing else marks a run finished, so every other state file is unfinished.

Resuming, launch nothing before reading the history. Run `run-state.py show {plan}`: it prints each phase's attempts in order with their statuses, and those numbers are the run's only attempt numbers. Take the highest-numbered attempt of the earliest phase you have not accepted as `done`, and act on its status:

- `launched`, and `.dev/{plan}/results/{phase}-{attempt}.json` exists for that attempt's own number: the phase finished and the session died before its result was read. Pick section 5 up at step 4, `check-result` on that exact path.
- `launched`, and no result file at that attempt's own number: the attempt died mid-phase. Close it with `run-state.py attempt {plan} {phase} --status failed` and no `--result`, so it is a failure with the reason "no result file" and no other attempt's result is read into it. Then take a new attempt from section 5 step 1, its guidance saying to start from the artifacts already on disk and not redo finished work.
- `done`, `failed`, or `stopped`: the attempt is closed already. Pick section 5 up at step 5 for it.

A result file belongs to the attempt number in its name and to no other. Never infer an attempt number from what is in `results/`: after a crash, the newest file there is the last attempt that finished, not the one that was running.

## 3. Scope, inline

<!-- interactive-only -->
Launch the copied `scope` skill inline, in this session: read its `SKILL.md` at the absolute path `{run-skill-root}/../scope/SKILL.md`, telling it to read `{scope-skill-root}` as that path's parent directory. It keeps its interview and ends with one explicit go question you ask the user.
<!-- /interactive-only -->

## 4. Handoff

At the go: create branch `factory/{plan}` from the base branch. Run `run-state.py handoff {plan} --branch factory/{plan} --seal .dev/{plan}/spec.md` to record the spec's sha256, its scenario texts per change set, its `⊘` lines, and the checkout's dirty files (D-checkout, D-handoff-seal).

## 5. Per phase: launch, judge, act

For each phase in order (`scope-review`, `build`, `ship`):

1. Take the attempt number first: `run-state.py attempt {plan} {phase}` appends a `launched` entry and prints `{phase} attempt {N}: launched`. That `N` is the attempt number for the launch prompt, the result path, and the close below. The state file owns it alone - never count attempts yourself and never invent one.
2. Launch one fresh-context subagent per [references/launch.md](references/launch.md)'s prompt template, naming the phase's skill path, the plan, attempt `N`, and (on a relaunch) the previous reason with explicit guidance. The record is written before the launch on purpose: the launch call does not return until the phase is over, so an entry written after it would never exist for the crash it is there to expose.
3. Wait for the host's completion signal - a batch in flight is not over.
4. Run `run-state.py check-result .dev/{plan}/results/{phase}-{attempt}.json` with `{attempt}` = `N`; a missing or unparseable file is a failed attempt with that reason (exit 1 like any other failure, never a crash; exit 3 is a bad call, not a phase outcome). Close the attempt with `run-state.py attempt {plan} {phase} --status {done|failed|stopped} --result .dev/{plan}/results/{phase}-{attempt}.json`, the same path.
5. Run the phase's evidence checks from [references/judgment.md](references/judgment.md).
6. A `stopped` result with kind `secret.found` or `action.destructive` ends the run immediately; no decision overrides it. The same two rules bind your own repairs: never rewrite history, force-push, or delete outside the run branch.
7. Record the decision with `run-state.py record {plan}` before acting on it, then act per the failure ladder in [references/judgment.md](references/judgment.md): advance, repair it yourself and re-check, relaunch with guidance, or end the run after the third accepted failure.
8. Before advancing, run `run-state.py diff-spec {plan}`; a dropped or reworded approved scenario or `⊘` line is evidence for the next judgment, not an automatic block.

## 6. Closing report

The last decision is recorded before the report is printed - the `end`, or the `advance` that accepts `ship` - because that entry is the only thing that marks the run finished for a later resume.
Print: phases, attempts, decisions, repairs, and the PR link or the exact action a person must take. Read only result files, check outputs, `git log`, and the state file for this - never a subagent transcript, the full spec, or implementation notes unless a judgment needs a specific section (D-orchestrator-context).
