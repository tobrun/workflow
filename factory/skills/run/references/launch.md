# Launch

The host transport ladder for one phase agent, and the exact prompt template. Mirrors ship's `orchestration.md` native-transport rung, one agent at a time instead of a batch.

## Transport ladder

1. **Claude Code**: the Agent tool.
2. **Codex**: `spawn_agent`.
3. **opencode**: the `task` tool.
4. **Unavailable**: stop before the interview - nobody pays for a scope that cannot run.

## Prompt template

```
You implement exactly one factory phase. Read {phase}-skill-root/SKILL.md at
the absolute path {skill_path}; treat {skill_path}'s parent directory as
{phase}-skill-root when the file uses that placeholder. Follow it exactly.

Plan: {plan}, at .dev/{plan}/ (absolute: {plan_dir}).
Attempt: {attempt}, the number run-state.py just printed for this launch.
{On a relaunch only: The previous attempt failed with reason "{reason}".
Guidance: {what to do differently, never "try again"}.}

Scratch root for this attempt: /tmp/{project-slug}/factory/{plan}/{phase}-{attempt}/.
Write your result to .dev/{plan}/results/{phase}-{attempt}.json as your last
action, per the schema in factory-run.md, echoing skill_path back exactly as
given above.

This phase never launches another phase, and after the go it never asks a
person anything - decide under the unattended policy in factory-run.md and
record the decision in auto_decided.
```
