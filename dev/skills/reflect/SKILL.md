---
name: reflect
description: Consolidate the run journal that scope, scope-review, build, ship, and their quick variants write at the end of every run into cited claims about how the skills themselves behaved, rank the recurring friction threads across repositories, and hand off one chosen thread as a scope brief with an eval case from the real run. Use periodically to decide what to improve in the dev workflow next, or with "resolve {thread} {sha}" once a fix for a thread has merged.
disable-model-invocation: true
---

# Reflect

Turn what past runs measured into evidence about the skills, so the next skill change fixes something that actually recurred.
The store is `~/.dev-workflow/memory/` (or `$DEV_MEMORY_DIR`), local to this machine and shared by every repository; [references/claims.md](references/claims.md) owns its records, the claim schema, and what makes a claim worth keeping.

`{reflect-skill-root}` is the directory holding this SKILL.md, `{claims}` is `python3 {reflect-skill-root}/../../scripts/claims.py`, and `{project-slug}` is the basename of the current repository root, or `workflow-memory` outside one.

Limits:
- Never edit a skill, a reference, or an eval. The output is a brief a person takes to `scope`; adaptation reaches a skill only through a reviewed commit.
- Never write `claims.jsonl`, `resolutions.jsonl`, or a derived file by hand; every change goes through `{claims}`, so the checks hold.
- A claim is about how a skill behaved, never about the consuming repository's code or its people.
- Nothing is committed, pushed, or published unless the user asks.

## Resolve mode

When invoked as `resolve {thread} {sha} [eval-id]`, run `{claims} resolve {thread} --commit {sha} [--eval {eval-id}]`, print `{claims} top --all`, and stop.
A later claim in that thread reopens it, which is how a fix that did not hold shows up.

## 1. Gather

Create `/tmp/{project-slug}/reflect/`, then run `{claims} pending > /tmp/{project-slug}/reflect/pending.json` and `{claims} top --all`.
With no pending runs, skip to step 4; with no pending runs and no claims at all, say the journal is empty, name the four skills that write it, and stop.

## 2. Extract

Launch one read-only subagent per batch of up to five pending runs, all in one message.
Hand each the batch's journal entries, the current thread list from `{claims} top --all`, and [references/claims.md](references/claims.md), and have it write candidate claims, one JSON object per line, to `/tmp/{project-slug}/reflect/candidates-{n}.jsonl`.
Each agent reads the transcript only around the lines its signals cite, never a whole transcript, and reuses an existing thread slug whenever the claim is the same problem.
An agent may write zero candidates; a run that went as designed is not friction.

## 3. Check loop

Concatenate the candidate files into `/tmp/{project-slug}/reflect/candidates.jsonl` and loop until it exits 0:

```bash
{claims} add /tmp/{project-slug}/reflect/candidates.jsonl
```

It adds nothing while any candidate fails, and each error names the candidate.
Fix that candidate's quote, source, or shape against the transcript, or drop it; never bend a quote toward a claim the evidence does not make.
Then run `{claims} mark` with every pending run id, including runs that produced no claims.

## 4. Present

Run `{claims} report /tmp/{project-slug}/reports/workflow-memory.html` and open it per [../../references/reporting.md](../../references/reporting.md).
Show `{claims} top`, then `{claims} show {thread}` for the top three open or reopened threads, condensed to each claim and its quote.
Ask one question with the host's structured user-input tool when available: which thread to pursue now, a claim that misreads its evidence and should be retracted, or none.
Recommend the thread with the most runs across the most repositories; a reopened thread outranks an open one with the same reach.

A claim the user says misreads its evidence is retracted with `{claims} retract {id} --reason "{their words}"`, and the question is asked again.

## 5. Brief

For the chosen thread, write `/tmp/{project-slug}/reflect/brief-{thread}.md` per [references/brief.md](references/brief.md).
Read the implicated skill's own text at `{reflect-skill-root}/../{skill}/` to name the lines the evidence points at.
Prefer a direction that adds or tightens a deterministic check the skill loops against over one that adds prose, per the repository rule that long rule lists become guidelines.

## 6. Close

Print the brief's path and its one-line problem statement.
Recommend the next step: run `/dev:scope` in the workflow repository with the brief, then `/dev:build`; once the fix merges, `/dev:reflect resolve {thread} {sha} {eval-id}`.
