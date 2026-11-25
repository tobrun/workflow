# Test Layers

Every scenario on a change set's `tests:` line in `spec.md` is tagged with its layer: `[unit]`, `[component]`, `[integration]`, `[contract]`, `[property]`, `[fuzz]`, `[e2e]`, `[smoke]`, or `[benchmark]`.
The pyramid's three layers carry most scenarios; the other six prove what those three cannot, and each answers a kind in [../../../references/verification.md](../../../references/verification.md).
A scenario is not met until a concrete test exists at its tagged layer - a prose input -> outcome with no test behind it is not done, however confident the implementation looks.
The tag is what keeps tests acting as the spec: it says where the proof has to live, so it can't quietly stay in prose.

## The pyramid

Cost, speed, and breadth of failure differ by orders of magnitude between the layers, so their populations should too: many unit tests, fewer integration tests, few e2e scenarios.

| Layer | Population | Runtime | What it proves |
| ----- | ---------- | ------- | -------------- |
| Unit | most of the suite | milliseconds | one business rule, exactly |
| Integration | a middle band | sub-second to seconds | two owned components really agree |
| E2E | a handful per spec | seconds to minutes | a whole user journey actually works |

Two rules follow, and they matter more than the ratios:

- **Push every check down to the cheapest layer that can still fail for the right reason.** If a rule can be wrong in a pure function, test it in a pure function. Proving a rounding rule through a browser click is a slow test that also localizes badly: when it fails, it doesn't tell you where.
- **Push up only what lower layers structurally cannot see.** Wiring, configuration, serialization across a boundary, and the shape of a real user journey are invisible to a unit test no matter how many you write. That is what the upper layers are for, and why they exist at all.

Two shapes to avoid: the **ice-cream cone**, where the suite is mostly e2e and every change waits on a slow run, and the **hourglass**, where unit and e2e are both fat but nothing tests real collaboration, so integration bugs surface only in production.

## Unit

Pure business logic, no I/O.
Fast enough to run on every save.
Mock only at the true architectural boundaries in [mocking.md](mocking.md) - everything else inside the application stays real.
A unit scenario describes a computation or a business rule: "expired coupon is rejected," not "checkout endpoint returns 400."

## Integration

Validates real collaboration between two or more components this codebase owns, at the seam between them: a service against a real test database, a module against a real module it calls.
Never reaches the outside world.
Prefer real wiring or a fake over a mock at the boundary, per [mocking.md](mocking.md).
An integration scenario describes a cross-component contract: "the order API persists the order and a repository read returns it back."

## E2E

Drives the actual running application - the built artifact, not a test-harness shortcut - through a realistic end-user scenario, against the **mocked environment** of [mocking.md](mocking.md#the-e2e-environment).
It never runs against production or a shared staging environment; the point is a repeatable journey, not a live probe.
This is the only layer allowed to drive a browser.
It produces the JSON scenario report in [e2e-report.md](e2e-report.md): screenshots for frontend systems, before/after data-model state for everything else.
An e2e scenario describes user-observable, end-to-end behavior: "a guest can complete checkout with an expired coupon and sees the correct error."
Keep the count small and reserve it for critical journeys - each scenario is also a piece of evidence someone reads in the report.

## The other tags

- **Component** - one component through its public interface, its collaborators replaced by fakes: a UI component rendered and driven with the testing library the repo uses, a service behind fake ports. Cheaper than integration, broader than unit.
- **Contract** - a provider and its consumer agree on a boundary: a consumer-driven contract (Pact) verified on both sides, or the payload validated against the schema the provider publishes (OpenAPI, JSON Schema, protobuf with `buf breaking`). A third party's contract comes from its authoritative protocol data, per [mocking.md](mocking.md).
- **Property** - an invariant stated over generated inputs with the ecosystem's property tool (Hypothesis, fast-check, proptest, jqwik, rapid): round-trips, ordering, idempotence, conservation of totals. A stateful property test that drives a state machine with generated command sequences is also how `formal` is met when no model is warranted. Commit any shrunk counterexample as a plain regression test.
- **Proof** - when the spec's `formal` line names Lean, Verus, or Kani, per [../../../references/proofs.md](../../../references/proofs.md): write the reference model first, copy each `### Theorems` statement verbatim into `Spec.lean`, prove each one, and write the differential `[property]` test against the model's oracle. Loop `proof-gate.py check --spec` until it exits 0, or 2 with the unfinished proof logged as a deviation; an exit of 1 is never left standing, and a statement that cannot be proved goes to the user, never edited.
- **Fuzz** - a harness that feeds a parser, decoder, or other untrusted-input entry point with generated bytes (Atheris, Jazzer, `go test -fuzz`, cargo-fuzz, libFuzzer), with a small seed corpus committed beside it. Build proves the harness runs and its seeds pass; ship runs it for its time budget.
- **Smoke** - the built artifact starts in the mocked e2e environment and serves its main path. One or two per spec, run in the e2e pass.
- **Benchmark** - a repeatable measurement of the hot code with the ecosystem's benchmark tool and a stated budget or baseline. Build proves it runs; ship compares base against branch.

## Where tests.md and mocking.md apply

[tests.md](tests.md)'s seams, behavior-over-implementation, tautology, and naming rules govern how you write a test at any layer.
[mocking.md](mocking.md) governs what may be replaced: architectural boundaries only at unit and integration, and the environment-only mocking that e2e is allowed.
