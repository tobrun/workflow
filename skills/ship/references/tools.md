# Acquiring the Tools

The verify phase runs the twenty kinds of [../../../references/verification.md](verification.md); sections 1 to 7 below define the analyses it has always run, and section 8 the ones that check the newer kinds.
Every existing required repository check remains mandatory; supplementary analysis must have changed-code targets and an actionable defect criterion.
For absent optional tooling, record applicability and the chosen check before acquisition. No meaningful target means skipped with a reason, never a pass.
Tool acquisition is implementation work within this task only when needed to validate its promised behavior; do not install a blanket permanent toolchain merely because a tool is absent.
The acquisition ladder, per tool, is: the repo's existing tooling, else the ecosystem's established tool, else a small repo-fitted script an agent writes.
Never hand-roll what the ecosystem already maintains, and never download a generic harness wholesale - fit the tool to this repo, commit it, and reuse it on every later run.

## Where tools live

Repo-fitted scripts and configs go under `tools/harden/` in the consuming repository, committed with a README line saying what each does and how to invoke it.
The first run pays the acquisition cost; every later run - and any other agent - reuses them.
Check `tools/harden/` before acquiring anything.
Acquisitions are independent of each other: when several tools are missing, acquire them in parallel - one agent per tool, scheduled within available host capacity - rather than building them one after another.

## 1. Project static analysis

Run everything the repo already configures, not a subset you pick: linters, type checkers, format checkers, and any other analyzer wired into the project.
Discover the full set from where the repo declares it - package scripts, Makefile or task-runner targets, CI workflow steps, pre-commit hooks, and tool config files at the root (`.eslintrc*`, `pyproject.toml` tool sections, `clippy.toml`, and the like).
A configured tool that CI runs and the verify phase skips is a check silently weakened; when unsure whether something counts, run it.
Prefer each tool's changed-files or per-path mode to scope to the diff; pre-existing findings elsewhere are noted, not fixed, unless the run is full-repo.
If the repo configures nothing, report the gap and run the narrow checks needed for this change. Adopt persistent lint/type tooling only when included in the authorized task.

## 2. Security scan

Three sub-scans, each zero-threshold:

- **Secrets**: gitleaks or the ecosystem equivalent over the in-scope files. A found secret is never fix-agent work - stop the verify phase and escalate immediately, because it needs rotation and possibly history rewriting, both human calls.
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

## 5. Coverage-weighted complexity

The score per function combines cyclomatic complexity with test coverage so that only *uncovered* complexity fails: for complexity c and coverage fraction p, use `c*c*(1-p) + c*p`.
A fully covered function scores its complexity; an uncovered one its complexity squared.
State whether p measures lines or branches; line coverage does not prove branch/path coverage, and neither score proves test quality.

- Coverage comes from the repo's existing coverage runner (jest/vitest `--coverage`, `coverage.py`, `go test -cover`, JaCoCo, tarpaulin). If the repo has none, wiring the standard one up is part of this step.
- Complexity comes from the ecosystem's standard analyzer (eslint `complexity` rule, `radon`, `gocyclo`, checkstyle) or, failing that, a small AST script.
- A repo-fitted script under `tools/harden/` joins the two reports and prints each function over threshold as `file:function score (complexity N, coverage P%)`.

Scope the report to functions the diff touched; pre-existing offenders elsewhere are noted, not fixed, unless the run is full-repo.

## 6. Flakiness detector

Almost always a repo-fitted script: run the tests the diff added or touched N times (default 5), shuffling order where the runner supports it (vitest `sequence.shuffle`, pytest-randomly, `go test -shuffle`, JUnit's method orderer).
Any run disagreeing with any other marks that test flaky - a violation like a failure, because a test that passes inconsistently proves nothing.
Keep it affordable: only diff-touched tests, reuse build caches between repeats, and run repeats inside one runner invocation where the framework allows.

## 7. Mutation testing

Run a focused mutation pass after the relevant ordinary tests pass. Survivors guide contract review and regression tests; counts are reported, never a score gate, per verify.md's "Mutation testing is reported, not a threshold".

Before acquiring a tool, select changed decision-heavy logic: pricing calculations, permission rules, validation, parsing decisions, or state transitions. Skip glue code that only forwards calls, wires dependencies, or maps transport data; a decision inside an adapter still qualifies. If no targets qualify, skip acquisition and execution and report `mutation testing: skipped (no changed decision-heavy logic)` in the wrap-up and PR Quality row, without counts.

Prefer the ecosystem's mutation framework - Stryker (JS/TS), mutmut or cosmic-ray (Python), PIT (JVM), cargo-mutants (Rust), go-mutesting (Go).
These handle mutant generation, test selection, and reporting far better than a hand-rolled loop; write only the thin config that scopes them.
Hand-roll only when the ecosystem has nothing: an agent-written script that applies one mutation at a time (flip `<` to `<=`, `==` to `!=`, `+` to `-`, negate conditions, drop return values), runs the narrowest relevant test command, and records survivors.

Run incrementally:

- Intersect the branch diff with the selected decision logic. Prefer changed-line ranges; otherwise select changed functions, classes, or files at the narrowest granularity the installed version supports. Exclude glue within mixed files where supported. Do not expand to the whole repository just to seed a cache.
- Use explicit target selectors as well as incremental history/cache reuse. History alone can still generate an initial whole-repo run or report cached mutants outside the diff. Report only the selected scope; refresh affected results after test changes and invalidate history when dependency or configuration changes make it stale.
- StrykerJS: combine `mutate` file/line selectors with incremental mode ([docs](https://stryker-mutator.io/docs/stryker-js/incremental/)). PIT: select changed `targetClasses` and use history reuse ([docs](https://pitest.org/quickstart/incremental_analysis/)). mutmut: select changed modules/functions or paths using the installed version's selectors and reuse its cache ([docs](https://mutmut.readthedocs.io/en/latest/)). Check that version's help before constructing the command; these tools do not share a git-diff flag.
- Run the relevant tests covering the targets, including unchanged tests; use framework test selection where available.
- Set a per-mutant test timeout so an infinite-loop mutant cannot hang the run.
- Inspect survivors against the promised behavior. Strengthen or add regression tests for meaningful gaps and inspect nearby boundaries and rule combinations. Record equivalent, duplicate, or out-of-contract findings with reasons rather than writing tests merely to raise a score; observable but unpromised wording is out of contract, not equivalent.
- Report the selected targets, command, runtime, killed/survived/uncovered counts, useful test changes, and survivor dispositions. Re-run after relevant test changes; unresolved survivors remain reported under the two-round rule.

## 8. The newer kinds

Each runs only when its kind applies, over what the diff changed, and records the budget or scope it ran with.

- **coverage**: the repository's coverage runner over the suite at the change's impact, then diff-cover (or the tool's own changed-lines report) against the merge base; report the percentage of changed lines covered and the uncovered ranges.
- **property**: re-run the `[property]` tests with a larger example budget than build used (Hypothesis `max_examples`, fast-check `numRuns`, proptest `cases`), at least ten times the default; a counterexample is a violation with its shrunk input.
- **fuzz**: run each `[fuzz]` harness for its time budget (default 5 minutes per target) with the committed corpus; commit new interesting inputs, and keep each crash's minimized input as a regression test.
- **formal**: check the model the spec named - TLC over the TLA+ spec with its config, the Alloy analyzer over its commands - or run the stateful property test with a long command budget; a counterexample trace is a violation. For Lean, run `python3 {ship-skill-root}/scripts/proof-gate.py check --project formal --spec .dev/{plan-name}/spec.md --recheck` per [../../../references/proofs.md](proofs.md), then the differential `[property]` test with ten times its budget; for Rust, `cargo verus verify` or `cargo kani`. Record the proved statements and the toolchain in the kind's row.
- **smoke**: build the artifact as CI does, start it in the mocked environment of [../../build/references/mocking.md](../../build/references/mocking.md), hit its health path and main path, and stop it.
- **benchmark**: run the `[benchmark]` targets on the merge base and on the branch on this machine, in one session, with the tool's repetition and statistics (benchstat, criterion, JMH, pytest-benchmark compare); report the change with its confidence.
- **performance**: drive the running local build on base and branch with the same scripted load (k6, locust), command timing (hyperfine), or page audit (Lighthouse), and compare latency percentiles, throughput, memory, or load time.
- **ui-ux**: during the e2e pass, run axe-core (or pa11y) on every changed screen, compare screenshots with the base at each viewport the app supports, and check that every new control is reachable and operable by keyboard; an intended visual change is evidence, an unintended one is a violation.
- **usability**: one fresh-context agent gets the e2e screenshots in journey order (or the CLI's help and error output) and the spec's user-facing scenarios, and reports each problem against Nielsen's ten heuristics with the step where a user would get stuck.
- **threat-model**: for each `### Threat model` line, find the mitigation's test in the receipts; a mitigation with no passing test is a penetration finding.
- **penetration**: start the build on localhost, run OWASP ZAP's baseline scan (or its API scan with the OpenAPI document) and nuclei against it, then have one agent try each threat-model threat as an exploit against that local build; report each finding with its request and response.

## Fix-agent prompts

Keep them minimal - the violation list, the file paths, the fix vocabulary, nothing else.
The agents inherit no conversation and need none: a meaningful surviving mutant at `cart.ts:41 (< flipped to <=)`, the promised threshold behavior, and "add the regression test at the layer the surrounding tests use" is a complete task.
Include the relevant contract or expected behavior for mutation findings; do not paste tool source, the whole spec, or this reference into fix prompts.
