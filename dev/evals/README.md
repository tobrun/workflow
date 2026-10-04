# Evals

Status: maintained

Eval definitions for the `dev` plugin's skills: realistic prompts and objective assertions used to check whether a skill change preserved behavior.

- `{skill}.json` - one file per skill: the eval prompt(s), the fixture each expects, and the assertions to grade the output against. Covers all 7 skills: `scope`, `scope-review`, `commit`, `build`, `ship`, `to-pitch`, `to-quiz`.
- `results.md` - the record of the most recent full run: scores, methodology, and findings.
- `tests/` - unit tests for the deterministic scripts the skills loop against (`lint-spec.py`, `change-set-brief.py`, `check-tests.py`, `pr-evidence.py`), run by `scripts/validate.sh` as check D01.

`build` runs in `"functional"` mode (a real fixture, a real subagent run, assertions checked against the actual output).
`scope`, `scope-review`, `commit`, `ship`, `to-pitch`, and `to-quiz` run in `"comprehension"` mode instead - each depends on either an interactive question loop, a live codebase, or prior artifacts (a finished spec, implementation notes, an e2e report) that are too expensive to stage on every iteration, so these check policy comprehension of the skill text directly.

## When to run these

Whenever a skill's `SKILL.md` changes in a way that could affect behavior (not just typo fixes), not only when trimming for concision.
Run before and after the change (old text via `git show <commit>:path`, new text from the working tree) so the comparison is apples to apples.

See the "Re-running this harness" section in `results.md` for the exact steps: recreate fixtures, spawn one subagent per (skill, version), grade against assertions, update the scores.

## Why old-vs-new instead of a fixed pass/fail bar

Skills are prose instructions, not code with a test suite; the only way to know a change didn't lose a rule is to run the same task against the version that had the rule and the version that might not, and compare. A single run against only the new version can't tell you what regressed.
