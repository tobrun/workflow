# AGENTS.md Template

The skeleton below is the output shape `check-agents-md.py` enforces.
Fill every `<<fill: ...>>` slot from the probe; the checker fails while one remains.
Rule lines outside the slots are the distilled lessons; keep their meaning, adapt their wording to the repo's vocabulary, and drop a line only when the repo makes it untrue (a repo with no tests yet still keeps the test rules, because they describe the tests to add).

```markdown
# <<fill: project name>>

<<fill: one sentence on what this repository is for>>
Default branch is `<<fill: branch>>`.

## Commands

- Setup: <<fill: `install command`>>
- Build: <<fill: `build command` or n/a (reason)>>
- Check: <<fill: `aggregate lint/typecheck command`>>
- Unit: <<fill: `unit test command`>>
- Integration: <<fill: `integration test command` or none>>
- E2E: <<fill: `command that builds, launches, and drives the artifact` or none>>
- Merge gate: <<fill: `project-owned CI commands`; remote-only: job (reason)>>

## Tests

<<fill: one to three lines on where each layer's tests live, and how the e2e environment stubs third parties, seeds its store, and fixes the clock>>
Put each check on the cheapest layer that can fail for the right reason: unit for one rule with no I/O, integration for owned components working together against a real store, e2e for a whole journey through the built artifact.
Mock only at boundaries this repo does not own (third-party network, the clock, randomness, slow infrastructure), prefer a small fake to a stub, and never mock code this repo owns.
Inject the clock and randomness instead of patching globals.
E2E drives the built artifact against stubbed third parties and a seeded store, never production or shared staging.
Every new test is seen failing before it passes, and a bug fix starts with a test that reproduces the bug.
Assert outcomes through the public interface with literal expected values, and name tests after the capability they prove.

## Quality

A change is done when every merge-gate command passes locally on the final checkout.
A failing check is work to fix, including one called flaky or pre-existing; prove "pre-existing" by running the same command on the merge base.
Fix findings at their source: never suppress them inline, loosen a tool's config, or add retries, sleeps, or looser assertions.
A secret found in the tree is escalated to a person immediately, not quietly removed.

## Architecture

<<fill: a pointer to the architecture overview, or one to three lines of dependency direction the code does not make obvious>>
<<fill: where decisions, contracts, and dependency rules live, or "There is no written architecture overview, decision log, or contract list yet.">>

## Contributions

Work in the smallest slice that shows observable behavior, one reviewable idea per commit.
Commit subjects are `type(scope): subject` in the imperative, with a `What:` and a `Why:` body; scopes are <<fill: the scopes history uses, or the areas commits touch when history has none>>.
Never add an AI co-author trailer; a person is accountable for every commit.
Plan and scratch files live under `.dev/` and are never committed.
Every pull request carries evidence from a real run, and a bug fix shows its test failing on the merge base and passing on the branch.

## Open items

- [ ] <<fill: slug>>: <<fill: the gap, and what closing it takes>>

## Maintaining this file

Keep this file to knowledge almost every agent session in this repository needs.
Do not repeat what the code already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning an entry over appending a new one, and keep each command runnable exactly as written.
```

## Rules for filling it

- Every command is exact, backticked, and runnable from the repository root, or prefixed with `cd dir && ` when it must run elsewhere.
- Backtick only paths that exist; describe an example, a generated file, or a future location in plain words.
  A missing path is allowed in backticks only when git ignores it (a build output) or inside an open item.
- A slot covering several packages lists each command, comma-separated; extra Commands bullets after the seven slots (a run or dev command, code generation) are allowed when they carry what the manifest does not show: a flag, a working directory, or a precondition.
- When the Merge gate is `none`, the Quality "done" line names the commands that stand in for it until one exists.
- Adapt a rule line's nouns to the repo (a static site has fixed content, not a seeded store), never its meaning.
- Omit the Open items section entirely when there are no gaps.
- Kept user sections (a knowledge base, a fork policy, an outage-causing gotcha) go between Contributions and Open items, under their own H2.
  A gotcha about an existing section goes in that section instead.
- Say each thing once, and do not describe the folder layout, the dependency list, or the framework unless something about it would surprise a newcomer.
- State outcomes and failure modes, not tool mandates; naming a tool is fine when it is a fact of this repo.
- One sentence per line, no em dash, 50 to 100 lines, never over 150.
