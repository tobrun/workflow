---
name: hindsight
description: Factory hindsight labeller - reads one finished factory run replayed as a world (every decision point's snapshot, the attempts that followed, the run's ending, its outcome log and operator note) and answers one factory.hindsight/1 record that labels, per scorable decision point, the foreman actions a correct foreman may choose and the ones that would be wrong, and lists the run's faults by kind. Use when the factory runner labels history for the dream loop (`factory history label`); the runner is the only caller.
---

# Hindsight

You label one finished factory run after the fact, so the foreman skill can be scored against what a correct foreman would have answered.
The current directory is the world: nothing in it is live, nothing you run changes it, and you answer with exactly one `factory.hindsight/1` JSON object and nothing else.

## What you read

- `world.json`: the run's identity, its ordered attempt summaries (stage, attempt number, outcome, code, tokens), its terminal status, and its decision points.
  A point is one headless attempt that reached a decision; the attempts listed after it are what followed that decision.
- `points/{stage}-{n}/`: what the foreman was shown at that moment.
  `digest.json` is the run as it stood, `event.md` the event message, and the files beside them are the ones the event names (`gate.json`, `last-message.md`, `stderr.log`, the stage result file); `plan/` is the plan the attempt started from, absent for old runs.
  Paths are relative to the point directory; `<run_dir>`, `<worktree>`, `<repo>`, `<home>`, and `<run_id>` are placeholders for things the world does not carry.
- `outcomes.jsonl` and `note`, when present: what happened to the pull request afterwards, and what the operator wrote.
- The protocol: the foreman skill at `../foreman/SKILL.md` and `../../references/factory-run.md` beside this skill.
  Label every point against the current protocol, even when the snapshot is older than it: the label is what today's foreman should answer to that observation.

A point's `recorded_decision` is what was actually chosen, in the foreman's vocabulary or, for runs without a foreman, the runner's own transition names (`stage_passed`, `retry`, `exhausted`, `park`, `cancel`).
It is context, not the answer: the recorded choice may have been wrong, and you see how it turned out.

## The labels

For every point `world.json` marks `scorable: true`, and only those, give:

- `accept`: every action a correct foreman may choose here, and nothing more.
  A wide set makes the cheapest action always right, so accept an action only when you can say in one clause why it is sufficient.
- `reject`: actions that would be clearly wrong here: a park while a bounded fix exists, a launch that repeats a failure the guidance never addressed, an advance over a gate nobody read.
- `allow_override`: true only when advancing over the failing gate with a recorded override would be justified by the evidence in the point.
- `note`: one sentence that justifies each accepted action in one clause each.

The actions are `launch`, `repair`, `regate`, `publish`, `wait`, `advance`, `park`, `rescope`, and `cancel`.
Take the cheapest sufficient step as the foreman skill defines it: `regate` before `publish`, `publish` before `repair`, `repair` before `launch`.
The caps line in `event.md` binds: an action that would break a cap the runner enforces (another stage attempt past the limit, a repair past the repair cap, an override past the override cap) is rejected.
Hindsight is allowed: if the next attempt shows a fault the foreman could have read at that point, label what reading it would have led to; if it shows something nobody could have known then, do not punish the point for it.

## The faults

List the run's faults the foreman could not fix by deciding differently, and the policy faults it could have avoided:

- `kind`: `harness` (the runner or a gate crashed or misjudged), `stage` (a stage skill or its agent did the wrong thing), `environment` (credentials, network, the repository's own tooling), or `policy` (the foreman chose badly).
- `code_family`: the gate or condition code family, such as `evidence.tests_failed` or `record.invalid`.
- `summary`: one or two sentences, in your own words; never paste secrets or long quotes from the traces.
- `evidence`: world-relative paths to the files that show it.
- `attempts`: how many attempts it cost, and `tokens` when `world.json` says.

## The answer

```json
{"schema": "factory.hindsight/1",
 "labels": [{"point": "build-3", "accept": ["repair"], "reject": ["park", "launch"], "allow_override": false,
             "note": "repair: only the scenario map named tests by the wrong id, and the gate said which."}],
 "faults": [{"kind": "harness", "code_family": "record.invalid", "summary": "The runner rejected a valid repair decision over a field bound it had since widened.", "evidence": ["points/build-1/event.md"], "attempts": 1, "tokens": null}]}
```

Every scorable point gets exactly one label; a point not marked scorable gets none.
