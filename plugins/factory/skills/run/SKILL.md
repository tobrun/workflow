---
name: run
description: Take a request through an interactive scope, then run an unattended scope-review, build, and ship as phases, judging each phase's completion itself and repairing or relaunching it on failure, ending in a pull request or a report. Use to run the factory end to end on Claude Code or Codex.
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

No request and a run branch checked out: resume that plan.
No request and no branch: resume the single state file under `.dev/` not marked done.
A request given (quoted text or a Markdown file path): derive a kebab-case plan slug from it, create `.dev/{plan}/` with `request.md` holding the request, then run `python3 {run-skill-root}/scripts/run-state.py init {plan} --request {request} --base {default-branch}` (D-plan-slug, D-request-input).

## 3. Scope, inline

<!-- interactive-only -->
Launch the copied `scope` skill inline, in this session: read its `SKILL.md` at the absolute path `{run-skill-root}/../scope/SKILL.md`, telling it to read `{scope-skill-root}` as that path's parent directory. It keeps its interview and ends with one explicit go question you ask the user.
<!-- /interactive-only -->

## 4. Handoff

At the go: create branch `factory/{plan}` from the base branch. Run `run-state.py handoff {plan} --branch factory/{plan}` to record the spec's sha256, its scenario texts per change set, its `⊘` lines, and the checkout's dirty files (D-checkout, D-handoff-seal).

## 5. Per phase: launch, judge, act

For each phase in order (`scope-review`, `build`, `ship`):

1. Launch one fresh-context subagent per [references/launch.md](references/launch.md)'s prompt template, naming the phase's skill path, the plan, the attempt number, and (on a relaunch) the previous reason with explicit guidance. Record the launch with `run-state.py attempt {plan} {phase}` so a crashed attempt is distinguishable from an unstarted one on resume.
2. Wait for the host's completion signal - a batch in flight is not over.
3. Run `run-state.py check-result .dev/{plan}/results/{phase}-{attempt}.json`; a missing or unparseable file is a failed attempt with that reason (its own exit code, never a crash). Close the attempt with `run-state.py attempt {plan} {phase} --status {done|failed|stopped} --result .dev/{plan}/results/{phase}-{attempt}.json`.
4. Run the phase's evidence checks from [references/judgment.md](references/judgment.md).
5. A `stopped` result with kind `secret.found` or `action.destructive` ends the run immediately; no decision overrides it. The same two rules bind your own repairs: never rewrite history, force-push, or delete outside the run branch.
6. Record the decision with `run-state.py record {plan}` before acting on it, then act per the failure ladder in [references/judgment.md](references/judgment.md): advance, repair it yourself and re-check, relaunch with guidance, or end the run after the third accepted failure.
7. Before advancing, run `run-state.py diff-spec {plan}`; a dropped or reworded approved scenario or `⊘` line is evidence for the next judgment, not an automatic block.

## 6. Closing report

Print: phases, attempts, decisions, repairs, and the PR link or the exact action a person must take. Read only result files, check outputs, `git log`, and the state file for this - never a subagent transcript, the full spec, or implementation notes unless a judgment needs a specific section (D-orchestrator-context).
