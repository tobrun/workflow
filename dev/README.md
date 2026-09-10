# dev

Development workflow skills for Claude Code, Codex, opencode, and Pi, built around two ideas: layered tests are the enforceable spec for behavior, and every phase produces something a human actually reviews as HTML, not markdown scrolling.

The skills chain loosely rather than as a rigid pipeline: `/scope` interviews for the real problem, argues every design decision against alternatives, and writes a self-contained spec whose change plan carries layer-tagged test scenarios; `/scope-review` puts the settled spec through a fresh-context, adversarially verified agent panel that checks the plan against the actual repo and refines the spec in place, looping without a human and closing with a short interview for the few findings only the user can decide, so a finished run hands `build` a spec ready to implement; `/build` executes the spec's change sets across unit/integration/e2e, proving every scenario with a test that has been seen to fail, in parallel waves where file lists allow, keeping a running implementation-notes log; `/ship` runs a deterministic quality gauntlet - the repo's own static analysis, security scan, dead code, duplication, dependency rules, coverage-weighted complexity, flakiness, mutation testing - looping fix agents until the checkers pass, then verifies the result with a fan-out review panel that checks spec conformance, e2e coverage, and logged deviations; `/commit` groups pending changes into granular commits with structured what/why messages; `/to-pitch` and `/to-quiz` turn finished work into a buy-in doc or a comprehension check.
The durable context is deliberately small: the code, its tests, the active spec under `.dev/{plan-name}/`, and three repo-tracked registries the skills maintain in the consuming project - `docs/decisions.md` (design decisions with their argued alternatives, read only after a review forms its findings), `docs/contracts.md` (boundary guarantees, read as premises before a review walks the diff), and `docs/dependencies.md` (machine-checkable module dependency rules, enforced by `ship`).
Every producing skill renders its own output as self-contained HTML under `/tmp/{project-slug}/reports/`. It publishes only when the user requests a shareable link and the host provides an artifact-publishing tool.
Every skill is explicit-invocation only: Claude Code and Pi use `disable-model-invocation: true`, the generated Codex distribution uses `agents/openai.yaml` with `allow_implicit_invocation: false`, and opencode enforces it with a `permission.skill` rule set to `ask` (see opencode installation below). Skills recommend the next step rather than launching each other.

## Skills

### scope

Specs a change by interviewing for the real problem behind the request, cataloging every design decision (with a subagent blind-spot pass on full-size changes), and arguing each one against alternatives in a `✓`/`✗`/`?`/`⚠`/`⊘` notation with evidence marks.
Writes a self-contained spec at `.dev/{plan-name}/spec.md` - research decisions, scope with invariants and a Validation block of the repo's real commands, and a change plan of numbered change sets each ending in a layer-tagged `tests:` line - designed as a fresh-context handoff to `build`.
A checker (`scripts/lint-spec.py`) enforces the spec's mechanics - unique slugs, argued alternatives, echoes that match their decision, tagged test scenarios - so the prose stays about judgment.
Promotes durable decisions to `docs/decisions.md` and cross-boundary invariants to `docs/contracts.md`, renders an expandable-card spec view, and has a reverse mode that audits the implicit decisions already embedded in existing code.

### scope-review

Reviews a settled spec with agents that did not write it and refines it in place, before build starts - the cheapest review in the chain, since a defect caught here costs a spec edit instead of a re-implementation, and the loop makes those edits itself.
Gates on `scope`'s own `lint-spec.py` first (a mechanically unsettled spec is sent back, not reviewed), then loops up to two rounds of panel, verification, and refinement: four lenses in parallel - feasibility (does the plan survive contact with the repo: named files, assumed hooks, claimed prior art, stated premises about current behavior), completeness (failure paths, second-order work, and affected call sites the spec never mentions), consistency (change sets versus their linked decisions and the project's ledgers, semantically), and testability (will the `tests:` lines produce real proof at their tagged layers) - with every BLOCK and CONCERN adversarially verified on ship's transport and aggregation machinery before a refine agent may touch the spec.
Refinements follow a strict authority order (recorded user intent beats the repo's reality beats settled decisions beats spec prose) and edit `spec.md` only; anything that would flip a decision, change user-visible scope, or add a dependency is never auto-applied - the run ends by asking the user those as decisions, one at a time with alternatives and tradeoffs, and applies the answers to the spec before finishing.
An APPROVED verdict means every finding was refined or answered: `build` can start directly. Only an answer that invalidates the premise or opens a genuinely new effort defers to `scope`, recorded in `.dev/{plan-name}/spec-review_N.md` with its open question.

### build

Executes a spec's change sets at the layer each `tests:` scenario is tagged with - unit for business logic, integration for real cross-component seams, e2e for driving the actual running application.
Enforces outcomes rather than rituals: every test must have been seen to fail before its green counts, with strict failing-test-first reserved for bug fixes, where red is the proof the issue was actually reproduced.
Runs independent change sets in parallel as waves of subagents batched by disjoint file lists in spec order, committing each change set and appending to a running `implementation-notes.md` that logs any deviations forced by an edge case.
Once every change set is committed, drives the real app against a mocked environment, loops until every e2e scenario passes, then renders the e2e report: screenshots per scenario for frontend systems, Test Scenario and Data Model State tables for everything else.
Every run then asks whether to push and open a PR, whether or not Jira is configured (see Jira integration below).

### ship

Runs the quality pass that finishes a change, in two phases; by default both run, and either can be requested alone ("gauntlet only", "review only").
Phase 1 puts the change through deterministic tools that cannot be argued with, looping fresh-context fix agents until every check passes: every linter, type checker, and format checker the repo already configures, a security scan (secrets, vulnerable dependencies, SAST), dead code and duplication introduced by the diff, module dependency rules from `docs/dependencies.md`, coverage-weighted cyclomatic complexity per function, flakiness runs over diff-touched tests, and mutation testing over the in-scope files.
It acquires tools up an explicit ladder - the repo's own tooling, the ecosystem's established tool, or a small repo-fitted script committed under `tools/harden/` for reuse - and treats thresholds as recorded decisions in `docs/decisions.md`, never silently adjusted config.
When the gauntlet's fixes touched code, phase 1 ends by re-running the spec's `[e2e]` scenarios and overwriting the e2e report, so the evidence phase 2 audits describes the post-fix code.
Phase 2 reviews the post-fix diff with a read-only panel of concern-focused agents, selected per diff except the always-on simplify lens; the gauntlet checks mechanics, the panel judges meaning.
Every non-trivial finding is adversarially verified against the repo.
Reads `docs/contracts.md` boundary guarantees as premises before the panel runs, checks spec conformance - including whether `[e2e]`-tagged scenarios have a passing entry in the e2e report and whether logged deviations still satisfy the spec - and reconciles verified findings against `docs/decisions.md` only after judgment, reporting still-holds/reopened/diverged instead of re-litigating settled questions.
Claude Code, Codex, and opencode use their native parallel subagent facilities. Pi preserves the same independent two-batch panel by launching isolated `pi --print` subprocesses with the current provider, model, and reasoning level.
Renders `review_N.html` alongside the `review_N.md` file for reviewer handoff.

### commit

Groups all pending changes into granular, logically-separate commits - splitting within a file when needed - with structured messages: a `type(scope):` subject, `What:`/`Why:` body, optional `Considered:`/`Constraint:`/`Directive:`/`Symptoms:` sections, and `Severity:`/`Risk:` metadata trailers.
After committing, syncs drifted docs and captures durable decisions and contracts from the commit bodies into `docs/decisions.md` and `docs/contracts.md`, which is what keeps the ledger current without excavating git history later.
Pushes by default; say "commit only" to skip the push.

### to-pitch

Packages a finished change - its spec, implementation notes, and e2e evidence - into one buy-in document: demo first, then why, what changed, how it was verified, and how to try it.

### to-quiz

Renders a context/intuition/what-was-done report with a graded comprehension quiz.
Cannot enforce a merge gate, so it says so plainly and produces an honest pass/fail check instead.

## Run metrics

`scope`, `scope-review`, `build`, and `ship` each start by snapshotting the run with `scripts/skill-metrics.py start` and end by printing what it measured: wall time, tokens split between the orchestrator and its subagents, agents dispatched, tool calls, the git delta since the snapshot, and any counters the skill tallied from tool output.
The numbers come from the session transcript under `$CLAUDE_CONFIG_DIR` (default `~/.claude`) and from git, never from the model's recollection.
Every run appends a row to `.dev/metrics.jsonl` in the consuming repository, and the table compares the run against the median of earlier runs of the same skill, which is where a skill's cost and catch rate become visible over time.

## Jira integration

The spec-driven workflow can mirror its local state to Jira through the
Atlassian CLI (`acli`). Install and authenticate `acli` before enabling it.
Create `.dev/config.json` in the consuming repository:

```json
{
  "jira": {
    "enabled": true,
    "site": "acme.atlassian.net",
    "project": "PROJ"
  }
}
```

`site` is optional and `project` is required when enabled. An absent config
file or `jira.enabled: false` keeps the workflow pure-local with no Jira
calls or questions.

The hierarchy is Initiative > Epic > Task. `scope` asks you to choose
an existing open Initiative, creates one Epic under it once the change plan is
final, stores the Epic key in the spec, and creates one Jira Task per change
set, storing each key under its change set and closing old issues when a
change set is superseded. `build` moves the Epic to In Progress at start,
moves each change-set issue through In Progress and Done as the orchestrator
dispatches and commits it, then asks `push and open the PR?` on every run. A
yes creates a keyed branch when needed, pushes it, and opens a PR whose title
starts with the Epic key. A no stops after the e2e report.

One-time Jira administration is required. Install the GitHub for Jira
integration, then add an automation rule: "when a linked pull request is
merged, transition the Epic to Done". The Epic key in the branch name and PR
title is what lets Jira link the pull request and trigger that rule. The
skills do not poll for merges.

## Claude Code installation

```bash
/plugin marketplace add tobrun/skills
/plugin install dev@nurbot
```

Invoke skills as `/scope`, `/build`, and so on.

## Codex installation

```bash
codex plugin marketplace add tobrun/skills
codex plugin add dev@nurbot
```

Invoke skills as `$dev:scope`, `$dev:build`, and so on.

## opencode installation

opencode reads Claude-format `SKILL.md` files natively, so no generated distribution is needed.
Clone this repository and symlink the source skills into opencode's global skill directory:

```bash
git clone https://github.com/tobrun/skills ~/ws/skills
mkdir -p ~/.config/opencode/skills
for skill in ~/ws/skills/dev/skills/*/; do
  ln -sfn "$skill" ~/.config/opencode/skills/"$(basename "$skill")"
done
ln -sfn ~/ws/skills/dev/references ~/.config/opencode/references
```

The last symlink keeps the shared references (`jira.md`, `decision-ledger.md`, `contracts.md`) reachable through the `../../references/` links inside the skills.
Symlinks mean a `git pull` updates the skills in place; restart opencode afterwards, since skills load at startup.

opencode has no `disable-model-invocation` field (it is ignored harmlessly); skills load through a model-invoked `skill` tool.
Preserve the explicit-invocation policy with a permission rule in `~/.config/opencode/opencode.json`:

```json
{
  "permission": {
    "skill": {
      "*": "allow",
      "scope": "ask",
      "commit": "ask",
      "build": "ask",
      "ship": "ask",
      "to-pitch": "ask",
      "to-quiz": "ask"
    }
  }
}
```

Invoke a skill by asking for it by name, for example "run the scope skill on this request"; the permission rule makes opencode confirm before loading one.
Verify discovery with `opencode debug skill`.
`ship` runs its review panel through opencode's native `task` subagents.

## Pi installation

```bash
pi install git:github.com/tobrun/skills
```

Invoke skills as `/skill:scope`, `/skill:build`, and so on. Pi loads the
source skills directly. A `ship` review phase starts multiple model processes,
one per selected lens and then one per non-trivial finding verifier, so its
model usage scales with the panel size.
