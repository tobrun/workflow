# Merge and Prune

The existing root AGENTS.md and CLAUDE.md are input, not output.
The template's own rule lines are not classified; they are the lessons, not generic advice to prune.
Classify each bullet or paragraph before drafting, splitting it only when its sentences land in different classes, and keep the classification: the approval table is built from it, grouped by section.

| Class | When | What happens |
| ----- | ---- | ------------ |
| keep | repo-specific, and true or plausibly true and unverifiable ("requires `MAPBOX_ACCESS_TOKEN`", "tests must not run from the root") | moves into the section it concerns, wording kept, em dashes replaced with a plain dash |
| update | repo-specific but stale against the probe (a renamed script, a moved path, a changed command) | rewritten to the current fact, with the probe evidence cited in the table |
| prune | what the filesystem already shows, generic agent advice, a duplicate, or stale with no current equivalent | dropped, with the reason in the table |
| conflict | a rule the file states that contradicts a lesson ("mock the database in unit tests", "retry flaky tests", "use a different commit format") | never overwritten silently; it becomes an interview question, because it may be a deliberate decision |

## Conflicts

A conflict is an instruction in an agent file; a repo fact that falls short of a lesson (an e2e suite that needs a live credential, a retry in a config file) goes through the concept map as an open item instead.

Ask one conflict at a time, with the lesson, the existing rule, and a recommendation.
When the person keeps their rule, it stays in its section and the conflicting lesson line is reworded to agree, never both.
When they drop it, the lesson line stands and the old rule is pruned.

## Length

When kept gotchas push the draft past 100 lines, condense them first, without joining sentences onto one line to hit the number.
Past the 150-line cap, ask which area-specific gotchas to drop; never drop a true, user-written gotcha silently.

## CLAUDE.md

This skill never writes or edits CLAUDE.md; Claude Code reads AGENTS.md directly.
After the merge, CLAUDE.md content that moved into AGENTS.md is duplicated, so the closing report recommends deleting CLAUDE.md, or trimming it to what only Claude uses (such as `@` imports).

## Re-runs

Re-running on a file this skill wrote should produce a minimal diff: keep the existing wording of every line whose fact did not change.
