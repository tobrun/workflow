---
name: bootstrap
description: Set up a repository's architecture enforcement and its CI verification evidence - probe its languages and structure, infer the module graph the code already follows, write docs/architecture.toml with each rule argued as a D- decision, generate the boundary, banned-API, and dependency configs with arch.py, freeze existing violations in a baseline that only shrinks, prove every rule fires with a red team, wire it into CI, and add the CI job that writes each pull request's verification evidence at its head commit. Use once per repository before relying on the dev workflow's architecture checks, or to re-bootstrap after a large restructuring.
disable-model-invocation: true
---

# Bootstrap

The deliverable is a repository where the architecture is enforced, not described: `docs/architecture.toml`, the configs `arch.py` generates from it, a CI job that runs them, and the decisions behind every rule in `docs/decisions.md`.
The file format, the ladder, the per-ecosystem tools, and who may loosen what are in [../../references/architecture.md](../../references/architecture.md); the decision notation is in [../../references/decision-ledger.md](../../references/decision-ledger.md); authority follows [../../references/execution.md](../../references/execution.md).
`{arch}` below is `{bootstrap-skill-root}/../../scripts/arch.py`; after the first `generate` the repository has its own copy at `.arch/arch.py`, and every later command uses that one.

A repository that already has `docs/architecture.toml` is not bootstrapped again: run `python3 .arch/arch.py run`, report what fails, and stop unless the user asks to restructure.

## 1. Probe

Read the manifests and CI config for the languages, the source roots, and the existing boundary tools (import-linter, Tach, dependency-cruiser, eslint-plugin-boundaries, Nx, go-arch-lint, depguard, ArchUnit, Konsist, cargo-deny, Bazel visibility).
Read the structure the build already enforces: workspace crates, Gradle modules, Bazel packages, Go `internal/` directories, separate packages.
An existing boundary tool is replaced by the generated one, never stacked with it; carry its rules into the file and say which config the generated one supersedes.

## 2. Infer the architecture the code follows

For each language, run `python3 {arch} infer --language {language} --dir {source root} --depth {n}` with the depth at which modules sit.
It prints the observed edges with import counts, the cycles, and a draft `[[modules]]` block.
Decide the shape as the first decision - fixed layers, hexagonal (a domain core with no outward edges, ports, adapters at the edge), vertical slices that talk only through public APIs, or a process split (server and client, main and worker) - and argue it with alternatives like any `D-` decision.
An execution-context boundary always gets a hard split: code for one runtime never imports the other's.

Then, from the observed graph:
- Each common edge that fits the shape becomes a `may_depend_on` entry.
- A rare edge against the shape, or the rarer side of a cycle, becomes a `[[baseline]]` entry, not an allowed edge.
- Cross-cutting concerns (auth, telemetry, feature flags) enter through one `[providers]` module when the code already funnels them, or as a decision to start funnelling them.
- Banned APIs come from the mistakes the history shows: raw timers, raw process spawning, untyped fetch or JSON, effect hooks, workaround comments. Ban each from product code and allow it only in the framework module that wraps it, with two invalid examples from the real mistake and two valid near-misses.
- A dependency allowlist lists what the manifests declare today.

Encode only what the code does today or what the user decides; an aspirational rule the code does not follow is noise.
Pure taste stays out of the file.

## 3. Settle the rules with the user

Put each rule on the strongest level of the ladder that can hold it.
Where structure would make a rule unnecessary - a package split, `internal/`, a crate - recommend it as a decision, but do not restructure code in this run.
Apply recommendations at `Confidence: 50%` or above and list them in one block the user can overturn; ask below that, and always ask about the shape, about every baseline entry count over a handful, and about any rule that would change how the team works.

Write each rule's decision into `docs/decisions.md` under an `Architecture` area, with its alternatives and an `enforced by:` line naming the rule ids it backs.
Every rule cites its decision, every `enforced by:` id exists, and `arch.py validate` fails until both hold.

## 4. Generate and prove

1. Write `docs/architecture.toml` and loop `python3 {arch} validate` until clean.
2. `python3 {arch} generate`, then install each generated tool as the ecosystem's dev dependency, pinned (Tach and other pre-1.0 tools especially).
3. `python3 .arch/arch.py run`. A violation the user accepted as existing goes into `[[baseline]]`; a new repository starts with an empty baseline. Regenerate and run again until clean.
4. `python3 .arch/arch.py redteam` until every rule fires. A rule that stays green enforces nothing: fix the rule or the tool setup, never accept it. Report each `skip` with its reason.

## 5. Wire it in

- **CI**: add a job to the repository's pull-request workflow running `python3 .arch/arch.py run --base origin/{default branch}` with the tools installed. CI is the authority; it runs for every change, whoever wrote it.
- **The Validation block** of later specs picks the job up from CI, so `build` and `ship` run it without being told.
- **AGENTS.md**: one line under Architecture pointing to `docs/architecture.toml` and saying that generated files under `.arch/` are never edited and that loosening a rule is a human call. The rules themselves stay in the file, not in prose.

## 6. The evidence job

Pull requests carry two kinds of verification text: what the agent claims, and what CI measured at the head commit, which the agent cannot write. The second comes from `ci-evidence.py`:

1. Copy `{bootstrap-skill-root}/../../scripts/ci-evidence.py` to `.verify/ci-evidence.py`.
2. Write `.github/workflows/verify.yml` from [assets/verify.yml](assets/verify.yml), filling each `<<fill: ...>>` from the repository's existing CI: the toolchain setup, a test command that writes JUnit XML and a coverage report (Cobertura, LCOV, JaCoCo, or a Go profile), and a revert command that runs only the given test files.
3. Prove it before committing: on a branch with a changed test, run `python3 .verify/ci-evidence.py collect --base origin/{default branch} --junit ... --coverage ... --revert-cmd ... --out /tmp/ci-evidence` with the same commands and read the block it writes. A row reading `not run` or `no report` is a setup gap to close, not a result.
4. On a host other than GitHub, wire the same four steps (pending, test, collect, publish) into its pipeline; `publish --body-file` writes the block into a body file for any tool that edits PR descriptions.

Commit the file, the generated configs, the CI changes, the vendored scripts, and the ledger entries per the commit skill's [message format](../commit/references/message-format.md) on the current work branch, under the same authority as `scope`'s ledger commit.
Report the rules by ladder level, the baseline size per rule, the red-team result, the evidence job's preflight block, and what the probe found that no rule covers yet.
