# Test Layers

Every scenario on a change set's `tests:` line in `spec.md` is tagged `[unit]`, `[integration]`, or `[e2e]`.
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

Two shapes to avoid: the **ice-cream cone**, where the suite is mostly e2e and every change costs a long red-green cycle, and the **hourglass**, where unit and e2e are both fat but nothing tests real collaboration, so integration bugs surface only in production.

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
It produces the HTML scenario report in [e2e-report.md](e2e-report.md): screenshots for frontend systems, before/after data-model state for everything else.
An e2e scenario describes user-observable, end-to-end behavior: "a guest can complete checkout with an expired coupon and sees the correct error."
Keep the count small and reserve it for critical journeys - each scenario is also a piece of evidence someone reads in the report.

## Where tests.md and mocking.md apply

[tests.md](tests.md)'s seams, behavior-over-implementation, tautology, and naming rules govern how you write a test at any layer.
[mocking.md](mocking.md) governs what may be replaced: architectural boundaries only at unit and integration, and the environment-only mocking that e2e is allowed.
