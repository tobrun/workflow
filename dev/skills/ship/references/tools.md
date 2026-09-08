# Acquiring the Tools

The gauntlet needs eight deterministic tools.
The acquisition ladder, per tool, is: the repo's existing tooling, else the ecosystem's established tool, else a small repo-fitted script an agent writes.
Never hand-roll what the ecosystem already maintains, and never download a generic harness wholesale - fit the tool to this repo, commit it, and reuse it on every later run.

## Where tools live

Repo-fitted scripts and configs go under `tools/harden/` in the consuming repository, committed with a README line saying what each does and how to invoke it.
The first run pays the acquisition cost; every later run - and any other agent - reuses them.
Check `tools/harden/` before acquiring anything.
Acquisitions are independent of each other: when several tools are missing, acquire them in parallel - one agent per tool, launched in a single message - rather than building them one after another.

## 1. Project static analysis

Run everything the repo already configures, not a subset you pick: linters, type checkers, format checkers, and any other analyzer wired into the project.
Discover the full set from where the repo declares it - package scripts, Makefile or task-runner targets, CI workflow steps, pre-commit hooks, and tool config files at the root (`.eslintrc*`, `pyproject.toml` tool sections, `clippy.toml`, and the like).
A configured tool that CI runs and the gauntlet skips is a check silently weakened; when unsure whether something counts, run it.
Prefer each tool's changed-files or per-path mode to scope to the diff; pre-existing findings elsewhere are noted, not fixed, unless the run is full-repo.
If the repo configures nothing, wire up the ecosystem's standard linter and type checker with their default configs as part of this step, committed like any other acquired tool.

## 2. Security scan

Three sub-scans, each zero-threshold:

- **Secrets**: gitleaks or the ecosystem equivalent over the in-scope files. A found secret is never fix-agent work - stop the gauntlet and escalate immediately, because it needs rotation and possibly history rewriting, both human calls.
- **Dependency vulnerabilities**: the ecosystem's audit tool (osv-scanner, npm audit, pip-audit, cargo audit) over every manifest the diff touched.
- **Static security rules**: the repo's own SAST config if one exists, else semgrep with the ecosystem's default ruleset, scoped to in-scope files.

## 3. Dead code detector

The ecosystem's detector - knip or ts-prune (JS/TS), vulture (Python), staticcheck's unused checks (Go) - else a repo-fitted script that greps each added or touched export for references.
Scope: symbols the diff added that nothing references, plus symbols the diff orphaned by removing their last caller.
Declare entry points and deliberate public API surface as exclusions in committed config; a public export is not dead merely because the repo itself never calls it.

## 4. Duplication detector

jscpd or PMD CPD, comparing the in-scope files against the whole repo and reporting only clones that a diff-touched file participates in.
The threshold is token-based (default 50) so trivial similarity does not fire; where the line sits is a recorded decision like any other threshold.
Pre-existing clones between untouched files are noted, not fixed, unless the run is full-repo.

## 5. Dependency checker

Almost always a repo-fitted script: parse the `rules` block of `docs/dependencies.md` (format in [../../../references/dependency-rules.md](../../../references/dependency-rules.md)), glob-match the in-scope files to modules, extract static imports with the language's own tooling (a compiler API, an AST module, or a disciplined grep for import statements), and print each forbidden edge as `file -> file (module -> module)`.
Ecosystem tools exist for some stacks (dependency-cruiser for JS/TS, import-linter for Python, ArchUnit for JVM); prefer one when the repo already uses it or adoption is one config file that mirrors `docs/dependencies.md` - but `docs/dependencies.md` stays the single source of truth, so generate the tool's config from it rather than maintaining two rule sets.

## 6. Coverage-weighted complexity

The score per function combines cyclomatic complexity with test coverage so that only *uncovered* complexity fails: complexity squared, scaled down by the fraction of the function's paths the tests execute. A fully covered function scores its complexity; an uncovered one scores its complexity squared.

- Coverage comes from the repo's existing coverage runner (jest/vitest `--coverage`, `coverage.py`, `go test -cover`, JaCoCo, tarpaulin). If the repo has none, wiring the standard one up is part of this step.
- Complexity comes from the ecosystem's standard analyzer (eslint `complexity` rule, `radon`, `gocyclo`, checkstyle) or, failing that, a small AST script.
- A repo-fitted script under `tools/harden/` joins the two reports and prints each function over threshold as `file:function score (complexity N, coverage P%)`.

Scope the report to functions the diff touched; pre-existing offenders elsewhere are noted, not fixed, unless the run is full-repo.

## 7. Flakiness detector

Almost always a repo-fitted script: run the tests the diff added or touched N times (default 5), shuffling order where the runner supports it (vitest `sequence.shuffle`, pytest-randomly, `go test -shuffle`, JUnit's method orderer).
Any run disagreeing with any other marks that test flaky - a violation like a failure, because a test that passes inconsistently proves nothing.
Keep it affordable: only diff-touched tests, reuse build caches between repeats, and run repeats inside one runner invocation where the framework allows.

## 8. Mutation testing

Prefer the ecosystem's mutation framework - Stryker (JS/TS), mutmut or cosmic-ray (Python), PIT (JVM), cargo-mutants (Rust), go-mutesting (Go).
These handle mutant generation, test selection, and reporting far better than a hand-rolled loop; write only the thin config that scopes them.
Hand-roll only when the ecosystem has nothing: an agent-written script that applies one mutation at a time (flip `<` to `<=`, `==` to `!=`, `+` to `-`, negate conditions, drop return values), runs the narrowest relevant test command, and records survivors.

Keeping it affordable:

- **Scope to the diff**: mutate only in-scope files, run only the tests that cover them (most frameworks do incremental or per-file runs; use that).
- Set a per-mutant test timeout so an infinite-loop mutant cannot hang the run.
- Equivalent mutants (mutations that provably cannot change behavior) are the one legitimate survivor category: mark them as such in the report with the reasoning, don't chase them forever - two fix rounds, then escalate per the loop rule.

## Fix-agent prompts

Keep them minimal - the violation list, the file paths, the fix vocabulary, nothing else.
The agents inherit no conversation and need none: a surviving mutant at `cart.ts:41 (< flipped to <=)` plus "write the test that kills it, at the layer the surrounding tests use" is a complete task.
Do not paste tool source, spec prose, or this reference into fix prompts; long prompts are how rules soften.
