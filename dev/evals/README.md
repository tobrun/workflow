# Evals

Eval definitions cover all 11 dev skills.
Each `{skill}.json` gives realistic prompts, fixture requirements, and observable assertions.
`results.md` records dated runs and their limits; it distinguishes full skill runs, comprehension checks, and deterministic protocol fixtures.

## Layers of validation

- `tests/` runs the deterministic script fixtures through their real CLIs as check D01 of `scripts/validate.sh`.
  These cover spec mechanics, wave briefs, runner receipts, PR evidence, review completeness, single-writer refinement, impact scope, packaging, and memory records.
- Policy comprehension prompts test routing and stated authority.
  They do not prove that an agent performs the workflow correctly under pressure.
- Functional skill runs use isolated repositories, actual implementation and application execution, and outputs graded against the requested behavior.
  Build has functional task definitions; ship and scope-review also include case-level functional protocol fixtures.

The default file-level `mode` applies to its ordinary prompts.
A case-level `mode: functional` identifies a deterministic fixture inside an otherwise comprehension-oriented file.
Do not label a script test as a full model workflow eval.

## When to run

Run relevant fixtures whenever a behavior or proof boundary changes.
For a substantial skill revision, forward-test realistic tasks against both the unchanged text and the proposed text in equivalent scratch repositories.
Independent evaluators receive the skill, the task, and raw artifacts, without the intended fix or prior conclusions.
Keep their git operations and outputs inside the fixtures; prevent network, production, publication, and global-memory side effects unless explicitly part of the evaluation.

Include boundary cases that distinguish the actual policy alternatives.
For example, 70% distinguishes a 65% threshold from 75%; 55% and 82% do not.
Include failure cases such as missing agent results, malformed enums, skipped tests, competing writes, and unavailable remotes.

## Grade observable behavior

Compare the same task across versions to expose lost behavior and new friction.
A deliberate repair may change the outcome: an invalid baseline PASS should become INCOMPLETE, not be preserved as a score to beat.
Use fixed invariants for claims such as no unauthorized push, no false approval, and no stale evidence.
Passing helper tests and comprehension answers provide useful confidence but never substitute for a complete skill run.
Record what actually ran, which commands and artifacts prove it, and what remains untested.
