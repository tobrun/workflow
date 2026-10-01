# Eval Results

Status: no full run recorded for the current skill set; one partial run below.

The last recorded run (2026-07-24) covered the pre-pivot five-skill chain and was invalidated by the pivot to the scope pipeline; its scores were removed rather than left to invite false confidence.
Run the harness below against the current 6 skills (`scope`, `commit`, `build`, `ship`, `to-pitch`, `to-quiz`) and replace this file with the dated results.

## Partial run, 2026-10-01: build `parallel-wave`, before and after the build-speed change

One functional run per version, not the full harness.
Old is the build skill before this change, new is the version that adds the wave gate, the change-set brief, and the cheap-red rule.
The fixture was a Node library with a 20 second legacy test in its suite, and a spec with change sets 1 and 2 disjoint and change set 3 editing files of both.
Every run of a Validation command was written to a log by the fixture's package scripts, so the counts are measured, not reported.

| Check | Old | New |
| ----- | --- | --- |
| Change sets 1 and 2 launched as subagents in one message | pass | pass |
| Change set 3 started only after 1 and 2 were committed | pass | pass |
| Subagents left git state and `implementation-notes.md` alone | pass | pass |
| Subagents told not to launch the app | pass | pass |
| Each subagent handed a brief from `change-set-brief.py` instead of the whole spec | fail (reads all of `spec.md`) | pass |
| Subagents run only their own test file plus syntax checks | fail (each ran the Validation block) | pass |
| Orchestrator runs the Validation block once per wave | pass | pass |
| Three change-set commits in spec order, `check-tests.py` clean | pass | pass |
| Runs of the full Validation block (log) | 4 | 2 |
| Extra break-and-rerun cycles to see a test red | 2 | 2 |
| Duration by `skill-metrics.py` | 7m 10s | 8m 55s |

Findings:

- The Validation block ran half as often, and no subagent ran it.
- Wall time did not improve on this fixture: the suite costs 20 seconds, so two saved runs are under a minute, less than the difference between two model runs. The saving grows with the cost of the block and the number of change sets; a real build is needed to measure it.
- The old run's notes named a test with a comma in it, which `check-tests.py` then split in two. The checker now splits only where a `path::name` follows the comma.

## Re-running this harness

1. Pick a baseline commit (the last commit before the change under test) and the working tree as "new".
2. For functional evals, recreate the fixture described in each `{skill}.json` under a scratch directory outside the repo.
3. Spawn one subagent per (skill, version) pointed at the respective `SKILL.md` text (via `git show <commit>:path` for old, the live file for new) plus the eval prompt.
4. Grade each output against that eval's `assertions`.
5. Record a scores table (skill, eval, old, new) and note any regression before merging the change under test.
