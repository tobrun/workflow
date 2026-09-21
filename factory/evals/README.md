# Factory evals

Three levels of verification (D-verification):

1. **Structural, free**: `scripts/validate.sh`'s `check_factory_unattended`, `check_factory_protocol`, and `check_factory_script` - run on every `scripts/validate.sh` invocation.
2. **Unit tests, free**: `python3 -m unittest discover -s factory/evals/tests -t .` - `test_run_state.py`, `test_factory_config.py`, `test_stub_pipeline.py`, `test_factory_checks.py`, `test_fixture_setup.py`. The stub pipeline in `factory/evals/stubs/` proves the bookkeeping (attempts, timestamps, terminality, budgets, drift) and nothing about the orchestrator's prose loop.
3. **One real end-to-end run per host, paid and manual**: this file's procedure, recorded in `results.md`.

## The end-to-end procedure

Per host (Claude Code, Codex), because these are paid host sessions build's mocked `[e2e]` environment cannot execute:

1. Set up the fixture: `bash factory/evals/fixture/setup.sh` and note the destination it prints.
2. Before opening the host, check the per-host setup below is in place, the secret scanner included - the gauntlet's acquire-a-missing-tool step needs a network this fixture run does not have.
3. Open the host in the fixture's destination directory.
4. Invoke `/factory:run "Add idempotency keys to webhook delivery"`.
5. Answer the interview, say go, then leave - no question should come after the go.
6. Once the run ends, record the outcome in `results.md`.

### Pass criteria

- The branch `factory/{plan}` pushed to the fixture's bare origin, with one commit per change set.
- `pr.md` written.
- `factory-run.json` recording `scope`, `scope-review`, and `build` as `done`, with every decision, and `ship`'s last attempt `failed` with the no-GitHub-remote reason.
- No question asked after the go.
- Every result file echoing `skill_path` as the path the launch prompt passed.
- `git log --name-only` on the run branch names no `.dev/` path.

Ship reaching `gh pr create` and failing there is part of the pass, not an exception to it: with `gh` installed and authenticated the command still refuses with "none of the git remotes configured for this repository point to a known GitHub host" before any network call, the orchestrator judges that under the third `gh` fault category in [`judgment.md`](../skills/run/references/judgment.md), and the run ends with a report naming the origin and the pull request as the one unfinished step. There is no run in which all four phases are `done` on this fixture; that ending leaves the pull-request step and the Codex GitHub prerequisites unproven.

### Custom-pipeline variant

The stub fixture cannot prove the prose loop in `run/SKILL.md`, so a real run on a pipeline that is not ours is its own paid, manual check, one per host. In the fixture's destination, write a two-phase `.factory/config.yaml` with `factory-config.py init` and `set`: a first `interview` phase and a second unattended phase, both pointing at skills that are not ours (a minimal skill that writes one file is enough), then run `python3 factory/scripts/factory-config.py check` and expect exit 0. Invoke `/factory:run "a request"`.

The variant passes when the run ends with `finished: yes` in `run-state.py show`, the closing report names each phase's type, skill path and timings from the attempt records, the interactive phase ran inline and the go followed it, and no phase was launched before the go. Record any of these that did not hold in `results.md`.

### Foreign-phase variant

One per host, on the same custom pipeline: make the unattended phase's skill deliberately fail its first attempt (write no result file, or a result whose `status` is `failed`). The variant passes when the attempt is closed failed with reason "no result file" or the phase's own reason, the orchestrator relaunches it or ends the run and never hand-repairs it (`show --resolved` classes it foreign), and the phase carried `unattended_safe: true`. Also run the pipeline once with that declaration removed and expect `check` to exit 1 naming the phase before any interview.

### Planted-secret variant

One per host, before build's first commit: write into `webhook/__init__.py` a `GITHUB_TOKEN = "ghp_{36 random alphanumerics}"`, generated at setup time as one contiguous literal:

```bash
LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 36
```

A contiguous, high-entropy literal is required: real gitleaks 8.30.1 reports "no leaks found" for `"ghp_" + "0" * 36`, both as written and concatenated, because the `github-pat` rule has an entropy floor an all-zeros body fails. The repo-fitted grep rung in [`tools.md`](../phases/ship/references/tools.md) matches the same contiguous high-entropy shape.

The variant passes when ship reports `stopped` with kind `secret.found`, nothing is pushed to the bare origin, and the report names the commit to purge.

### Per-host setup

- Every host: a secret scanner ship's gauntlet can run offline - `gitleaks`, or the offline repo-fitted grep [`tools.md`](../phases/ship/references/tools.md) allows.
- Codex additionally: `GH_TOKEN` (or `GITHUB_TOKEN`), `gh auth setup-git`, `sandbox_workspace_write.network_access=true`, `agents.max_depth` at least 2.

A variant run that fails for a missing scanner is a setup fault: record it as one and re-run, not a factory fault.

### Recorded as untestable, not run

A host-side subagent crash mid-build, and a user editing the checkout during a run: no mocked layer can drive a real host, and a host-side crash cannot be reproduced deterministically. Record the attempt and note it as untestable rather than treating a missing scenario as a gap.
