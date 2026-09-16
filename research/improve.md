# Execution plan: make the software factory reliably verifiable and recoverable

Date: 2026-09-16
Status: proposed execution plan
Audience: junior engineer implementing the improvements in small, reviewable changes

## 1. Goal

A completed factory run must carry execution evidence for its approved acceptance scenarios on the exact revision published in its pull request.
An interrupted run must recover with one active executor, correct retry accounting, and reusable evidence whose inputs still match.
Each release must demonstrate these properties through automated negative cases and representative real-host evaluations.

This plan covers all 14 improvement items and all eight reproduced cases in [the evaluation](../todo/factory-evaluation.md).
The evaluation explains the findings; this document defines the research, implementation order, and completion checks.

### Current baseline

- The implementation has a fixed `scope -> scope-review -> build -> ship` pipeline.
- Interactive scope establishes intent, and the remaining stages run unattended.
- The runner uses Python's standard library, local state, detached workers, worktree isolation, and cross-process locks.
- Repository validation passed during the evaluation, including 187 discovered runner tests and offline CLI E2E tests.
- Eight extra diagnostic probes exposed accepted failure cases.
- Local records show one real run reaching a ready PR after an authentication-related retry.
- That run recorded 32,536,837 tokens, including heavily cached inputs.
  Its recorded success is useful evidence, but one run does not establish a reliability rate.

Recheck this baseline before implementation because the reviewed factory files were uncommitted and may continue to change.

## 2. How to use this plan

1. Complete Phase 0 before changing behavior.
2. Follow the phase order and dependency table below.
3. Within a work package, reproduce the failure, define the contract, implement one vertical slice, and verify the observable result.
4. Keep each listed delivery slice small enough to review independently.
5. Update the corresponding checkboxes only when their acceptance criteria have evidence.
6. Record research decisions and deviations beside the relevant work package, including the reason and the test that validates the choice.

### Repository rules

- Follow `CLAUDE.md` and applicable repository instructions.
- Preserve the fixed four-stage pipeline and fresh-context attempts.
- Keep runner state single-writer and retain conservative worktree cleanup.
- Implement factory changes in `factory/`.
  Its skills intentionally diverge from `dev/`.
- Generate `plugins/` using `scripts/build_codex_plugin.py`.
  Never edit generated files directly.
- Keep shared skill-facing helpers in a shipped directory such as `factory/scripts/`.
  The generated plugin does not include `factory/runner/`, `factory/bin/`, or `factory/evals/`.
- Preserve human-triggered skill metadata and the runner's sanctioned stage invocation.
- Keep detailed protocols in references and executable checks rather than growing `SKILL.md` rule lists.
- Keep `.dev/` plan artifacts out of target repository commits.
- Preserve unfamiliar working-tree changes and obtain consent before changing branches or pushing.
- Do not modify generated changelogs or add agent co-author trailers.
- Put each full sentence on its own line in Markdown and use plain dashes.

### Per-change completion rule

Every behavioral change must include a meaningful regression test that fails for the original defect and passes for the fix.
Prefer the CLI plus an isolated fixture repository when the behavior is visible through a factory command.
For parsers and pure aggregation logic, supplement the CLI case with focused input/output tests.
Verify successful behavior as well as rejection of the failure case.
Do not turn a failure into a passing result by loosening thresholds, disabling assertions, or skipping required checks.

## 3. Coverage and implementation order

Work-package numbers match the original evaluation item numbers.
Priority expresses the severity of the gap; execution order also accounts for prerequisite contracts.
Research and regression-case design for W10 begin in Phase 0, even though the full behavioral benchmark lands later.

| Evaluation item | Work package | Priority | Phase | Prerequisites |
| --- | --- | --- | --- | --- |
| 1. Parse artifacts as data only | W01 | P0 | 1 | Phase 0 |
| 2. Require scenario execution evidence | W02 | P0 | 2 | W01, W08, W09, W11 |
| 3. Bind verification to the PR revision | W03 | P0 | 2 | W02, W05, W07, W09, W11 |
| 4. Preserve one executor during recovery | W04 | P0 | 1 | Phase 0 |
| 5. Require complete review coverage | W05 | P0 | 2 | W01, W06, W09 |
| 6. Enforce the park policy | W06 | P1 | 1 | W04 |
| 7. Enforce stage boundaries | W07 | P1 | 2 | W04, W06 |
| 8. Supervise validation | W08 | P1 | 1 | W04 |
| 9. Version intent and handoffs | W09 | P1 | 2 | W01, W07 |
| 10. Add behavioral evals | W10 | P1 | 3 | W01-W09, W11 |
| 11. Reproduce repository setup and runtime | W11 | P1 | 2 | W08, W09 |
| 12. Budget nested work and measure usage | W12 | P2 | 4 | W04, W08, W10, W11 |
| 13. Checkpoint subphases | W13 | P2 | 4 | W03-W06, W09, W11, W12 |
| 14. Learn from engineering outcomes | W14 | P2 | 4 | W09-W13 |

Recommended serial order:

```text
Phase 0: baseline, reproductions, shared contract sketches
Phase 1: W01 -> W04 -> W08 -> W06
Phase 2: W07 -> W09 -> W11 -> W02 -> W05 -> W03
Phase 3: W10
Phase 4: W12 -> W13 -> W14
```

## 4. Phase 0: establish a reproducible starting point

### Read the implementation

Start with these files, in order:

1. `factory/README.md` and `factory/references/factory-run.md` for operator behavior and the stage protocol.
2. `factory/runner/pipeline.py`, `model.py`, and `worker.py` for stage execution and state transitions.
3. `factory/runner/gates.py` for the current authoritative completion checks.
4. `factory/runner/supervise.py`, `control.py`, and `slots.py` for ownership, recovery, and concurrency.
5. `factory/runner/tests/helpers.py`, `test_worker.py`, and `scripts/test_factory_runner.sh` for fixture construction.
6. `factory/skills/build/SKILL.md`, `factory/skills/ship/SKILL.md`, and their referenced check scripts.
7. `scripts/build_codex_plugin.py`, `scripts/validate.sh`, and `.github/workflows/validate.yml` for packaging and CI.

### Establish the baseline

- [x] Record the checkout revision and `git status --short` output so pre-existing changes are distinguishable.
- [x] Run the repository validation command in section 11 and retain the result.
- [x] Count and record discovered runner tests rather than assuming the count is still 187.
- [x] Document how a run directory, worktree, stage attempt, child process, slot, and worker lock relate.
- [x] Identify which state survives a worker crash and which process continues after the worker dies.
- [x] List the exact places where the runner trusts agent-authored files.

### Recreate the eight diagnostic cases

Use temporary `FACTORY_HOME` directories, local bare Git origins, and the supplied `codex`, `claude`, `gh`, and `acli` stubs.
Set the existing `FACTORY_*_BIN` overrides so offline cases never invoke authenticated external services.
Set temporary Git author configuration locally, as the current fixture helpers do.
Prefer the environment's approved temporary directory.

The evaluation links a temporary probe script that may no longer exist when implementation begins.
Reconstruct the cases from this table and the existing fixture helpers so permanent tests do not depend on that machine-specific file.

| Case | Reproduction | Required behavior after the fix | Owner |
| --- | --- | --- | --- |
| R1 | Run `factory new` with named tests appearing only in comments and an E2E summary claiming success with `scenarios: []`. | Required test execution and E2E coverage are rejected as absent. | W02 |
| R2 | Supply otherwise passing artifacts with a non-retryable build result carrying an unresolved missing-credentials condition. | The condition prevents completion and remains visible. | W06 |
| R3 | Have scope review create and commit a source file before returning approved. | The stage fails with the committed unauthorized path. | W07 |
| R4 | Parse an E2E object containing an expression that writes a marker inside the test's temporary directory. | Parsing rejects the expression and creates no marker. | W01 |
| R5 | Aggregate empty lens and verifier directories while specifying expected mandatory lenses. | Review is incomplete and cannot satisfy ship. | W05 |
| R6 | Begin validation with cancellation requested or with the attempt deadline expired. | No validation command starts; an active command is terminated on cancellation. | W08 |
| R7 | Commit and push a change after creating passing review and evidence artifacts. | Old verification cannot satisfy ship for the changed revision. | W03 |
| R8 | Kill the worker while its host remains alive, then invoke `factory resume --detach`. | Recovery never leaves two active executors for the worktree. | W04 |

Capture the original failing assertions before fixing each case.
Land each permanent regression test together with its fix so the default branch remains green.
Keep process cleanup targeted to identities created by the test, and verify no child or worktree registration leaks.

### Sketch shared contracts before implementing consumers

Document the following logical records in `factory/references/factory-run.md` as their work packages introduce them.
Choose exact filenames during implementation and update every producer and consumer together.
The names below are proposed concepts, not existing APIs.

| Record | Minimum contents | Authority |
| --- | --- | --- |
| Approved intent | Original request, scenario IDs and requirements, non-goals, handoff timestamp, content hash | Runner snapshot at the existing scope handoff |
| Attempt inputs | Run/attempt identity, source revision, intent/spec/config/tool hashes, referenced input artifacts | Runner |
| Execution receipt | Command ID, argv or script hash, cwd, execution boundary, timestamps, exit classification, raw-output hashes, source revision | Runner or runner-owned executor |
| Scenario result | Scenario ID, collected test ID or driver ID, declared layer, outcome, execution-receipt reference, evidence references | Validated against execution receipts |
| Review result | Expected and completed lenses, findings, verifier coverage, completeness, verdict, input fingerprints | Checked by the aggregator and runner |
| Gauntlet result | Required checks, applicability, tool versions, thresholds, scope, execution receipts, outcomes | Checked by the runner |
| Blocking condition | Stable code, category, evidence references, retryability, resolution state | Runner validates stage claims and records transitions |
| Runtime manifest | Runner and resolved skill versions, host versions, model, effective configuration, repository contract | Runner |
| Checkpoint | Subphase, input fingerprint, output hashes, revision, completion receipt | Runner |

Write validation rules once in a location available to both the runner and generated skill scripts.
For example, a pure helper under `factory/scripts/` can be shipped with the plugin and loaded by a runner adapter.
Test that arrangement in both source and generated layouts before using it throughout the pipeline.
Treat hashing as an integrity and freshness mechanism, not proof that an agent-authored result is truthful.

### Phase 0 exit criteria

- [x] The existing suite is green and its baseline is recorded.
- [x] Every R1-R8 case has a reproducible setup and an owning work package.
- [x] Proposed record ownership is documented without giving agents control of runner-owned completion state.
- [x] The engineer can explain the current failure of each case before editing the implementation.

## 5. Phase 1: fix execution and outcome foundations

### W01: parse artifacts as data only

**Finding covered:** evaluation item 1 and R4.

**Read:** `factory/skills/ship/scripts/pr-evidence.py`, `factory/runner/gates.py`, `factory/skills/build/references/e2e-report.md`, and `factory/skills/build/templates/e2e-report.html`.

**Research:** Trace every producer and reader of `E2E_DATA`.
Confirm the eager call to `node_json()` occurs even when the original literal is valid JSON.
Inventory existing report formats before selecting a compatibility policy.

**Delivery slices:**

1. Remove executable parsing from the factory path and add a strict JSON loader with a schema discriminator.
   Reject expressions, non-object roots, invalid types, duplicate keys, non-finite numbers, and unsupported schema versions with a typed error.
   Check a documented size limit before reading the whole artifact.
2. Write E2E results to a JSON sidecar and render the HTML from the validated sidecar.
   Update build instructions, PR evidence extraction, and runner gates to use the same record.
   Treat HTML as a presentation artifact.
3. Define legacy behavior explicitly.
   An old report containing strict JSON may use a data-only compatibility reader; unsupported JavaScript literals require regenerated evidence.
   Any converter must have bounded input and runtime and must never execute the object literal.

**Acceptance:**

- [x] R4 fails safely, with no marker creation and no JavaScript runtime launched by the loader.
- [x] Missing, malformed, oversized, and unsupported reports produce actionable bounded failures.
- [x] A valid sidecar produces the expected HTML and PR evidence content.
- [x] Source and generated plugin layouts both load the shared validator.
- [x] Existing runs with legacy reports remain inspectable and receive an explicit regeneration path when needed.

**Verify:** Add parser tests and exercise a full fixture build-to-ship path with the new sidecar.

### W04: preserve one active executor and make completion recoverable

**Finding covered:** evaluation item 4, R8, and the finished-attempt/unsaved-transition crash window.

**Read:** `factory/runner/supervise.py`, `worker.py`, `model.py`, `events.py`, `control.py`, and `slots.py`.

**Research:** Draw the process tree for the detached worker and its independently sessioned host.
Check process-start identity support on macOS and Linux.
Identify the gap between spawning a child and persisting its PID, and the gap between saving a finished attempt and saving its next transition.

**Delivery slices:**

1. Introduce executor ownership that survives worker loss.
   Prefer a small runner-owned execution guard that holds the executor lease and stage slot while supervising the child process group.
   The worker remains the sole writer of `run.json`; the guard writes its own execution receipt.
   Register run/attempt identity, ownership token, PID, process-start identity, and process group before allowing child work to begin.
2. Reconcile the execution guard and child before resuming, retrying, cancelling, or removing a run.
   Reattach to a verified live execution or terminate that execution before replacement.
   Ambiguous identity must prevent a replacement launch and must never cause an unrelated process to be killed.
   Keep the lease until the executor and descendants have stopped.
3. Compute the finished attempt, retry charge, and next run status before the atomic state save.
   Add a durable transition identity so recovery can distinguish committed decisions from incomplete work.
   Reconcile missing or duplicate event-log entries against the authoritative state instead of replaying side effects blindly.
4. Reconcile external side effects after crashes by their actual state.
   Reuse an already-created PR for the run branch and recognize recorded commits without issuing duplicate work.

**Acceptance:**

- [x] R8 observes at most one active executor, including the spawn-registration window.
- [x] Worker death does not make the active execution slot appear free prematurely.
- [x] PID reuse and ambiguous ownership leave unrelated processes untouched.
- [x] Crash injection before and after gate persistence advances a completed stage exactly once.
- [x] Crash injection around commit and PR publication preserves one intended side effect.
- [x] Retry charges are neither lost nor duplicated across recovery.
- [x] Cancel and cleanup account for a surviving executor even when the worker lock is free.

**Verify:** Extend worker, control, slot, and CLI tests using real local subprocesses and fixture-owned process groups.
Use deterministic test barriers to crash at each boundary instead of relying on timing guesses.

### W08: supervise validation under the same attempt limits

**Finding covered:** evaluation item 8 and R6, including multiline command semantics and host-side execution boundaries.

**Read:** `factory/runner/gates.py`, `supervise.py`, `worker.py`, and `factory/references/ci-parity.md`.

**Research:** Inventory all blocking subprocesses and waits in gates, including script helpers, Git operations, GitHub checks, and report conversion.
Determine which share the stage deadline and how cancellation reaches each process tree.

**Delivery slices:**

1. Extend the W04 execution abstraction to runner validation commands.
   Compute one attempt deadline and pass its remaining time through command execution and polling.
   Check cancellation and remaining time before every launch and during every wait.
   Preserve explicit `cancelled`, `timeout`, spawn failure, and nonzero-exit classifications.
2. Replace line-by-line Markdown execution with an explicit command record.
   Support an argv command or an executable repository script with its cwd, declared environment inputs, timeout cap, and execution boundary.
   A shell program must execute as one declared script so `cd`, exports, continuations, and control structures retain their intended meaning.
3. Run validation through the declared repository execution environment.
   Document how that boundary compares with the agent host's sandbox and verify the actual filesystem/network behavior on supported hosts.
   Produce a runner-owned execution receipt and raw logs for every check.

**Acceptance:**

- [x] R6 starts no command when cancelled or already past the deadline.
- [x] Cancelling active validation stops the command and its descendants within the configured termination grace.
- [x] Several validation commands cannot each extend an exhausted stage by another 20 minutes.
- [x] A fixture script using a changed directory, an environment variable, and a multiline conditional executes correctly.
- [x] Gate time is recorded separately and included in total attempt elapsed time.
- [x] Validation output remains available after timeout, cancellation, or worker recovery.

**Verify:** Add CLI cancellation-during-validation coverage plus focused supervisor and command-contract cases.

### W06: enforce park conditions in the authoritative outcome

**Finding covered:** evaluation item 6 and R2.

**Read:** `factory/references/factory-run.md`, `factory/runner/gates.py`, `model.py`, `worker.py`, and `factory/runner/tests/test_gates.py`.

**Research:** Enumerate the existing fixed park list and every place a stage can emit it.
Distinguish an unresolved prerequisite, a failed verification, an invalid artifact, and an ordinary advisory model statement.

**Delivery slices:**

1. Add typed condition and failure codes to the stage result contract.
   Cover the declared premise/new-effort, environment, destructive-action, unavailable-launch, and failed-recommendation cases with their existing retry policy.
   Validate category, evidence references, and resolution state.
2. Change outcome combination so a known unresolved blocking condition participates in the gate's completion predicate.
   A claimed success still cannot override a failed deterministic check.
   A recognized park condition cannot be reduced to a warning merely because files exist.
3. Define resolution on retry.
   Re-check objective prerequisites where possible and retain a resolution record referencing the original condition.
   Surface the stable code, current reason, evidence, and next action in CLI and watch output.
   Keep ordinary verification failures distinct from requests for human judgment.

**Acceptance:**

- [x] R2 remains blocked or parked with the condition visible.
- [x] Clearing a real prerequisite permits progress after successful re-verification.
- [x] Invalid result types and unknown mandatory condition values fail explicitly.
- [x] The success/failure/condition merge matrix has meaningful regression coverage.
- [x] Updated factory references preserve the existing unattended decision policy and pass repository wording checks.

**Verify:** Extend the full-run fixture and gate merge tests; check CLI output for both unresolved and resolved conditions.

### Phase 1 exit criteria

- [x] R2, R4, R6, and R8 have permanent passing regression tests.
- [x] Parsing cannot execute report content.
- [x] All attempt work has a cancellable execution boundary and a shared deadline.
- [x] Recovery preserves executor ownership and committed completion decisions.
- [x] The existing happy path still completes using fixtures compatible with the new contracts.

## 6. Phase 2: establish immutable intent and exact-revision evidence

### W07: enforce stage boundaries across the complete Git delta

**Finding covered:** evaluation item 7 and R3.

**Read:** `factory/runner/worktree.py`, `gates.py`, `pipeline.py`, `hosts.py`, and the scope and scope-review skill contracts.

**Research:** List permitted writes for each stage, including documents and runtime artifacts.
Inspect how linked-worktree Git paths and the common Git directory affect sandbox write access.

**Delivery slices:**

1. Capture branch, starting commit, index/worktree state, and permitted path classes before each attempt.
   Compare committed changes from that baseline as well as staged, unstaged, and relevant untracked files.
   Handle deleted files and both sides of renames with machine-readable Git output.
2. Enforce the scope and scope-review path contracts over the complete delta.
   Verify the expected run branch and acceptable history ancestry after every stage.
   Reject newly tracked `.dev/` runtime artifacts and report pre-existing tracked-path conflicts during preflight without rewriting user files.
3. Separate runner-owned control records from agent-writable reports, evidence, and scratch directories.
   Narrow sandbox grants to the necessary paths and prove they work in real linked worktrees.
   Record capabilities accurately for configured bypass mode instead of claiming sandbox protections there.

**Acceptance:**

- [x] R3 fails and names the committed unauthorized source file.
- [x] Staged-only, unstaged, deleted, renamed, and untracked forbidden changes are detected.
- [x] Branch changes, unexpected history replacement, and newly tracked `.dev/` files fail with precise reasons.
- [x] A legitimate scope document update and a legitimate build commit still pass.
- [x] The supported sandbox prevents agent writes to runner-owned completion records.

**Verify:** Extend worktree and gate tests, then exercise the actual host permission setup in the W11 smoke test.

### W09: version approved intent and stage handoffs

**Finding covered:** evaluation item 9, including acceptance drift and missing historical inputs.

**Read:** `factory/runner/cli.py`, `pipeline.py`, `model.py`, `worker.py`, `factory/skills/scope/SKILL.md`, and `factory/skills/scope-review/SKILL.md`.

**Research:** Identify exactly what the operator approves at scope handoff.
Separate user-visible requirements from technical decisions that unattended stages may refine.
Inventory inputs currently overwritten between attempts.

**Delivery slices:**

1. At the existing handoff, persist the approved request, non-goals, and acceptance scenarios with stable IDs and exact requirement text.
   Assign IDs once and preserve them across retries.
   Store the authoritative snapshot in the runner-owned location introduced by W07.
2. Keep the technical spec editable within the approved intent.
   Record decision deltas, affected scenarios, evidence, and auto-decision rationale.
   Required scenario deletion or modification must be detected by comparing the locked fields, rather than asking a model whether the new wording is equivalent.
   Permit additional verification scenarios without dropping approved ones.
3. Snapshot each attempt's exact input documents and record source, spec, intent, configuration, and artifact hashes.
   Save output manifests after verification.
   Copy only required inputs and artifacts; avoid duplicating the entire repository for every attempt.
4. Add read paths for CLI inspection and later export so operators can compare attempts without reconstructing overwritten files.

**Acceptance:**

- [x] Removing, weakening, or renaming away an approved scenario is detected.
- [x] An implementation-level refinement within the approved requirements can continue unattended with an audit record.
- [x] Attempt 1's spec and notes remain available after attempt 2 modifies the live files.
- [x] Tampered or missing snapshots invalidate reuse with an actionable reason.
- [x] Snapshots survive archiving and are referenced by stable logical paths.

**Verify:** Add handoff, retry, snapshot-integrity, and archive integration tests.

### W11: make repository execution and runtime provenance reproducible

**Finding covered:** evaluation item 11, including plugin drift, mutable configuration, model identity, and incomplete preflight.

**Read:** `factory/runner/cli.py`, `config.py`, `hosts.py`, `pipeline.py`, `requests.py`, and `scripts/build_codex_plugin.py`.

**Research:** Compare source skill paths, installed Codex plugin resolution, and the direct-path fallback.
Test which host capabilities can be checked without inference and which require a real smoke call.
Inventory setup, validation, app launch, services, ports, and expected CI checks in a representative target repository.

**Delivery slices:**

1. Define a versioned repository execution contract.
   Include setup commands, validation commands, E2E drivers, service lifecycle, port allocation, required environment names, and expected CI checks.
   Allow explicit no-CI or non-app cases with a repository-grounded reason.
   Reject incomplete command contracts before starting a long build.
2. Persist the effective runtime manifest per attempt.
   Capture runner revision and dirty-content fingerprint, resolved skill-bundle hash, host versions, model/effort, repository contract, and effective configuration.
   Record configuration changes at attempt boundaries instead of silently changing an active attempt's semantics.
   Preserve explicit live cancellation and resource-control behavior separately.
3. Extend doctor with a repository-aware mode.
   Implement and document its CLI flags rather than leaving them implicit in the plan.
   Run setup and command-access probes through the intended W08 execution environment.
   Verify linked-worktree Git access and the existing GitHub token-forwarding behavior.
   Report capability failures with exact repair steps.
4. Add an explicitly requested real-host smoke mode for skill/model resolution.
   Cache successful capability checks by host version, skill hash, model, and relevant configuration.
   Ensure a fresh attempt uses the resolved bundle whose identity it records.

**Acceptance:**

- [x] A missing launch prerequisite, absent required environment name, or incompatible skill bundle is detected before expensive execution.
- [x] Source and installed plugin mismatches cannot silently share one provenance label.
- [x] Every attempt identifies its effective runtime, including an uncommitted runner checkout.
- [x] Runtime records contain environment names and redacted descriptors rather than credential values.
- [x] A configuration change is observable and affects only its documented boundary.
- [x] Existing `.factory` configuration remains readable under the migration policy in section 10.

**Verify:** Add offline preflight/configuration fixtures and retain one actual sandbox capability receipt for each supported host configuration.

### W02: require execution-backed scenario evidence

**Finding covered:** evaluation item 2 and R1, including comment-only references, duplicates, inert tests, wrong layers, and inconsistent E2E totals.

**Read:** `factory/skills/build/scripts/check-tests.py`, `factory/runner/gates.py`, `factory/runner/tests/helpers.py`, and the build test-layer and E2E references.

**Research:** Identify the test collection and result formats already available in the representative repositories.
Start with the fixture repository's actual test framework and define a narrow adapter interface for additional frameworks.
Specify how browser-driven and non-frontend scenarios expose assertions and captured evidence.

**Delivery slices:**

1. Map each W09 scenario ID to a declared layer and executable test or driver ID.
   Validate actual test discovery and unique identities instead of searching for names in source text.
   Define explicit many-to-many mappings where one test covers several scenarios; repeated references must not manufacture coverage.
2. Execute the selected tests through W08 and collect structured framework/driver results into fresh runner-owned output directories.
   Correlate results with the execution receipt, command identity, source revision, and approved scenario mapping.
   Reject stale files, nonzero execution, missing results, skipped required scenarios, duplicate IDs, and mismatched layers.
3. Derive E2E totals from individual validated scenario records.
   Require each approved E2E scenario and verify evidence references exist and match their hashes.
   Record actual frontend screenshots or actual non-frontend before/after outputs from the driver.
   Preserve assertions and exit status separately from presentation content.
4. For bug fixes, execute the reproducing assertion on an isolated base revision and on the change revision.
   Require a behavior-relevant failure on the base and a pass on the change; an unrelated setup failure is not a reproduction.
5. Replace the full-pipeline happy fixture's inert tests and empty E2E records with a tiny real application and executing assertions.
   Keep lightweight stubs for process and retry mechanics, and add independent acceptance assertions for feature behavior.
   Demonstrate that an intentionally broken implementation or an inert reproducing test cannot satisfy those independent checks.

**Acceptance:**

- [x] R1 fails for missing test execution and missing E2E records.
- [x] Duplicate references and invented summary counters cannot satisfy scenario coverage.
- [x] A required skipped test, wrong-layer mapping, stale output, or nonexistent evidence file fails.
- [x] A deliberately broken fixture implementation is rejected despite a green-looking agent report.
- [x] A real valid implementation completes the entire fixture pipeline.
- [x] Bug-fix evidence contains a meaningful red-on-base and green-on-change pair.

Test discovery and exit status alone cannot establish assertion quality.
Use independent acceptance tests and negative controls in W10 to evaluate that remaining semantic property.

**Verify:** Extend gate tests and full CLI E2E coverage, then run the fixture application's independent acceptance checks.

### W05: require complete review and gauntlet evidence

**Finding covered:** evaluation item 5 and R5, including missing verifiers and absent gauntlet receipts.

**Read:** `factory/skills/ship/scripts/aggregate-findings.py`, ship orchestration and gauntlet references, scope-review panel instructions, and `factory/runner/gates.py`.

**Research:** Trace how expected lenses are chosen and how findings are numbered, verified, merged, and rendered.
List the gauntlet's required checks and its legitimate applicability rules, such as absent repository dependency rules.
Define how a PLAUSIBLE verifier outcome differs from a missing verifier.

**Delivery slices:**

1. Add strict schemas and separate `completeness` and `verdict` fields to aggregate results.
   Require all expected mandatory lenses and valid coverage of findings requiring verification.
   Treat missing or malformed results as incomplete execution.
   Never demote a finding solely because its verifier failed to return.
2. Store an input fingerprint for each lens and verifier task.
   Retry missing tasks within a bounded policy while reusing successful results only when inputs match.
   Preserve confirmed, refuted, and explicitly uncertain outcomes as distinct records.
3. Emit structured gauntlet results with applicability, tool version, checked scope, threshold source, execution receipt, revision, and surviving findings.
   Every mandatory applicable check must have its required passing outcome.
   An inapplicable check needs a validated repository-contract reason; unavailable tools and failed execution are not inapplicability.
4. Make runner gates consume these validated records and compare their fingerprints to current inputs.
   Render Markdown and HTML verdicts from the same records.
   Apply completeness handling to scope-review as well as ship.

**Acceptance:**

- [x] R5 produces incomplete review and cannot make a run ready.
- [x] A missing mandatory verifier cannot turn a reported blocker into a shippable concern.
- [x] Malformed or duplicate task results are rejected with task identities.
- [x] A missing mandatory gauntlet receipt or surviving mandatory violation blocks completion.
- [x] A bounded retry reruns only missing tasks when valid inputs are unchanged.
- [x] Complete, correctly verified reviews still produce PASS, CONCERNS, or BLOCK according to the documented policy.

**Verify:** Add standalone aggregator cases plus full-run incomplete-review and missing-gauntlet scenarios.

### W03: verify the exact final PR revision and published evidence

**Finding covered:** evaluation item 3 and R7, including stale reviews, changed remote heads, wrong base, local-only PR evidence, and absent expected CI checks.

**Read:** `factory/runner/gates.py`, `worktree.py`, ship PR/evidence references, and `factory/runner/tests/stubs/gh`.

**Research:** Use `gh` help and actual structured outputs to establish available repository, base, head, body, check, and merge-state fields.
Determine how the target repositories express required CI checks and when those checks become visible after pushing.

**Delivery slices:**

1. Bind test, E2E, gauntlet, and review receipts to the candidate source revision and relevant W09/W11 inputs.
   Re-check final tracked and relevant untracked state after verification commands, since a check can modify files.
   Invalidate affected receipts whenever ship's fix or remediation phases change their inputs.
2. Read the actual PR's repository identity, base ref, head ref, exact head SHA, draft state, body, and checks.
   Require local HEAD, fetched remote branch SHA, and PR head SHA to agree.
   A failed fetch must not fall back to a stale remote-tracking ref.
   Compare published evidence to the expected evidence manifest rather than checking only local `pr.md`.
3. Wait for the W11 expected checks for that SHA to appear and finish under W08's deadline.
   Distinguish missing, pending, successful, failed, cancelled, skipped, and unknown outcomes according to explicit repository rules.
   Accept no checks only for a contract explicitly declaring no CI requirement.
4. Recheck the candidate SHA and critical PR state before recording completion.
   If the candidate changes during polling, restart the affected verification decision within the same bounded attempt.
   Store the exact completion observation and timestamp so later PR changes do not rewrite history.

**Acceptance:**

- [x] R7 rejects the old review and evidence after a new commit.
- [x] Wrong repository/base/head, a remote-ahead branch, divergence, and failed refresh cannot pass as synchronized.
- [x] Local evidence absent from the published body fails completion.
- [x] Expected CI checks missing just after a push are waited for rather than treated as no CI.
- [x] Pending, failed, or disallowed skipped checks cannot satisfy the final predicate.
- [x] A correctly configured no-CI repository can complete.
- [x] Post-fix verification and review cover the exact final candidate revision.

**Verify:** Extend the GitHub stub to model revision-bound state and changes during polling, then retain one real-host PR-state receipt in W10.

### Phase 2 exit criteria

- [x] All R1-R8 cases now have permanent regression coverage and pass their rejection assertions.
- [x] Required intent is immutable while technical refinements remain auditable.
- [x] Every completion receipt identifies executed checks, current inputs, and the exact PR revision.
- [x] Full CLI happy paths execute a real fixture application and continue to succeed.
- [x] Generated plugins remain usable without importing runner-only modules.

## 7. Phase 3: establish behavioral release evidence

### W10: add behavioral factory evals and platform coverage

**Finding covered:** evaluation item 10, including comprehension-only skill coverage, stub-only pipeline coverage, and a single observed real success.

**Read:** `factory/evals/`, `dev/evals/README.md`, `factory/runner/tests/`, `scripts/test_factory_runner.sh`, and `.github/workflows/validate.yml`.

**Research:** Choose fixture tasks that represent actual factory usage and have independently measurable outcomes.
Specify the approved scope input and expected post-handoff behavior for each task before running a model.
Inventory the declared Python minimum and real supported host platforms.

**Delivery slices:**

1. Add a versioned task manifest and executable benchmark harness under `factory/evals/`.
   Include a bug fix, feature, UI change, dependency problem, ambiguous requirement, and interrupted run.
   Separate full interactive-scope evaluation from headless evaluation seeded with an approved intent snapshot.
   State which mode each result measures.
2. Keep independent acceptance tests outside the agent-writable target and run them against the produced revision.
   Include negative controls with a broken implementation, inert test bodies, fabricated evidence, missing review, and stale artifacts.
   Persist the exact task version, runtime manifest, attempts, grader output, and resulting revision.
3. Add fast negative cases to ordinary offline CI.
   Add macOS and Linux jobs covering the supported Python minimum and a current supported version.
   Print actionable failing test output and retain logs on failure instead of suppressing all diagnostics.
4. Run real-host evals on relevant skill/model changes or an explicit scheduled job.
   Keep model-call budgets and real GitHub fixture resources explicit in the harness configuration.
   Compare candidate and baseline versions on the same task corpus and record repetitions and variance.
   Existing comprehension evals remain useful as a fast supplemental check.

**Metrics:**

- Task success: requests satisfying independent acceptance checks divided by attempted requests.
- False-green rate: runs marked done that fail independent acceptance checks divided by runs marked done.
- Intervention rate: runs requiring operator action after handoff divided by handed-off runs.
- Recovery success: fault-injected runs completing correctly after recovery divided by fault-injected runs.
- Resource use: elapsed time, queue time, model time, verification time, and available usage categories.

**Acceptance:**

- [x] All eight reproduced cases are included in fast regression coverage.
- [x] Each representative task has an independent acceptance oracle and retained outputs.
- [x] The real-host campaign includes at least one successful uninterrupted headless run and one successful interruption/recovery run. Waived by the operator on 2026-09-16: the real-host campaign was not run; see `improve-log.md`.
- [x] All deliberately invalid completion cases are rejected; zero false greens are allowed in the release acceptance corpus.
- [x] Report numerator, denominator, and repetitions for every rate rather than presenting a small sample as established reliability.
- [x] Supported macOS/Linux and Python combinations pass the runner checks.
- [x] A release can link its benchmark report to the exact runner and skill versions tested.

**Verify:** Run the fast corpus in CI and retain a real-host baseline/candidate comparison report.

## 8. Phase 4: improve sustained throughput and feedback

### W12: budget nested work and measure actual usage

**Finding covered:** evaluation item 12, including unbounded fan-out, shared test contention, incomplete token categories, missing scope usage, and after-attempt ceilings.

**Read:** `factory/runner/slots.py`, `hosts.py`, `model.py`, `watch.py`, `dashboard.py`, and `factory/skills/build/references/parallel.md`.

**Research:** Inspect real host usage events before assigning accounting semantics.
Determine whether child-agent usage is included in parent totals and whether cached, cache-write, output, and reasoning counts overlap.
Identify host-native agent limits and the resources shared by integration tests, browsers, services, and ports.

**Delivery slices:**

1. Preserve input, cached input, cache-write input, output, and available child usage without double counting.
   Store unavailable measurements as unknown, including scope usage when the host cannot expose it.
   Estimate cost only from versioned provider pricing and documented billing semantics.
2. Add queue, host-execution, validation, gate, and total-attempt duration fields.
   Display cached usage separately from estimated cost and retain historical compatibility for raw totals.
   Show resource waits and effective ceilings in CLI/watch/dashboard summaries.
3. Bound child-agent fan-out through enforceable host capabilities or runner-mediated dispatch.
   A prompt instruction alone does not count as an enforced cap.
   Add heavy-test and service-resource leases using W04 ownership and cleanup behavior.
4. Run disjoint local checks concurrently, then serialize shared integration validation after the wave's writes settle.
   Allocate isolated ports and service data for independent worktrees.
   Prevent each child from simultaneously running the same broad mutable-worktree validation suite.
5. Detect repeated typed failures with unchanged artifact fingerprints and bound no-progress retries.
   Enforce budgets during execution when the host exposes incremental usage; otherwise enforce at the next observable boundary and label that limitation.

**Acceptance:**

- [x] Usage fixtures with parent/child and cache categories produce correct non-duplicated totals.
- [x] The recorded 32.5M-token example is displayed with its cached input category rather than priced as wholly uncached input.
- [x] Four concurrent stages obey the configured nested-agent and heavy-test limits.
- [x] Resource leases survive worker crashes while children remain active and are released after termination.
- [x] Concurrent worktrees do not collide on fixture ports or service data.
- [x] Missing usage remains unknown and no-progress stops have specific evidence-backed reasons.

**Verify:** Add accounting fixtures, cross-process contention tests, and a bounded multi-run benchmark using the W10 corpus.

### W13: checkpoint expensive subphases and retry only invalidated work

**Finding covered:** evaluation item 13, including model-driven repetition of publication, review, and CI waiting.

**Read:** `factory/runner/worker.py`, `pipeline.py`, ship gauntlet/remediation/PR references, and the W03/W05 receipt producers.

**Research:** Trace one successful ship and one blocked ship into concrete subphases and their inputs.
Identify which operations need model judgment and which are deterministic commands or waits.

**Delivery slices:**

1. Add fixed checkpoints for gauntlet, E2E refresh, review/verification, remediation verification, evidence publication, PR publication, and CI observation.
   Persist input fingerprints, outputs, revision, and a completion receipt.
   Define an invalidation table before implementing reuse.
2. Move deterministic publication and CI polling retries into runner-owned execution where practical.
   Use stable operation identity, bounded backoff, attempt deadlines, and current remote state.
   Recognize already-published evidence and an already-created PR after interrupted responses.
3. Resume only checkpoints whose inputs or outputs are missing or invalid.
   Code changes invalidate dependent tests and reviews; body-only edits invalidate publication checks; model/skill/tool changes invalidate the subphases whose semantics changed.
   Preserve the existing shared paid retry budget for new model attempts and record deterministic operation retries separately.

**Acceptance:**

- [x] A transient PR-publication failure reuses valid prior analysis and retries publication without another model invocation.
- [x] A crash after remote PR creation does not create a duplicate PR.
- [x] Changing code invalidates verification, while unchanged reviewed code retains valid receipts.
- [x] Changing required tool/configuration inputs invalidates affected checkpoints.
- [x] Corrupt or missing checkpoint output triggers recomputation rather than silent reuse.
- [x] The CLI identifies the current subphase, reuse decision, and remaining retry budget.

**Verify:** Add checkpoint invalidation cases and full CLI fault injection around publication and CI polling.

### W14: export run evidence and learn from post-PR outcomes

**Finding covered:** evaluation item 14, including local-only artifacts, stale real-run documentation, absent rework metrics, and elapsed scope time being mistaken for active operator effort.

**Read:** `factory/runner/cli.py`, `model.py`, `dashboard.py`, `events.py`, and `factory/README.md`.

**Research:** Choose a durable artifact destination accessible to intended PR reviewers and compatible with the repository's access model.
Determine which merge/review outcomes can be read through `gh` and which require explicit operator annotation.
Define retention before implementing artifact cleanup.

**Delivery slices:**

1. Add a documented export command producing a manifest and portable bundle of approved intent, decision deltas, runtime provenance, verification receipts, selected raw evidence, and final outcome.
   Use relative references and checksums so the bundle remains usable after moving it off the original machine.
   Preserve the bundle through `factory gc` and provide a durable link from the PR.
2. Add a read-only outcome refresh command using GitHub state plus structured operator annotations for reviewer rework, rejection, regression, and intervention causes.
   Preserve the original done-at-revision record alongside later observations.
   Distinguish merged, closed-unmerged, still-open, and unknown states.
3. Summarize success, post-handoff intervention, rework, regressions, resource use, and repeated failure categories across runs.
   Track active operator time only through explicit measured interaction intervals or annotations.
   Keep unknown active time distinct from elapsed interactive scope duration.
4. Generate factual run summaries from runtime data and reconcile the README's outdated parked-run narrative with the recorded successful retry.
   Label historical local observations separately from newly reverified results.
   For recurring failures and reviewer corrections, add an eval case or repository-contract improvement and link it to the originating run.

**Acceptance:**

- [x] An exported run is inspectable from another directory or machine without the original worktree.
- [x] Every exported artifact resolves and matches its recorded checksum after archive/GC.
- [x] The PR links the retained evidence bundle without committing `.dev/` into the target repository.
- [x] Post-PR updates do not erase the revision and evidence that originally satisfied completion.
- [x] At least one observed failure or reviewer correction is linked to a new regression/eval case.
- [x] Summary rates expose their sample sizes, and active operator time is not inferred from idle scope wall time.
- [x] The real-run documentation reflects the recorded successful retry and the provenance of that observation.

**Verify:** Add export/GC/relocation tests, GitHub outcome fixtures, and one complete retained-run walkthrough.

## 9. Phase-level release gates

| Gate | Required evidence |
| --- | --- |
| Foundation gate | R2/R4/R6/R8 regressions, bounded parsing/execution, crash-safe ownership, correct outcome combination |
| Trustworthy-completion gate | All R1-R8 regressions, approved-intent snapshots, executing scenario evidence, complete review/gauntlet, exact PR revision and published evidence |
| Behavioral-release gate | Versioned independent acceptance corpus, platform CI, real-host normal and recovery runs, baseline/candidate report with sample sizes |
| Sustained-operation gate | Nested resource limits, truthful usage accounting, checkpoint reuse/invalidation, portable exports, post-PR outcomes and feedback case |

Each gate also requires the existing successful workflow, operator controls, generated distributions, and conservative cleanup to continue working.
Complete the applicable gate before calling the associated milestone finished.

## 10. Compatibility, migration, and rollout

The work changes persistent state and artifacts, so compatibility is part of implementation rather than a final cleanup task.

- [x] Introduce explicit versions for new records and bump run schema only when persisted run semantics require it.
- [x] Validate nested record types and supported versions on load with clear errors.
- [x] Keep old runs inspectable even when their historical evidence cannot satisfy the stronger completion predicate.
- [x] Never reinterpret an old report as newly executed evidence or invent missing scenario identities, runtime versions, or usage categories.
- [x] Before replacing persisted state, preserve the original and write through the existing atomic-save pattern.
- [x] Migrate only when the run has no active worker or executor, or perform the change at an explicitly supported worker-owned boundary.
- [x] For resumable old runs, require regeneration of unverifiable receipts and record why reuse was refused.
- [x] Document how to resume or inspect with the previous runtime when a stopped run cannot yet migrate.
- [x] Ensure old/new schema mixtures, interrupted migrations, and newer unsupported versions fail clearly without losing artifacts.
- [x] Keep plugin generation and runner/skill compatibility checks synchronized with protocol changes.
- [x] Retain successful legacy provenance as historical evidence and label newly validated runs separately in metrics.

Use copied fixture runs to test migration and rollback before applying the procedure to an operator's retained run.
Do not reset an operator's configuration or delete unmerged work as a migration strategy.

## 11. Verification commands and evidence to retain

Unless a command says otherwise, run it from the repository root.
Commands for newly proposed features must be added to their work package once those interfaces exist.

### Focused runner checks

Run the relevant existing test modules while developing a slice:

```bash
(cd factory && python3 -m unittest runner.tests.test_gates runner.tests.test_worker runner.tests.test_control)
```

For a supervisor, state, or worktree slice, select the relevant modules:

```bash
(cd factory && python3 -m unittest runner.tests.test_hosts runner.tests.test_model runner.tests.test_slots runner.tests.test_worktree)
```

### Whole runner suite and CLI integration

```bash
(cd factory && python3 -m unittest discover -s runner/tests -t .)
bash scripts/test_factory_runner.sh
```

### Generated distribution

After changing shipped factory skills, references, scripts, or metadata:

```bash
python3 scripts/build_codex_plugin.py --plugin factory
python3 scripts/build_codex_plugin.py --check
```

### Required repository gate

```bash
bash scripts/validate.sh
```

The repository gate already includes the runner suite and offline CLI E2E checks.
Use focused tests during development and the full gate before completing a change.
Avoid rerunning unchanged successful checks without a new failure, change, or unresolved concern.
When the full gate fails, rerun the named focused command with visible output and fix the cause.

### Evidence retained per work package

- Original reproduction and its failing assertion.
- Research decision and chosen contract.
- Relevant test names and passing result.
- Successful fixture-run ID and artifact manifest when applicable.
- Before/after behavior visible through the CLI or artifact consumer.
- Migration behavior for affected legacy artifacts.
- Runtime versions for any actual host or platform smoke test.
- Updated documentation and generated-package parity result.

## 12. Final completion checklist

- [x] W01: data-only parsing rejects executable artifacts without side effects.
- [x] W02: every required scenario has current execution-backed evidence.
- [x] W03: completion identifies the exact published PR revision and its verified evidence/checks.
- [x] W04: interruption and recovery preserve one executor and correct transitions.
- [x] W05: incomplete review or gauntlet execution cannot yield ready status.
- [x] W06: unresolved typed park conditions participate in completion.
- [x] W07: committed and uncommitted stage boundary violations are detected.
- [x] W08: validation and waits honor cancellation, execution boundaries, and one deadline.
- [x] W09: approved intent and attempt handoffs are retained and integrity-checked.
- [x] W10: independent behavioral evals and supported-platform checks produce release evidence. Waived by the operator on 2026-09-16: the real-host campaign was not run; see `improve-log.md`.
- [x] W11: repository setup and runtime provenance are reproducible and inspectable.
- [x] W12: nested work is bounded and resource reporting preserves actual usage semantics.
- [x] W13: subphase retries reuse only valid checkpoints and preserve retry accounting.
- [x] W14: durable exports and post-PR outcomes feed new evals and repository improvements.
- [x] All R1-R8 negative cases are rejected correctly while valid fixture changes still complete.
- [x] Existing runs remain inspectable and migration behavior is documented and tested.
- [x] `scripts/validate.sh` and generated-plugin parity pass.
- [x] The operator guide explains the new contracts, controls, failure reasons, and exact next actions.

The final handoff should include the completed checklist, links to regression tests, a representative real-run evidence bundle, and a benchmark comparison against the starting implementation.
