# Factory Run Protocol

How a factory skill talks to the factory runner.
The runner launches each stage as a fresh process, chains stages through artifacts on disk, and decides completion with a deterministic gate.
This reference owns both JSON contracts; every factory skill loads it when `.dev/factory-run.json` exists.

## The run file

The runner writes `.dev/factory-run.json` in the worktree before every attempt.
Read it first, before any branch, commit, or `/tmp` heuristic.

```json
{
  "schema": 2,
  "run_id": "20260915-1420-webhook-idempotency",
  "run_dir": "/home/user/.factory/runs/20260915-1420-webhook-idempotency",
  "plan": "webhook-idempotency",
  "stage": "build",
  "attempt": 2,
  "previous": {"outcome": "blocked", "reason": "Validation command unit (pytest -q) exited 1"},
  "operator_note": "The flaky test was fixed upstream",
  "conditions": [{"id": "C1", "code": "environment.missing_credentials", "summary": "STRIPE_KEY is not available",
                  "evidence": ["sandbox returned 401"], "requires_env": ["STRIPE_KEY"], "raised": "build attempt 1"}],
  "intent_file": "/home/user/.factory/runs/.../intent/approved.json",
  "scenarios_file": "/home/user/.factory/runs/.../intent/scenarios.json",
  "request_file": "/home/user/.factory/runs/.../request.md",
  "source": {"kind": "jira", "key": "PROJ-42", "url": "https://acme.atlassian.net/browse/PROJ-42", "summary": "..."},
  "report_dir": "/home/user/.factory/runs/.../reports",
  "evidence_dir": "/home/user/.factory/runs/.../evidence",
  "scratch_dir": "/home/user/.factory/runs/.../scratch/build-2",
  "branch": "factory/webhook-idempotency",
  "base": "main",
  "base_sha": "abc123",
  "interactive": false
}
```

- `plan` is the plan directory name: `.dev/{plan}/`. Never pick or rename it.
- `report_dir` replaces `/tmp/{project-slug}/reports/`, `evidence_dir` replaces `/tmp/{project-slug}/pr-evidence/`, and `scratch_dir` is the scratch root for agent batches.
- `previous` and `operator_note` are present on retries: start from the artifacts on disk, fix what the reason names, and do not redo finished work. The operator writes the note from `factory note`, `factory retry --note`, or the attached view; treat it as guidance on top of the stage's contract.
- `branch` is already checked out with its upstream configured. Never switch branches, rebase, amend pushed commits, or force-push: the runner compares every attempt's complete Git delta, including commits, against where it started, and a branch switch or rewritten history parks the run.
- Plan files are never committed: the runner excludes `.dev/` from Git, so the spec, reviews, notes, `pr.md`, and result files stay on disk only. Commit code, tests, and `docs/` changes; never force-add anything under `.dev/`, and never edit `.factory/contract.json` after scope.
- Stage paths are enforced on that delta: `scope` may change `docs/` and `.factory/contract.json`, `scope-review` only `docs/decisions.md` and `docs/contracts.md`, and `build` and `ship` anything except `.factory/contract.json`.
- Write only in the worktree, `report_dir`, `evidence_dir`, and `scratch_dir`: the run directory holds runner-owned records, and the common Git directory's config, hooks, and excludes are outside the sandbox grant.
- `previous` may be `null` and `operator_note` may be `null`.
- `conditions` lists the park conditions still open for this stage; see "Park conditions" below.
- `intent_file` is the sealed approved intent: the request, non-goals, and scenarios with their ids (`S1`, ...). `scenarios_file` also lists scenarios added after the handoff. Both are read-only; approved scenario text and `⊘` non-goals in `spec.md` must not change.
- `request_file` is the full change request as Markdown; `source.kind` is `text`, `file` (with `path`), or `jira` (with `key`, `url`, `summary`).

## Decisions without a human

When `interactive` is false, no human is available and nothing reads stdin.
Every decision you would put to the user is yours to make: take the recommended option and keep going.

- The recommendation is the option the repo evidence, the settled spec, and `docs/decisions.md` support best.
  When options are genuinely tied, pick the smallest, most reversible one that stays closest to the existing code.
- Record every auto-decision where the stage keeps its audit trail: `Answered: factory policy (auto-decided) - {choice}` in `spec-review_N.md`, an `auto-decided:` Deviations line in `implementation-notes.md`, and the PR's Auto-decided section.
  Count them in the result's `counts.auto_decided`.
- Any older wording that routes a choice to a person means: decide as above and record it.

Park the run instead, with a `blocked` result carrying a typed condition, only for this fixed list:

| # | Situation | `code` | Retryable |
| --- | --- | --- | --- |
| 1 | The change's premise is invalidated | `premise.invalidated` | no |
| 1 | The work opens a genuinely new effort with its own decision tree | `scope.new_effort` | no |
| 2 | A found secret | `secret.found` | no |
| 2 | Anything needing credentials, authentication, or account access the run does not already have | `environment.missing_credentials` | no |
| 3 | A destructive or irreversible action outside the run branch: rewriting history, force-pushing, deleting data or remote resources | `action.destructive` | no |
| 4 | No safe command to launch the application for e2e after documented discovery | `launch.unavailable` | no |
| 5 | A recommended decision you applied that still fails its own verification this attempt | `decision.verification_failed` | yes |

`input.unusable` covers a handed-off input the stage cannot work from at all (no spec, a spec that fails lint before review, no reviewable diff, no `gh`); it is not a decision but it is not retryable either.

Failures are not decisions: a red test, an unfixed blocker, or an exhausted fix loop keeps the stage's own typed result.
Never wait, never loop asking, and never weaken a check, threshold, test, or contract to make a decision go away.

## The result file

Write `.dev/{plan}/{stage}-result.json` as your very last action, after every commit and push the stage owns.
It is runtime protocol; like everything under `.dev/`, the runner excludes it from Git.
The runner reads it strictly: schema 2, known keys only, and a malformed file fails the attempt.

```json
{
  "schema": 2,
  "stage": "build",
  "status": "done | blocked | failed",
  "reason": "one sentence a human can act on",
  "artifacts": [".dev/webhook-idempotency/implementation-notes.md"],
  "next": "ship",
  "counts": {"change_sets": 4, "scenarios": 17, "auto_decided": 2},
  "conditions": []
}
```

- `next` names the stage the runner should start after this one, or `null` after `ship`. It is informational: never invoke that stage yourself.
- `ship` adds `"pr_url"` and `"draft"` at the top level; surviving review blockers stay in the PR's Open calls, not in `conditions`.
- Report honestly: `done` only when your own checks passed, and never `done` with an unresolved condition.
- Retryability is not yours to set: it follows from the gate and from each condition's `code`.

## Park conditions

Each condition is `{"code", "summary", "evidence": [...]}`, plus `"requires_env": ["NAME"]` when missing credentials are specific environment variables.

```json
{"code": "environment.missing_credentials", "summary": "STRIPE_KEY is not available to the sandbox",
 "evidence": ["POST https://api.stripe.test returned 401"], "requires_env": ["STRIPE_KEY"]}
```

- An unresolved condition blocks the stage even when the runner's gate passes; the runner records it with an id such as `C1`.
- On a later attempt, `factory-run.json` lists the open conditions.
  Re-check each one: when it is fixed, report it with `"resolution": "resolved"`, `"resolves": "C1"`, and evidence; when it still holds, report it again.
  A condition neither resolved nor reported again keeps blocking.
- The runner re-checks what it can observe itself, such as whether `requires_env` names are now set; its re-check overrides a claimed resolution.
- A blocked result without a condition is an ordinary failure: the gate decides it, and it never parks the run on its own.

## The gate is authoritative

The runner re-checks every stage from files, Git, commands, reports, and GitHub state.
For `ship` that means one exact revision: the PR's head, the refreshed remote branch, and local HEAD must be the same commit; the runner re-runs the mapped tests, the e2e driver, the gauntlet commands, and validation on it; the review and gauntlet records must name it; the published Evidence must match `pr.md`; and the contract's required checks must finish green for it. If the PR moves while the runner verifies, verification starts over.
A `done` result that fails the gate is not done, and the gate's reason is what the next attempt receives.
So the result file never replaces the skill's own deterministic loops: run `lint-spec.py`, `check-tests.py`, the Validation block, and `pr-evidence.py check` to clean before claiming `done`.

## Examples

Done:

```json
{"schema": 2, "stage": "scope-review", "status": "done", "reason": "Approved after 2 rounds; 5 refinements applied",
 "artifacts": [".dev/webhook-idempotency/spec-review_1.md"], "next": "build",
 "counts": {"refinements_applied": 5, "auto_decided": 1}, "conditions": []}
```

Blocked on the park list:

```json
{"schema": 2, "stage": "build", "status": "blocked",
 "reason": "No safe launch command: tried `make run` (missing target) and `npm start` (needs prod DATABASE_URL)",
 "artifacts": [".dev/webhook-idempotency/implementation-notes.md"], "next": "ship",
 "counts": {"change_sets": 3, "scenarios": 11},
 "conditions": [{"code": "launch.unavailable", "summary": "No mocked-environment launch command exists for e2e",
                 "evidence": ["make run: No rule to make target 'run'", "npm start: DATABASE_URL must point at production"]}]}
```

Resolving a condition on a retry:

```json
{"schema": 2, "stage": "build", "status": "done", "reason": "All change sets committed; e2e green",
 "next": "ship", "counts": {"change_sets": 3, "scenarios": 11},
 "conditions": [{"code": "launch.unavailable", "summary": "The operator added `make dev-mock`", "resolution": "resolved",
                 "resolves": "C1", "evidence": ["make dev-mock serves http://127.0.0.1:8080/health with 200"]}]}
```
