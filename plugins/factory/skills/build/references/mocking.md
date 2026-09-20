# Mocking Guidelines

Mocks exist to make tests deterministic and fast, not to isolate every class from every other class.
Every mock is a place where the test stops verifying reality, so use as few as possible.

## Mock only at architectural boundaries

Replace a dependency only when it crosses a boundary you don't control in the test:

- Network calls to third-party services (payment providers, external APIs)
- The system clock and randomness
- Anything genuinely slow or flaky in a test environment (email delivery, message queues)

Everything inside the application - services, repositories wired to a test database, domain objects - should be real.
If the internal wiring is wrong, a test full of internal mocks will still pass, which makes it worse than no test.

## Never mock internal collaborators

If you feel the need to mock a class your own codebase owns, that is a design signal, not a testing problem.
Either test at a higher seam where the collaborator can just run, or the collaborator itself is a seam worth agreeing on with the user.

The tell that a mock is wrong: the test asserts *that* a method was called (`toHaveBeenCalledWith`) instead of *what the outcome was*.
Interaction assertions couple the test to the implementation; state and output assertions couple it to behavior.

## Prefer fakes over mocks at the boundary

When you do replace a boundary dependency, prefer a small working fake over a per-test stub:

- An in-memory repository instead of stubbing each query
- A fake clock you can advance instead of freezing time per test
- A recording fake for outbound calls (a fake mail sender with a `sent` list) instead of call-count assertions

Fakes keep behavior consistent across tests and survive refactors; ad-hoc stubs encode one test's assumptions.

## The e2e environment

E2E has no mocks *inside* the application and no real world *outside* it.
The application under test is the real built artifact with its real internal wiring; everything it depends on beyond its own boundary is mocked, seeded, and deterministic:

- Third-party HTTP replaced by a local stub server or recorded fixtures, with no credentials that could reach a real provider.
- A dedicated database or store, seeded to a known state before the run and torn down after.
- A fixed clock and seeded randomness, so the same scenario produces the same report twice.
- Outbound side effects (mail, payments, webhooks, queues) captured by a recording fake rather than delivered.

Never point an e2e run at production or a shared staging environment.
A live run makes the scenario unrepeatable, its before/after state unreliable as evidence, and its side effects real.
If the mocked environment for a dependency doesn't exist yet, building it is part of the work, not a reason to fall back to the real one.

## Determinism

Inject the clock and randomness at the seam rather than patching globals.
A test that patches `Date.now` globally is fragile and can leak into other tests; a component that accepts a clock is testable by construction.
