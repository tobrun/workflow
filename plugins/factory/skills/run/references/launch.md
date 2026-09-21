# Launch

The host transport ladder for one phase agent, and the exact prompt template. Mirrors ship's `orchestration.md` native-transport rung, one agent at a time instead of a batch.

## Transport ladder

1. **Claude Code**: the Agent tool.
2. **Codex**: `spawn_agent`.
3. **opencode**: the `task` tool.
4. **Unavailable**: stop before the interview - nobody pays for a scope that cannot run.

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
