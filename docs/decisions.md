# Decisions

## Principles

P-deterministic-guards-over-prose: when a loop or skill must be constrained, prefer a check it cannot argue with over an instruction
  promoted 2026-09-17 - recurred across several decisions in the factory work (removed 2026-09-20); matches CLAUDE.md "prefer a deterministic check it loops against"

## Quality gauntlet

D-complexity-threshold: Where does the ship gauntlet's coverage-weighted complexity line sit in this repository? (2026-09-18)
  ✓ 10 per function, applied to functions a branch adds - holding pre-existing functions a diff only brushes to any line would turn every change into a refactor of code it did not write (user, 2026-09-18); the evidence was the factory runner's established functions sitting far above the default, measured with radon 2026-09-18, and that code was removed 2026-09-20 ⚠ touched pre-existing functions stay over the line until a change that owns them splits them
  ✗ 6 on new functions - splits nearly every new function into helpers that each earn nothing on their own, against keeping indirection earned
  ✗ 6 on every touched function - a refactor of modules outside the change

## Removed

D-remove-factory: The factory plugin and its runner are removed from this repository (2026-09-20)
  ✓ delete `factory/`, `plugins/factory/`, the runner end-to-end test, and the F01 through F04 validator checks - the implementation was not working out (user, 2026-09-20); the repository goes back to shipping `dev` alone
  ✗ keep it unmaintained behind a flag - dead weight in every validation run and every plugin build, and it would keep drifting from `dev`
