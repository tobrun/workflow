# Evals

Status: maintained

Eval definitions for the `factory` plugin's skills.
They cover only what the factory copies add on top of `dev`: the run file, the result file, the unattended decision policy (take the recommended option; park only for the fixed list), run-provided paths, and never launching the next stage.
The shared behavior these skills inherit from `dev` is covered by `dev/evals/`; rerun those against the factory copy when a change touches inherited text.

- `{skill}.json` - one file per pipeline skill: `scope`, `scope-review`, `build`, `ship`.

Every factory eval runs in `"comprehension"` mode: the prompt stages a `.dev/factory-run.json` in words and asks what the skill does, graded against objective assertions.
The runner's deterministic gates are tested separately by `factory/runner/tests/` and `scripts/test_factory_runner.sh`; these evals check that the skill text keeps agreeing with those gates.

## When to run these

Whenever a factory `SKILL.md`, `references/factory-run.md`, or a reference that names a run path changes in a way that could affect behavior.
Run before and after the change (old text via `git show <commit>:path`, new text from the working tree) so the comparison is apples to apples, the same method `dev/evals/README.md` describes.

## Behavioral benchmark

`bench/` measures what the comprehension evals cannot: whether runs actually deliver the requested behavior, and whether a green run can be wrong.

- `bench/manifest.json` is the versioned task corpus: a bug fix, a feature, an ambiguous requirement, a dependency problem (missing credentials), an interrupted run, and a UI change (real hosts only).
- Each task under `bench/tasks/` has the request, the approved spec the harness seals as the run's intent, and `acceptance/` checks that never enter the target repository; they run against the revision the run produced.
- `bench/bench.py run --mode offline` drives stub hosts through scripted behaviors: the correct one, and negative controls for the bug-fix task (a broken implementation, inert tests with a fabricated driver, agent-written evidence, a missing review, stale review and gauntlet records).
  It measures the runner's gates, and `scripts/validate.sh` check F04 fails on any false green, any accepted negative control, or any failed correct task.
- `bench/bench.py run --mode real --real-config FILE` runs the real hosts against a sandbox GitHub repository (see `bench/real-config.example.json`); `max_runs` and `max_tokens_per_run` are hard limits, and nothing runs without that file.
- `bench/bench.py grade --run-dir DIR --task ID` grades a run a person scoped interactively; results record `scope` as `seeded` or `interactive`, so the two are never mixed silently.
- `bench/bench.py report DIR [--baseline DIR] [--markdown FILE]` reports task success, false-green rate, intervention rate, recovery success, and resource use, each with its numerator, denominator, and repetitions, per-task variance, and the runner and skills identities tested.

Run the real campaign on skill or model changes, comparing a candidate against a baseline on the same corpus with at least three repetitions per task:

```bash
python3 factory/evals/bench/bench.py run --mode real --real-config ~/.factory-bench.json --repetitions 3 --out /tmp/bench-candidate
python3 factory/evals/bench/bench.py report /tmp/bench-candidate --baseline /tmp/bench-baseline --markdown /tmp/bench-report.md
```

Every result keeps `run.json`, events, the sealed intent, each attempt's gate and runtime manifest, the acceptance output, and the produced revision.

