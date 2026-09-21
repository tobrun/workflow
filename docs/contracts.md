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

## Config-driven factory

C-factory-phase-contract: the launch prompt requires every phase, ours or foreign, to write .dev/{plan}/results/{phase}-{attempt}.json with schema factory.result/1 as its last action, and never to launch another phase. This imposes both, it does not guarantee them; a phase that writes nothing is read as a failed attempt
  guaranteed by: the wrapper in run/references/launch.md, which validate.sh F02 checks for the results path literal, the factory.result/1 literal and the exact sentence "Never name or launch the next phase."; F02 over every built-in phase body for factory-run.json, the results path literal and that same sentence; and run-state.py check-result treating a missing or unparseable file as a failed attempt ? verify: not yet built; resolve when config-driven-factory/spec.md change set 7 lands
  relied on by: the judgment loop, run-state.py check-result, and the one-invoker rule (D-invoker-invariant)
  (2026-09-20, config-driven-factory/spec.md; supersedes C-factory-result once change set 7 lands)
C-factory-unattended: after the go, no FACTORY-OWNED phase body routes a decision to a person. A phase body the factory did not write is covered by no check: its author declares it unattended_safe in the config, and a phase that blocks on a person is neither prevented nor detected
  guaranteed by: the validate.sh scan over factory/phases and factory/skills/run, for factory-owned bodies only ? verify: not yet built; resolve when config-driven-factory/spec.md change set 7 lands
  relied on by: the judgment loop, which never waits on a person
  (2026-09-20, config-driven-factory/spec.md; supersedes the entry above once change set 7 lands)
C-factory-plan-files: plan files under .dev/ are never committed by a factory run; the run branch carries code, tests, docs/ and, after an explicit inject, the .factory/ tree
  guaranteed by: the run skill's preflight, which runs git check-ignore -q .dev/ in the consuming repository and appends the entry to .gitignore as a recorded repair when it is missing ? verify: not yet built; resolve when config-driven-factory/spec.md change set 7 lands
  relied on by: any consuming repository that has not ignored .dev/ itself; git log --name-only on the run branch is the proof the closing report cites, and ship reviews .factory/ files in that log as part of the change
  (2026-09-20, config-driven-factory/spec.md; supersedes the entry above once change set 7 lands)
