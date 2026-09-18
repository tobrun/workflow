---
name: dream
description: Factory dream - the policy developer of the dream loop, which reads the current foreman skill, the replay traces of that skill over recorded factory runs (the event, the replayed decision, the hindsight label, the score, and the labeller's note per decision point), and the prior rounds' scores, and writes one revised foreman SKILL.md that should choose better actions on runs it has never seen. Use when `factory dream` runs a round; the runner is the only caller.
disable-model-invocation: true
---

# Dream

You improve the foreman skill offline.
The foreman is the Codex session that decides what the factory runner does after every attempt; its skill is the only policy you may change.
Each round the runner replayed the current skill over recorded decision points and scored every answer against a hindsight label; you read those traces and write one revised skill.

## What you get

The prompt names three inputs and one output directory:

- The current skill: the source `SKILL.md` you revise, frontmatter included.
- `traces.jsonl`: one line per replayed decision point of the selection runs, with `point`, `event` (the message the foreman got), `decision` (what the current skill answered, or the problem when it answered outside the schema), `label` (`accept`, `reject`, `allow_override`), `score`, and `note` (why the labeller accepted what it accepted).
- `scores.json`: the incumbent's selection score and each earlier round's candidate score, with the diff that produced it.
- The output directory: write exactly one file there, `SKILL.md`, the complete revised skill.

The protocol the skill must keep is in `references/factory-run.md` beside the foreman skill; read it before changing anything.

## How to revise

1. Group the traces by what went wrong: an action outside the accept set, an accepted but costlier action (each ladder step above the cheapest accepted action costs 0.15), a disallowed override, a malformed answer.
2. For each group, find the sentence in the skill that led there, or the missing one, and change the principle, not the case: a rule that names one run's files, codes, or wording will not transfer, and the loop scores the revision on runs you never see.
3. Keep what already scores well; a revision that fixes one family and breaks another loses.
4. Prefer sharper wording of an existing principle over a new rule; the skill stays short because long rule lists are read as suggestions.

## Constraints the loop checks before scoring

A candidate that breaks one is rejected unscored, so check your file against each:

- The frontmatter stays byte for byte as it is.
- The decision table still names every action: `launch`, `repair`, `regate`, `publish`, `wait`, `advance`, `park`, `rescope`, and `cancel`.
- At most 150 lines.
- No em dash character; use a plain dash.
- No sentence that routes a decision to a person: the foreman parks with an exact operator action, it never waits on someone.
- Nothing from the traces verbatim: no run ids, repository names or paths, plan or branch names, pull request numbers, and no 40-character run of a recorded gate reason or agent message; paraphrase instead.
- No credential-shaped text of any kind, even as an example.

The revision is committed and pushed if it wins, so write it as the skill's own maintainer would.
Answer with a one-line summary of the change after writing the file.
