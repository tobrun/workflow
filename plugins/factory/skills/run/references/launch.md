# Launch

The host transport ladder for one phase agent, and the exact prompt template. Mirrors ship's `orchestration.md` native-transport rung, one agent at a time instead of a batch.

## Transport ladder

1. **Claude Code**: the Agent tool. Pass the phase's resolved `model` (`show --resolved`) as the tool's `model` parameter when the type declares one; omit the parameter when it does not, which inherits the session's model exactly as before this axis existed.
2. **Codex**: `spawn_agent`. Pass `model` the same way if the tool accepts one; if it does not, the value is simply unused and the subagent inherits the session's model - never work around this by any other means (a subprocess, a nested `codex exec`), since that is the per-host argv the runner was removed for.
3. **opencode**: the `task` tool. Same rule as Codex: pass `model` if the tool takes one, otherwise it is unused.
4. **Unavailable**: stop before the interview - nobody pays for a scope that cannot run.

A type's `model` is never valid on more than one host's catalog at once - `show --resolved` never validates it against the host you happen to be running on, and an unrecognized value is the launch call's own error, not a config finding. `scope`'s `interview` type - and any other `interactive` type - never reaches this ladder at all: it runs inline in the orchestrator's own session, so it has no launch call to carry a model to, and `factory-config.py check` refuses a `model` declared there.

## The wrapper

The template below wraps any phase skill, ours or a team's own, and imposes the result shape on it: the result file at `results/{phase}-{attempt}.json`, schema `factory.result/1`, written as the phase's last action. It imposes the shape, it does not guarantee it - a skill with strong opinions about its own output can defeat it, so a phase that writes nothing is read as a failed attempt, and the phase's declared `checks` verify the outcome independently of what the result says.

## Prompt template

```
You run exactly one factory phase. Read the skill at the absolute path
{skill_path}, and treat {skill_path}'s parent directory as the skill's own
root when the file uses a root placeholder. Follow it exactly.

Plan: {plan}, at .dev/{plan}/ (absolute: {plan_dir}).
Attempt: {attempt}, the number run-state.py just printed for this launch.
{On a relaunch only: The previous attempt failed with reason "{reason}".
Guidance: {what to do differently, never "try again"}.}

Scratch root for this attempt: /tmp/{project-slug}/factory/{plan}/{phase}-{attempt}/.
Write your result to .dev/{plan}/results/{phase}-{attempt}.json as your last
action: a JSON object with "schema": "factory.result/1", "phase", "status"
(done, failed or stopped), "reason" when not done, and skill_path echoed back
exactly as given above; the full schema is in factory-run.md.

Never name or launch the next phase. After the go you never ask a person
anything - decide under the unattended policy in factory-run.md and record
the decision in auto_decided.
```
