# Review Lenses

Each lens is one agent in the panel.
Select only lenses with surface in the diff; every lens sees the whole diff but stays in its lane.
Prompt assembly and delivery live in [orchestration.md](orchestration.md); this file owns the lens definitions and the shared rules.

## spec-conformance

Include whenever a spec directory was found.
Checks the diff against `spec.md` - its scope, change plan, and decisions - not against general quality:

- Does each in-scope change set's `tests:` scenario exist as a real test, **at the layer it was tagged with** (`[unit|integration|e2e]`)? A scenario met by a test at the wrong layer (a unit test standing in for an e2e scenario) is a finding, not a pass.
- Do the scope section's invariants and error-handling entries hold in the diff? A broken invariant is a BLOCK.
- Does the diff implement each change set's `✓` decisions, without smuggling in a `✗` rejected alternative? A `⚠` accepted downside that manifests is expected, not a finding - the spec already admits it.
- Is a `docs/contracts.md` guarantee cited in the brief broken by the diff while its reliance sites still assume it? That is a BLOCK naming who gets hurt.
- Is every `[e2e]`-tagged scenario backed by a passing scenario record in `{plan-name}-e2e.json`? New user-facing behavior with no matching e2e scenario, or a scenario whose `status` is `fail`, is a BLOCK.
- Check `implementation-notes.md` for logged Deviations: does the conservative choice still satisfy the spec's scope and decisions, or does it need a call-out to the reviewer? An unresolved deviation that changes user-facing behavior from what the spec promised is a CONCERN at minimum.
- If a change set named a README or user-facing doc file, is that change present in the diff? A promised doc update that never happened is a CONCERN.
- Does the diff do significant work no change set describes? Scope creep is a CONCERN, not a crime; name it so the reviewer can decide.

## correctness

Bugs the compiler and tests would miss.
Walk the hardest paths first: concurrency and ordering hazards, boundary conditions (empty, one, max, off-by-one), null/absent paths, mutation while iterating, broken invariants, state-machine holes, unsafe retries, error paths, time and timezone handling, resource lifecycle, type-system escapes.
For each suspect path: state the invariant, name the input that breaks it, trace it through the code.
A BLOCK must name the triggering input; "this could race" without a scenario is a CONCERN.

## security

Authn/authz gaps, injection, secrets in code or logs, unsafe deserialization, crypto misuse, path traversal, SSRF, supply-chain risk in new dependencies, overly broad permissions.
Rate exploitability in this codebase, not theoretical severity.

## architecture

Module boundaries, layering, dependency direction, abstraction quality, coupling introduced by the diff.
Judge coherence against the patterns already established in the surrounding code; deviation from an established convention is a finding, personal style preference is not.
When the repo keeps `docs/dependencies.md`, treat its edges as settled: a conforming diff needs no boundary debate, and a diff that edits the rules file is reviewed as the decision it is, not as incidental churn.
Read `docs/architecture.md` first, when the repo keeps one, for orientation on where things live and how they talk ([../../../references/architecture.md](../../../references/architecture.md)): a diff that reshapes a component, flow, or boundary the overview describes without updating it is a finding.

## tests

Test quality per this plugin's philosophy, not raw coverage:

- Tests must live at public seams; tests that mock internal collaborators, test private methods, or verify through side channels are implementation-coupled findings.
- Tautological tests (assertion recomputes the expected value the way the code does) are findings; expected values need an independent source of truth.
- New behavior in the diff without a test at its seam is a finding. (Missing e2e backing for user-facing behavior belongs to spec-conformance, not here.)
- Brittle patterns: global time/random patching, order-dependent tests, interaction assertions where a state assertion would do.

## simplify

Always include this lens; every diff has simplification surface.
The gauntlet's duplication, dead code, and complexity checkers are its deterministic front line, so spend this lens on what they cannot see: semantic duplication below the token threshold, generality nothing asked for, indirection that earns nothing.

Unnecessary complexity that a simpler version would avoid, with behavior held constant:

- Reinvented helpers: logic the codebase or standard library already provides; Grep for the existing helper before flagging and name it in the finding.
- Duplication introduced by the diff: the same logic now living in two places that will silently diverge.
- Speculative generality: abstractions, parameters, or config with exactly one caller and no planned second one.
- Needless indirection: layers that only forward calls, wrappers that wrap nothing.
- Wrong altitude: low-level mechanics inlined into high-level flow (or vice versa) that a small extraction would clarify.

Every finding must sketch the simpler alternative concretely enough that the verifier can check it preserves behavior; "this feels complex" is not a finding.
Mostly CONCERN and NIT; reserve BLOCK for duplicating an existing tested helper.
This lens is about making the new code smaller and clearer, not about bugs (correctness), boundaries (architecture), or deleting unused code (dead-code).

## performance

Algorithmic complexity on real data sizes, N+1 queries, hot-path allocations, blocking I/O on async paths, missing pagination or streaming for unbounded data, cache invalidation.
Only flag what is on a path that plausibly matters; micro-optimizations are NITs at best.

## api-design

Naming, type signatures, error returns, consistency with the codebase's existing API idioms, backwards compatibility of anything already public.

## errors-observability

Swallowed errors, catch-and-continue without logging, missing context in log lines at decision points, errors that will be undebuggable at 3 AM, retry loops without backoff or limits.

## docs

Comments and docs touched or needed by the diff: stale or now-misleading comments, missing WHY comments on non-obvious constraints, public API docstrings.
Doc updates promised by the spec belong to spec-conformance, not here.

## dead-code

The gauntlet's detector already caught provably unreferenced symbols; this lens hunts what needs reading, not reference counting.
Unreachable branches, commented-out code, leftover debug scaffolding, vestigial parameters, dead flags and config keys, code only its own tests still call.
Verify with Grep across the whole repo before flagging; check tests, dynamic string-keyed lookups, and external API surface.
If you cannot prove it dead, report at most a CONCERN and say what you could not rule out.

## Shared rules (include in every lens prompt)

Severity ladder:

- **BLOCK**: a concrete problem you can name with the scenario that triggers it; would misbehave in production or violates the spec.
- **CONCERN**: a path you cannot convince yourself is safe, or a gap worth a conversation; include what you checked.
- **NIT**: minor polish; skip NITs entirely on diffs over 15 files.

Stay in your lane; other lenses cover the rest.
A finding based on a guess about unseen code is not a finding.
Every finding needs file, line, a one-line title, and a 2-4 line detail naming the evidence.
Also report up to 5 genuinely good things your lens noticed.
