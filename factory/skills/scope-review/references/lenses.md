# Spec Review Lenses

Each lens is one agent in the panel.
The artifact is `spec.md`; the repo is the evidence.
Every lens reads the whole spec but stays in its lane; prompt assembly and delivery follow ship's [orchestration.md](../../ship/references/orchestration.md) with the substitutions in this skill's SKILL.md.

## feasibility

Does the change plan survive contact with the repo?

- Every file a change set names exists, or the set says it is new; the described edit is possible at that site - the function, hook, or config it assumes is really there.
- Prior art and idioms the spec cites exist where it says they do.
- Premises about current behavior are checked against the code, never trusted: an "X already handles Y" claim that is false is a BLOCK naming the site.
- The Validation block's commands exist in the repo's manifests and run the layers the plan relies on.

## completeness

What will the implementer hit that the spec never mentions?

- Failure paths and edge cases with no decision, no scope entry, and no test scenario.
- Second-order work the change implies but no change set carries: migrations, config, deploy steps, doc updates.
- The medium-decision trap: choices that look small but swing how much work is done, silently defaulted instead of argued.
- Call sites and consumers affected by the change but outside every change set's file list; Grep for them.

## consistency

The spec against itself and the project's ledgers, semantically - `lint-spec.py` already enforced the mechanics.

- A change set that contradicts its linked `✓` decision, or quietly implements a `✗` rejected alternative.
- A scope invariant or error-handling entry that no change set enforces.
- A decision colliding with `docs/decisions.md` without acknowledging it, a plan that would break a `docs/contracts.md` guarantee while reliance sites still assume it, or a new dependency edge `docs/dependencies.md` forbids.

## testability

Will the `tests:` lines produce real proof?

- Each scenario is provable at its tagged layer, and concrete enough that whoever writes it invents nothing - a vague scenario is a finding even when the feature is right.
- Scenarios cover the scope's invariants and failure paths, not just happy paths.
- A `tests: none - {reason}` whose reason does not hold is a finding.
- `[e2e]` scenarios are runnable in the mocked environment build uses; a scenario needing a live third party will fail there.

## Shared rules (include in every lens prompt)

Severity ladder:

- **BLOCK**: implementation following this spec would fail or build the wrong thing; name the concrete site or scenario.
- **CONCERN**: a gap worth a conversation before build; include what you checked.
- **NIT**: minor polish.

`file`/`line` point into `spec.md` unless a repo path is named.
Stay in your lane; other lenses cover the rest.
A finding based on a guess about unseen code is not a finding - read the repo first.
Every finding needs a location, a one-line title, and a 2-4 line detail naming the evidence.
Also report up to 5 genuinely good things your lens noticed.
Output contract: the same JSON as ship's lens agents, defined in [orchestration.md](../../ship/references/orchestration.md).
