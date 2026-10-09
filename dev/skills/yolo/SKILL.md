---
name: yolo
description: Take one task from request to an open pull request in a single unattended run - the scope, build, and ship stages run back to back in this session with every quality loop intact, and every question those stages would put to the user is answered by their own recommendation and recorded for the PR reviewer instead. Use when the user hands over a task and wants it finished without being asked anything, or says to yolo it.
disable-model-invocation: true
---

# Yolo

Finish the task the user gave you, ending in a pull request, without asking them anything along the way.
The stages are the sibling skills' bodies, read by path and followed in this session - never launched as skills: `../scope/SKILL.md`, then `../build/SKILL.md`, then `../ship/SKILL.md`, each with its references and scripts resolved from its own directory.
Their quality loops - the argued decisions and spec lint, the per-scenario tests and e2e run, the gauntlet, the verified panel, the remediation rounds, the evidence check - run exactly as written.
What changes is one thing: wherever a stage would stop for the user, this skill applies the stage's own recommendation, writes down that it did, and keeps going.

Invoking this skill is the task.
The task is the text the user gave with it, or a Markdown file they pointed at; with neither, the one thing you may ask is what the task is, before anything starts.

## Preflight

1. Run `python3 {yolo-skill-root}/../../scripts/skill-metrics.py start yolo`; the stages' own `start` and `end` calls are skipped, this run measures as one.
2. Confirm the host has a subagent tool: the blind-spot pass and the review panel cannot run without one, and a yolo that silently drops them is not a yolo.
   Without one, stop here and say so; nothing has been spent yet.
3. Check `.dev/` is git-ignored (`git check-ignore -q .dev/`); when it is not, add the entry to `.gitignore` yourself and commit it with the first change set.

## The no-question contract

Every stage keeps its full body.
These rules replace only the moments where a stage would wait on the user:

- **The task text is the interview.** Read it as the user's answers; what `scope` would still ask becomes a cataloged decision and is argued like any other.
- **A recommendation is the answer.** Whatever confidence it carries, apply it; write `auto-applied at Confidence: NN%` on the chosen line as `scope` does for its 75%+ cases.
  Below 75%, also mark the line `⚠` and give it a `? verify:` naming the one fact that would overturn it, so the reviewer sees exactly where the run guessed.
- **No `⚑` survives.** A decision `scope` would leave waiting on the user resolves to its best-evidenced alternative under the rule above; the change plan never links an open decision, so nothing settles later.
- **The ask is the scope.** Climb to the problem and pressure-test the premise as `scope` does, but a premise that does not hold is recorded as a `⊘` line with the evidence, and the task is still built as asked - the reviewer decides whether to merge it.
  Do not shrink the task to a first slice; split it into change sets instead.
  A `⊘` deferral is allowed only for work the task itself calls optional.
- **The next step is the recommended one.** `scope` recommends `build`; when it would offer `scope-review` for a large change as a user choice, skip it, that is not the recommendation.
  `build` ends by recommending `ship`, and `ship` runs its default flow, all three phases.
- **Confirmations are granted.** Sizing, the small-or-full call, a tiny or huge diff, the push, the PR, the e2e launch command: decide, do, and record.
- **Human calls become open calls.** A gauntlet threshold, a suppression, an upgrade that breaks the build, an e2e scenario red three times on one root cause, a reviewer blocker that survives both remediation rounds, a `diverged` ledger classification: never change the threshold, the ledger, the spec, or a contract to clear it.
  Record it where the stage already records such things and continue; it reaches the PR's Open calls section.
- **A missing input is discovered, never requested.** Validation commands, the launch command for e2e, the plan directory: find them from the repository as the stage's fallback says; when none exists, log the gap as a deviation and run what can be run.
- **Reports are for the PR.** Skip the spec and review HTML renders; keep the e2e report, since the PR's evidence is extracted from it.
  Publish nothing.

## Hard stops

Three things end the run before the PR, with a plain report and no question:

- a secret found by the gauntlet or anywhere else
- a step that would need a destructive action: rewriting history, force-pushing, deleting outside the work branch
- a resource the session cannot reach and the task cannot be done without, such as credentials for a service the change must call

Everything else is a deviation, an open call, or a draft PR, never a stop.

## Where the calls land

Every decision made in the user's place is written when it is made, in the file the stage already keeps, per [../../references/plan-layout.md](../../references/plan-layout.md):

| Stage | Where | As |
| ----- | ----- | -- |
| scope | `spec.md` | the auto-applied block, `⚠` and `? verify:` marks on lines under 75% |
| build | `implementation-notes.md` | Deviations lines and fixup entries |
| ship | `pr.md` | Open calls, one line each |

Before creating the PR, add every scope line under 75% and every build deviation to the Open calls section, so the body lists every guess in one place.
A run with an open call of any kind, or a real blocker, opens the PR as a draft; a clean run opens it ready once its required checks are green.

## Wrap up

Print the measured run metrics with `python3 {yolo-skill-root}/../../scripts/skill-metrics.py end yolo --count decisions=N --count auto_applied=N --count below_75=N --count change_sets=N --count scenarios=N --count violations_fixed=N --count findings_verified=N --count remediation_rounds=N --count open_calls=N`, pasting its table verbatim, plus any `--friction` lines per [../../references/run-journal.md](../../references/run-journal.md).
Then, in one message: the PR URL and whether it is draft or ready, the state of its required checks, the review verdict with the `review_N.md` paths, and the open calls in the order the reviewer should read them - guesses below 75% first, then blockers, then deviations.
Recommend `scope` on the plan directory when an open call needs a decision changed, and `scope-quick`, then `build`, then `ship-quick` when the reviewer accepts findings that are plain defects; never launch either.
