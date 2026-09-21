# Contracts

## Config-driven factory

C-factory-phase-contract: the launch prompt requires every phase, ours or foreign, to write .dev/{plan}/results/{phase}-{attempt}.json with schema factory.result/1 as its last action, and never to launch another phase. This imposes both, it does not guarantee them; a phase that writes nothing is read as a failed attempt
  guaranteed by: the wrapper in run/references/launch.md, which validate.sh F02 checks for the results path literal, the factory.result/1 literal and the exact sentence "Never name or launch the next phase."; F02 over every built-in phase body for factory-run.json, the results path literal and that same sentence; and run-state.py check-result treating a missing or unparseable file as a failed attempt
  relied on by: the judgment loop, run-state.py check-result, and the one-invoker rule (D-invoker-invariant)
  (2026-09-20, config-driven-factory/spec.md)
C-factory-unattended: after the go, no FACTORY-OWNED phase body routes a decision to a person. A phase body the factory did not write is covered by no check: its author declares it unattended_safe in the config, and a phase that blocks on a person is neither prevented nor detected
  guaranteed by: the validate.sh scan over factory/phases and factory/skills/run, for factory-owned bodies only
  relied on by: the judgment loop, which never waits on a person
  (2026-09-20, config-driven-factory/spec.md)
C-factory-plan-files: plan files under .dev/ are never committed by a factory run; the run branch carries code, tests, docs/ and, after an explicit inject, the .factory/ tree
  guaranteed by: the run skill's preflight, which runs git check-ignore -q .dev/ in the consuming repository and appends the entry to .gitignore as a recorded repair when it is missing
  relied on by: any consuming repository that has not ignored .dev/ itself; git log --name-only on the run branch is the proof the closing report cites, and ship reviews .factory/ files in that log as part of the change
  (2026-09-20, config-driven-factory/spec.md)
