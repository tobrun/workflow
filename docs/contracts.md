# Contracts

## Factory plugin

C-factory-result: every factory phase skill writes the result path the launch prompt names (.dev/{plan}/results/{phase}-{attempt}.json) as its last action, with schema factory.result/1, and never launches the next phase
  guaranteed by: the Factory context section of each phase SKILL.md, checked by validate.sh check_factory_protocol (scripts/validate.sh, factory-plugin/spec.md change set 5)
  relied on by: the run skill's judgment loop, run-state.py check-result
  (2026-09-20, D-result-envelope)
C-factory-unattended: after the go, no factory phase skill routes a decision to a person - scope is the one interactive phase and is outside the check's scan root, and run/SKILL.md's person-routing lines live in <!-- interactive-only --> blocks
  guaranteed by: the validate.sh check over factory/skills/{scope-review,build,ship,run} and factory/references/factory-run.md, whose regex self-tests against four sample phrases and fails validation on a failed self-test
  relied on by: the orchestrator's judgment loop, which never waits on a person; a run out of options ends with a written report instead of a question
  (2026-09-20, D-unattended-wording-check, D-human-touchpoints)
C-factory-plan-files: plan files under .dev/ are never committed by a factory run; the run branch carries code, tests, and docs/ only
  guaranteed by: the run skill's preflight, which runs git check-ignore -q .dev/ in the consuming repository and appends the entry to .gitignore as a recorded repair when it is missing, so the rule has a carrier in any consuming repository and not only in the fixture ? verify: no free scenario reaches the missing-entry branch, because the fixture's setup.sh writes the entry itself (D-verification names this one of the six knowingly unproven behaviors)
  relied on by: any consuming repository that has not ignored .dev/ itself; git log --name-only on the run branch is the proof the closing report cites
  (2026-09-20, D-plan-files-ignored)
