# Verification Kinds

Twenty kinds of verification, each with what it proves, when it applies, where it is written, and what a failure does.
`scope` plans them in the spec's `### Verification` block, `build` writes the tests they need, and `ship`'s verify phase runs or checks every one and records it as one row of `pr.md`'s Quality table.
A kind that does not apply is skipped with a reason, never left out: `lint-spec.py` refuses a spec missing one, and `pr-evidence.py check` refuses a Quality table missing one.
That table is the agent's account. In the PR, CI's `ci-evidence.py` re-measures what it can at the head commit - tests, test edits, fails-on-revert, changed-line coverage, each spec scenario's test - in a block only CI writes, with each finding listed under its check, per [../source/ship/references/pull-request.md](../source/ship/references/pull-request.md).

## The kinds

| Kind | Proves | Applies when | Written by | Run by |
| --- | --- | --- | --- | --- |
| `unit` | one business rule, exactly | any logic change | build: `[unit]` scenarios | build, ship |
| `functional` | a feature's behavior across the components this repo owns | a behavior change | build: `[integration]` scenarios | build, ship |
| `component` | one component through its public interface, collaborators faked (a UI component rendered and driven, a service behind fake ports) | a component with a public interface changes | build: `[component]` scenarios | build, ship |
| `contract` | provider and consumer agree on a boundary's shape and meaning | an API, message, schema, or third-party integration changes | build: `[contract]` scenarios | build, ship |
| `property` | invariants hold for generated inputs: round-trips, ordering, idempotence, totals | logic with a statable invariant | build writes `[property]` scenarios | ship: once as written, then with a larger budget |
| `fuzz` | no crash, hang, or unbounded resource use on malformed input | parsers, decoders, untrusted input, native or unsafe code | build writes `[fuzz]` harnesses | ship: on the seeds, then time-boxed |
| `formal` | a model of the design reaches no bad state, or a proved statement holds for every input | concurrency, a distributed protocol, a state machine, lock or message ordering; a deterministic kernel whose bug would be costly or silent | scope decides the model and approves any statements; build writes it | ship model-checks it or runs the proof gate |
| `static-analysis` | types, security rules, secrets, vulnerable dependencies, dead code, duplication, complexity | any code change | the repository's config | ship |
| `lint` | style and format, plus the tests of the repository's own rules (ast-grep or Semgrep fixtures, ESLint RuleTester, `arch.py redteam`) | any change | the repository's config | ship |
| `coverage` | the changed lines are executed by tests | any code change | - | ship |
| `mutation` | the tests notice a change to the changed decision logic | changed decision-heavy logic | - | ship |
| `regression` | existing behavior holds, and a fixed bug stays fixed | always; a bug fix adds a test red on the base and green on the branch | build | ship |
| `acceptance` | every spec scenario passes at its tagged layer | a spec exists | build: every `tests:` line | build (`check-tests.py --deferred`) for the fast layers, ship (`check-tests.py`) for all |
| `smoke` | the built artifact starts and serves its main path | a runnable artifact changes | build writes `[smoke]` scenarios | ship, in the e2e pass |
| `benchmark` | hot code is no slower than on the base | a hot path, an algorithm, a data structure, or a stated budget | build writes `[benchmark]` scenarios | ship: once, then base against branch |
| `performance` | latency, throughput, memory, or load time of the running system is no worse than on the base | a user-facing endpoint, query, startup, or page changes | - | ship, base against branch |
| `ui-ux` | accessibility, no unintended visual change, every viewport, keyboard reachability | a UI changes | ship's e2e pass screenshots | ship |
| `usability` | a user can finish the task: Nielsen's ten heuristics and a task walkthrough over the screenshots, or a CLI's help, errors, and defaults | a user-facing flow, CLI, or API surface changes | - | ship, a fresh-context agent |
| `threat-model` | each trust boundary's STRIDE threats have a mitigation tied to a test | auth, permissions, untrusted input, secrets, stored user data, a new network edge | scope: `### Threat model` | ship checks each mitigation's test |
| `penetration` | the threat model's threats are not exploitable on the running build | whenever `threat-model` applies | - | ship, against the local build only |

## Scenario tags

A `tests:` scenario carries one tag, the layer whose test proves it: `[unit]`, `[component]`, `[integration]`, `[contract]`, `[property]`, `[fuzz]`, `[e2e]`, `[smoke]`, or `[benchmark]`.
`[integration]` is how this workflow writes functional tests, and `[e2e]` scenarios are the acceptance journeys; [../source/build/references/layers.md](../source/build/references/layers.md) says what belongs at each layer.
A kind marked `yes` in the Verification block that has a tag needs at least one scenario with that tag, and a tag needs its kind marked `yes`.
Build runs the `[unit]`, `[component]`, `[integration]`, and `[contract]` tests it writes; the other five tags are deferred layers, written by build and run first by ship, per [../source/build/references/layers.md](../source/build/references/layers.md#deferred-layers).

## The spec's Verification block

Under `## Scope`, one line per kind, every kind exactly once:

```
### Verification

- unit: yes - pricing rules in cart.ts
- fuzz: no - no parser and no untrusted input in this change
- formal: yes - the order state machine as a stateful [property] test (D-order-model)
- threat-model: yes - the coupon endpoint takes user input; see Threat model
```

`formal: yes` names its mechanism, decided as a `D-` decision: a TLA+ model checked with TLC or an Alloy model when the design's correctness lives in interleavings or protocol steps; a Lean reference model with proved statements and a differential `[property]` test against it when a deterministic kernel has a clean spec; Verus or Kani when that kernel is Rust; otherwise a stateful property-based test that drives the state machine with generated command sequences.
A Lean mechanism needs a `### Theorems` block: per statement, `- {name}: {plain-English reading}`, a fenced `lean` block stating `def {name} : Prop := ...`, and a `witness:` line with a concrete `example` or `#guard` showing it is not vacuous, per [proofs.md](proofs.md).
`threat-model: yes` needs a `### Threat model` block: per trust boundary, each STRIDE threat that applies as `- {boundary}: {S|T|R|I|D|E} {threat} -> {mitigation} - tests: {scenario tag and text}`.

## Tools

Use the repository's own tool first, then the ecosystem's established one, per [../source/ship/references/tools.md](../source/ship/references/tools.md).

| Kind | Python | TS/JS | Go | JVM | Rust | C/C++ |
| --- | --- | --- | --- | --- | --- | --- |
| `property` | Hypothesis | fast-check | rapid | jqwik | proptest | RapidCheck |
| `fuzz` | Atheris | Jazzer.js | `go test -fuzz` | Jazzer | cargo-fuzz | libFuzzer |
| `contract` | Pact, schemathesis | Pact, schema validation | Pact | Pact, Spring Cloud Contract | Pact | protobuf `buf breaking` |
| `coverage` | coverage.py + diff-cover | c8/istanbul + diff-cover | `go test -cover` + diff-cover | JaCoCo + diff-cover | cargo-llvm-cov | gcov/llvm-cov |
| `benchmark` | pytest-benchmark | tinybench, vitest bench | `go test -bench` + benchstat | JMH | criterion | Google Benchmark |
| `performance` | k6, locust, hyperfine, Lighthouse for pages | | | | | |
| `ui-ux` | axe-core or pa11y through Playwright, Playwright screenshot comparison | | | | | |
| `penetration` | OWASP ZAP baseline or API scan, nuclei, plus an agent's exploitation attempt per threat | | | | | |
| `formal` | TLA+ with TLC, Alloy, a Lean 4 model checked by `proof-gate.py`, or the stateful mode of the property tool | | | | Verus, Kani | |

## Gates

Every failure first goes to fix agents and is re-run, up to three rounds, per [../source/ship/references/verify.md](../source/ship/references/verify.md).
What happens after the third round depends on the kind:

- **Must pass**: `unit`, `functional`, `component`, `contract`, `acceptance`, `regression`, `smoke`, `static-analysis`, and `lint`. A failure left after three rounds is a human call, and the PR stays a draft.
- **Fix and report**: `coverage` under 80% of changed lines, a `benchmark` or `performance` regression over 10% against the base, any `fuzz` crash or hang, a `property` or `formal` counterexample, any medium-or-higher `penetration` finding, a serious or critical `ui-ux` accessibility violation, and `usability` findings. What survives three rounds is listed under Open calls; it never blocks the ship and never by itself makes the PR a draft.
- **Integrity**: a `formal` proof that cheats - a banned construct, a foreign axiom, a proof of another statement, or a statement that differs from the approved one, per [proofs.md](proofs.md) - is must-pass, though an unfinished proof is fix-and-report.
- **First runs**: a deferred-layer test that fails its first run, as written, is an `acceptance` failure and must pass; only what a larger budget or a longer run finds follows the kind's own gate.
- **Reported**: `mutation`, per [../source/ship/references/verify.md](../source/ship/references/verify.md).
- `threat-model` is checked, not run: a mitigation with no passing test is a `penetration` finding.

The margins are recorded decisions like every threshold; a repository's own `D-` entry replaces them.
A found secret stops the run at once, whatever the kind.

## Limits

- Penetration testing targets only the build this run started on localhost, never a shared, staging, or production host, and never a third party's service.
- Benchmarks and performance runs compare base and branch on the same machine in the same run, with enough repetitions for the tool's own statistics (benchstat, criterion, JMH) to call a difference; a single timing is noise, not a finding.
- A fuzz or property run's clean result is a time-boxed observation, not a proof; report the budget it ran with.
- A proof certifies the model against its statements, not the shipping code and not that the statements say the right thing; the differential test ties the code to the model, and a person's reading of the statements is the rest.
