# Software factory evaluation and improvement backlog

Date: 2026-09-16

## Assessment

The project has a credible local software-factory implementation with a strong orchestration foundation.
The next milestone should be trustworthy completion: a green run must mean the requested behavior was demonstrated on the exact code in the pull request, and recovery must preserve one active executor.
Those guarantees currently have reproducible gaps.

Keep the fixed four-stage pipeline, fresh processes, worktree isolation, file-based handoffs, and small Python control plane.
Concentrate the next iteration on stronger executable contracts, recovery, and behavioral evaluation.

### What is already working well

- **Clear workflow:** interactive scope followed by unattended scope review, build, and ship.
- **Useful operational controls:** attached and detached execution, inspection, logs, pause, notes, cancellation, retry, and resume.
- **Durable local state:** atomic writes with file and directory fsync, worker locks, attempt directories, and an event history.
- **Bounded top-level execution:** cross-process stage slots, stage timeouts, and a shared retry budget.
- **Good context boundaries:** fresh headless processes and short handoff prompts referencing artifacts on disk.
- **Substantial verification scaffolding:** spec linting, test references, repository validation commands, evidence checks, and independent review-panel design.
- **Conservative cleanup:** automatic collection requires a completed run and a merged PR, and preserves the plan in the archive.
- **Working distribution tooling:** generated Codex packages, parity validation, and intentional separation between the factory and hand-invoked development workflow.

### Evidence reviewed

Reviewed the current working tree, including the uncommitted factory implementation, runner tests, skill definitions, shared references, generated-package workflow, implementation plan, and local run records.

- `bash scripts/validate.sh` passed, including the runner suite and the offline CLI E2E scenarios.
- The runner suite contains **187 discovered tests**.
- Ran **eight additional diagnostic probes**, using temporary local repositories and stub hosts for execution tests.
- Local runtime state records one real run reaching `done`, with ready PR #44, after one authentication-related ship retry.
- That run records **32,536,837 total tokens** across headless attempts.
  Its streams report **31,770,198 cached input tokens out of 32,408,004 input tokens**, approximately 98%, plus 128,833 output tokens.
  The raw total should not be interpreted as an uncached-token bill.
- The README's real-run notes stop at the earlier parked attempt, so they understate the observed completion progress.

The existing offline tests establish useful runner behavior.
The additional probes exercise failure cases those tests currently accept.
The real-run outcome above is taken from local records; this review did not independently revalidate its feature behavior or GitHub state.

## Reproduced gaps

| Probe | Observed result | Why it matters |
| --- | --- | --- |
| Full `factory new` with test names only in comments and an E2E report containing no scenario records | All stages completed and the CLI exited 0 | Passing currently does not establish test execution or E2E scenario coverage. |
| Full run with a non-retryable build result reporting missing credentials | Run completed, with only a warning about the conflicting result | The fixed park policy can be overridden by a passing gate. |
| Full run where scope review committed an unauthorized source file | Scope review and the entire run completed | Stage path checks inspect outstanding changes, so committing a forbidden edit hides it. |
| Load an E2E data block containing a JavaScript filesystem expression | The parser created a temporary marker file and returned successfully | Parsing a report executes code in the runner's host environment. |
| Aggregate a review with all expected lens results missing | Aggregate verdict was `PASS`, with an empty panel and populated `failedLenses` | Review availability and review verdict are conflated. |
| Build gate invoked with cancellation already requested and an expired deadline | It ran validation and passed | The attempt's control and timeout guarantees do not cover the whole gate. |
| Commit and push code after a passing review, then evaluate ship | Ship passed using the old review | Evidence and approval are not tied to the final source revision. |
| Kill a worker while its host process survives, then run `factory resume --detach` | Both the old and replacement host processes were alive | The worker lock does not prevent two agents editing the same worktree. |

The probe program is retained for this session at `/private/var/folders/bw/_rsst1cd2gsfhn152x20z4mc0000gn/T/opencode/factory-evaluation-probes.py`.
Its temporary repositories were cleaned up after execution.

## Prioritized improvements

P0 addresses false completion, executable artifact parsing, and conflicting executors.
P1 establishes repeatability and evidence sufficient to expand unattended use.
P2 improves sustained throughput and learning from completed work.

### 1. P0: Parse artifacts as data only

**Finding:** `load_e2e_data()` evaluates an agent-authored JavaScript object using Node's `eval()`.
The tuple of parser candidates eagerly calls `node_json()` even when the first candidate is valid JSON.
The Node subprocess also has no timeout.
This is an executable handoff into the host-side gate.

**Improve:** Write a strict JSON sidecar for E2E results and render HTML from it.
Validate its schema before use, and reject malformed data without evaluating expressions.
Apply execution limits to any required compatibility converter.

**Acceptance:** The marker-file probe is rejected without side effects, and invalid or oversized reports produce bounded, typed gate failures.

**Source:** `factory/skills/ship/scripts/pr-evidence.py:63-76,135-146`; `factory/runner/gates.py:380-392`.

### 2. P0: Require execution evidence for every acceptance scenario

**Finding:** `check-tests.py` checks reference counts and whether a name appears anywhere in a file.
Comments, duplicate references, or inert test bodies can satisfy that check.
The E2E gate trusts summary counters without reconciling the actual scenarios.
The supplied happy-path fixture itself has empty E2E scenarios and tests whose bodies are `pass`.
That is sufficient for an orchestration fixture, but exposes how little independent behavioral proof the gate requires.

**Improve:** Give acceptance scenarios stable IDs and map each to a collected test ID, declared layer, execution result, and raw evidence.
Have runner-controlled test commands produce machine-readable results.
Derive counts from unique scenario records; reject missing, duplicated, skipped, failed, or mismatched required scenarios.
For bug fixes, retain red-on-base and green-on-change reproduction evidence.

**Acceptance:** Comment-only references and invented summary totals fail, and a real small application passes only after its required scenarios execute successfully.

**Source:** `factory/skills/build/scripts/check-tests.py:72-85,118-129`; `factory/runner/gates.py:412-427`; `factory/runner/tests/helpers.py:48-51`.

### 3. P0: Bind final verification to the exact PR revision

**Finding:** Ship can mutate code after build, but its runner gate does not re-establish behavioral correctness on that final code.
It accepts the highest-numbered review without checking the reviewed SHA.
It checks local `pr.md`, rather than the published PR body.
The GitHub query does not establish the intended base or exact head SHA, and `unpushed()` only counts local commits absent remotely.
Zero ahead commits does not establish equality when the remote is ahead.
An empty checks list currently passes without determining whether expected checks have appeared.

**Improve:** Attach source SHA, spec hash, relevant configuration hashes, and artifact hashes to verification records.
Invalidate evidence when those inputs change.
Before completion, verify the actual PR repository, base, head SHA, published evidence, expected checks, and final clean source tree.
Rerun affected deterministic checks after ship's fixes and require fresh review coverage of those changes.

**Acceptance:** A post-review commit invalidates completion; a stale report, wrong PR base, different remote head, or unpublished evidence cannot satisfy ship.
An intentional no-CI repository remains supportable through an explicit repository contract.

**Source:** `factory/runner/gates.py:459-477,509-577`; `factory/runner/worktree.py:223-229`; `factory/skills/ship/SKILL.md:30,55-60,121-134`.

### 4. P0: Make recovery preserve one active executor

**Finding:** Resume explicitly warns about a surviving orphaned host and then starts another attempt anyway.
Worker locks and slots disappear with the worker, while its independently started host process can continue.
There is also a completion crash window: `finish()` saves the finished attempt before applying and saving its transition.
If the worker dies there, `reconcile_orphan()` sees no open attempt and requeues the stage instead of completing the recorded decision.
The concurrent-host case was reproduced; the completion-window case is identified from code inspection.

**Improve:** Track executor identity with PID, process-start identity, attempt ID, and an ownership token.
Reconcile or stop a positively identified surviving executor before launching its replacement; ambiguous ownership should prevent replacement execution.
Persist the finished attempt and its next transition as one recoverable operation.
Make stage side effects idempotent against the recorded attempt and revision.

**Acceptance:** Crash-injection tests at spawn, commit, gate completion, transition, and PR publication preserve one executor and do not repeat completed side effects or lose retry accounting.

**Source:** `factory/runner/control.py:191-233`; `factory/runner/supervise.py:168-205`; `factory/runner/worker.py:270-295,339-355`.

### 5. P0: Distinguish a complete review from a favorable review

**Finding:** The aggregator returns `PASS` when every expected lens is missing.
A BLOCK finding without a verifier result becomes a CONCERN, which ship accepts.
The runner checks a Markdown verdict, but does not require the review's underlying panel results or gauntlet records.
Execution failures can therefore improve the apparent verdict.

**Improve:** Give review artifacts separate completeness and verdict fields.
Require the selected mandatory lenses to finish and BLOCK findings to receive a valid verification result.
Treat unavailable review evidence as an incomplete stage eligible for targeted retry.
Require machine-readable gauntlet results with checked scope, tool version, thresholds, revision, and outcome.

**Acceptance:** Missing all reviewers, a missing required verifier, and absent mandatory gauntlet evidence cannot produce a ready run.
Retrying a missing lens does not rerun successful lenses whose inputs are unchanged.

**Source:** `factory/skills/ship/scripts/aggregate-findings.py:110-163`; `factory/skills/ship/SKILL.md:104-109`; `factory/runner/gates.py:527-542`.

### 6. P1: Enforce the declared park policy as part of completion

**Finding:** A passing gate wins over any blocked result, including non-retryable outcomes carrying a park-list reason.
Several park conditions cannot be inferred from the current file and Git checks, so treating the result as entirely advisory loses necessary information.

**Improve:** Define strict typed blocking conditions and include their resolution in the authoritative completion contract.
Preserve gate authority over unsupported success claims, while requiring a recorded park condition to be resolved before success.
Use stable failure codes and remediation categories instead of relying on natural-language reason strings.

**Acceptance:** A recognized unresolved environment or premise block cannot disappear into a warning merely because expected artifacts exist.

**Source:** `factory/references/factory-run.md:53-62`; `factory/runner/gates.py:209-237,599-619`.

### 7. P1: Enforce stage boundaries across committed and uncommitted changes

**Finding:** Scope and scope-review path enforcement uses `git status`.
Unauthorized edits disappear from that check when the agent commits them.
Other important constraints, including staying on the run branch and keeping plans out of Git, are primarily prompt instructions.

**Improve:** Capture each stage's starting SHA, branch, index, and worktree state.
Validate the complete stage delta, including commits, staged changes, unstaged changes, and relevant untracked files.
Require the expected branch and reject tracked runtime artifacts.
Make runner-owned metadata writable only through the runner where the host supports that separation.

**Acceptance:** Scope review committing a source file fails with the offending path, and switching branch or force-adding `.dev/` cannot bypass the stage contract.

**Source:** `factory/runner/gates.py:273-282,321-322,369-371`; `factory/runner/worker.py:247-256`; `factory/runner/hosts.py:27-35`.

### 8. P1: Supervise validation as part of the attempt

**Finding:** Build validation ignores `GateContext.deadline` and `cancel_requested`.
Each command instead gets an independent 20-minute allowance after the model process finishes.
The Markdown parser also executes fenced lines as separate commands, so stateful blocks such as `cd`, exports, or multiline shell constructs are not faithfully preserved.
These commands run directly on the host rather than inside the agent's configured sandbox.

**Improve:** Use one process-supervision abstraction for host execution, validation, and cancellable waits, with a shared remaining deadline.
Represent validation as an explicit argv or a repository script with a working directory, environment, and execution boundary.
Keep Markdown as the human-readable rendering of that contract.

**Acceptance:** Cancellation terminates validation and its descendants promptly, an exhausted deadline starts no new checks, and a supported multiline validation script runs with its intended shell state.

**Source:** `factory/runner/gates.py:147-206,430-439`; `factory/runner/worker.py:219-245`.

### 9. P1: Version the approved intent and every stage handoff

**Finding:** The spec is mutable and excluded from Git.
Scope review can alter decisions and scenarios, and later attempts read the current files.
Attempt directories preserve prompts and gate results, but do not snapshot the exact spec, notes, and evidence each attempt received.
This makes it difficult to distinguish legitimate refinement from acceptance criteria drifting toward the implementation.

**Improve:** Snapshot the approved request, observable acceptance criteria, and non-goals at handoff.
Separate those invariants from technical decisions the factory may autonomously refine.
Record structured decision deltas and content-addressed input/output manifests per attempt.
Reuse an artifact only when its relevant inputs still match.

**Acceptance:** Every attempt can be explained from its retained input snapshot, and removing or weakening an approved acceptance scenario is detected automatically.
Technical refinements within the recorded intent continue unattended.

**Source:** `factory/references/factory-run.md:34-38,42-62`; `factory/skills/scope-review/SKILL.md:54-81`; `factory/runner/pipeline.py:92-123`; `factory/runner/worker.py:161-177,270-278`.

### 10. P1: Add behavioral factory evals and release evidence

**Finding:** All four factory skill eval definitions are comprehension questions about what the agent should do.
The runner's E2E tests use deterministic stub hosts.
Neither establishes that real models consistently implement, verify, recover, and ship the requested behavior.
The single recorded real success required an environment repair and is insufficient to characterize reliability.

**Improve:** Add a small versioned benchmark of representative fixture repositories and tasks: bug fix, feature, UI change, dependency issue, ambiguous requirement, and recoverable interruption.
Evaluate real execution against independent acceptance tests and retained artifacts.
Include adversarial negative cases from this review in fast offline CI, and run paid model evals on relevant skill/model changes or a scheduled cadence.
Run runner compatibility checks on macOS and Linux across the supported Python range.

**Acceptance:** Releases report task success, false-green rate, post-handoff interventions, recovery success, and resource use against a fixed benchmark.
The eight reproduced cases have regression coverage.

**Source:** `factory/evals/README.md:5-17`; `scripts/test_factory_runner.sh`; `factory/runner/tests/helpers.py:17-71`; `.github/workflows/validate.yml:9-27`.

### 11. P1: Make repository onboarding and runtime provenance reproducible

**Finding:** Preflight verifies binaries, host GitHub authentication, plugin-name visibility, and repository basics.
It does not establish that the actual headless environment can run this repository's checks or that its installed Codex skills match the runner checkout.
Models are hard-coded, configuration is reloaded, and attempts do not retain a complete effective runtime manifest.
The existing GitHub token-forwarding repair is a useful example of a real environment mismatch already addressed.

**Improve:** Add a small repository execution contract for setup, validation, app launch, test services, ports, and expected CI checks.
Record the runner revision, resolved skill bundle hash, host versions, model/effort, and effective configuration per attempt.
Extend doctor with repository-specific checks through the intended execution environment and an explicit, cached host-capability smoke test.

**Acceptance:** A missing launch prerequisite or incompatible skill bundle is detected before a long build, and an old run can identify the exact runtime and inputs it used.

**Source:** `factory/runner/cli.py:128-159,703-803`; `factory/runner/pipeline.py:18,37-48,81-89`; `factory/runner/worker.py:64-68`; `factory/README.md:245-271`.

### 12. P2: Budget nested work and measure actual resource use

**Finding:** The semaphore limits top-level stages, while each stage may launch many agents and test processes.
Parallel change-set agents each run the wider validation suite against a shared mutable worktree.
Token accounting discards cache categories, checks the ceiling after an attempt, and omits scope usage.
The recorded first ship attempt consumed about 64% of the run's raw token total, making it the first phase worth profiling.

**Improve:** Track input, cached input, cache-write, output, and available child-agent usage separately.
Estimate cost only with explicit provider pricing semantics.
Add per-run child-agent and heavy-test limits, shared resource leases, and queue-wait metrics.
Serialize shared integration checks after a build wave, while parallel agents run genuinely isolated local checks.
Detect repeated failures and lack of progress using typed failures and artifact changes.

**Acceptance:** Four active stages cannot create unbounded test contention, and the operator can distinguish queue time, model time, validation time, cached usage, and estimated cost.
Resource ceilings describe their enforcement granularity honestly when host usage arrives only at turn completion.

**Source:** `factory/runner/slots.py:37-68`; `factory/runner/hosts.py:83-135`; `factory/runner/model.py:368-397`; `factory/skills/build/references/parallel.md:20-33,51-58`.

### 13. P2: Checkpoint expensive subphases and retry only invalidated work

**Finding:** The runner knows four stages, while ship contains eight checks, fix loops, E2E refresh, review, verification, remediation, publication, and CI waiting.
Reuse within a stage is largely instructed through prose and inferred from artifacts.
The short second ship attempt suggests reuse is already useful, but it is not mechanically established.

**Improve:** Add a small set of explicit subphase checkpoints with input fingerprints and completed outputs.
Distinguish fixing code, retrying an unavailable reviewer, publishing an existing PR body, and waiting for CI.
Use deterministic retries and backoff for transient publication/polling failures when no model judgment is needed.
Keep these checkpoints within the fixed pipeline.

**Acceptance:** A PR-publication failure after a green review resumes publication without repeating analysis, while a code change invalidates the relevant verification checkpoints.

**Source:** `factory/runner/pipeline.py:81-89`; `factory/runner/worker.py:161-245`; `factory/skills/ship/SKILL.md:41-134`.

### 14. P2: Close the loop from run completion to engineering outcomes

**Finding:** The product currently ends at a ready PR, and its telemetry primarily describes attempts, elapsed time, and raw tokens.
Merge checks are used for cleanup, but there is no factory-level view of reviewer rework, rejected changes, regressions, or repeated setup failures.
Important plans and evidence are local, and the README's stale real-run narrative illustrates the maintenance gap.

**Improve:** Add a run export containing retained intent, decisions, evidence manifests, execution provenance, and outcome summary.
Link that durable bundle from the PR without committing `.dev/` into target repositories.
Record post-PR outcomes and turn recurring failures or reviewer corrections into new eval cases and repository-contract improvements.
Generate factual run summaries from runtime data and measure active operator time separately from elapsed scope time.

**Acceptance:** An operator can answer which requests shipped successfully, how much intervention and rework they needed, why failures repeated, and which eval now guards each discovered regression.

**Source:** `factory/runner/model.py:158-190`; `factory/runner/cli.py:591-653`; `factory/runner/dashboard.py:82-118`; `factory/README.md:245-271`.

## Recommended implementation sequence

1. **Close the false-green and recovery paths:** items 1-8, with negative regression cases added as each contract is fixed.
2. **Establish reproducible execution:** items 9-11, with an initial representative real-host benchmark and retained run manifests.
3. **Improve sustained operation:** items 12-14, guided by measured bottlenecks and post-PR outcomes.

The next release criterion should be explicit: the reproduced negative cases fail correctly, representative valid changes still reach ready PRs, and a killed worker cannot leave two executors on one worktree.
That strengthens the project's central promise while preserving the simple architecture already in place.
