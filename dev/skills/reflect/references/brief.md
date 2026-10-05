# Brief

The handoff from `reflect` to `scope` for one thread.
It is self-contained: a person or a fresh session reading only this file can start `scope` in the workflow repository.

```markdown
# {thread}

Problem: {one sentence, in terms of what the runs did, not what the skill text says}

## Evidence

{claims show output condensed to one bullet per claim:}
- [c-0012] {claim} - "{quote}" ({repo}, {run id}, line {line})

Reach: {runs} runs across {repos} repositories, status {open|reopened}; {for a reopened thread, the commit that was meant to fix it}.

## Where the skill text is implicated

- `dev/skills/{skill}/SKILL.md` step {n}: "{the instruction the evidence contradicts or that let it happen}"

## Direction

{the smallest change that would have prevented the evidence; prefer a deterministic check the skill loops against over another line of prose, and say which one}

Not in scope: {what the evidence does not support changing}

## Eval case

Append to `dev/evals/{skill}.json`, built from the real run so the regression is the one that happened:

{"id": "{skill}-{thread-suffix}", "prompt": "...", "assertions": ["..."]}

## After the fix merges

/dev:reflect resolve {thread} {sha} {eval id}
```

Keep quotes as they are in the store; trim around them, never inside them.
When the evidence points at two skills, write one brief per skill and say so in each.
