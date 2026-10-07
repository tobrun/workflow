---
name: ship-quick
description: Re-ship a small change with one validation run, one reviewer that checks the previous review's findings are fixed, and a pull request update followed to green, with no gauntlet, no lens panel, and no remediation loop. Use when the user asks for a quick ship, or wants fixes for an earlier ship review pushed without repeating the full ship run.
---

# Ship Quick

Confirm the fixes hold, update the pull request, and follow it to green.
This is `ship` without the gauntlet, the lens panel and its verifiers, the remediation loop, and the HTML report.
Invoking this skill is the task: detect the diff yourself and start immediately.

First run `python3 {ship-quick-skill-root}/../../scripts/skill-metrics.py start ship-quick`.
Locate the plan directory and gather the branch diff per [../../references/plan-layout.md](../../references/plan-layout.md), and find the highest-numbered finished `review_N.md`, if any.

## 1. Validate once

Run the repository's required pull-request commands at the diff's impact per [../../references/ci-parity.md](../../references/ci-parity.md).
Fix what fails and re-run that command until it is green.

## 2. One reviewer

Launch a single fresh-context, read-only agent with the spec, the previous review, and the diff.

- With a previous review: it reports every Blocker and Concern in it as fixed or still open, each with the evidence, then reads the diff since the review's recorded `head` for new breakage.
- Without one, or when the review records no `head`: it reads the whole branch diff.
- New findings are blockers only, each with the scenario that triggers it - no concerns, no nits, no simplification.

Confirm a new blocker by reading the code yourself before it counts; drop what you cannot confirm.

## 3. Report

Write `.dev/{plan-name}/review_N.md` at the next free index in the shape of [../ship/references/report-format.md](../ship/references/report-format.md), with `Panel: quick` and the Previous findings section filled.
The verdict is BLOCK when a blocker stands, CONCERNS when only concerns remain open, and PASS otherwise.
On BLOCK, stop here: report the standing blockers and recommend `scope-quick`, then `build`, then this skill again.

## 4. Pull request

Follow [../ship/references/pull-request.md](../ship/references/pull-request.md) for the commit and push rules: never force-push, never rebase.

- A pull request exists: edit `.dev/{plan-name}/pr.md` in place - the review line of the Summary and the Open calls - and keep its Evidence and Quality sections as the earlier run wrote them.
  Loop `python3 {ship-quick-skill-root}/../ship/scripts/pr-evidence.py check .dev/{plan-name}/pr.md` until it passes, then update the body.
- No pull request yet: create it as that reference describes.

Then follow the required checks to green per "Follow the PR to green" in ci-parity.md, and mark the pull request ready once they are.

## Wrap up

Close with `python3 {ship-quick-skill-root}/../../scripts/skill-metrics.py end ship-quick --count findings_fixed=N --count findings_open=N --count new_blockers=N`, pasting its table verbatim.
Then give the verdict, the findings still open, the path of the review file, and the pull request URL with the state of its checks.
Say plainly that this run skipped the gauntlet and the panel, and recommend `ship` when the change has grown beyond the findings it set out to fix.
